"""
apps/backtest/engine.py — 事件驱动回测引擎（BacktestEngine）

设计说明：
- 回测引擎是整个系统的"指挥中心"，负责协调以下组件：
    DataFeed → EventBus → Strategy → SimulatedBroker → Reporter

- 严格的时间顺序保证（防未来函数）：
    每个时间步只有当前 K 线数据对策略可见，
    策略产出的订单只在下一根 K 线才可能成交（由 Broker 保证）。

- 与实盘代码路径对齐：
    策略通过 EventBus 订阅 KlineEvent 并发布 OrderRequestEvent，
    Broker 通过 EventBus 订阅 OrderRequestEvent 进行撮合，
    这与实盘的事件驱动流程完全一致。

- 权益曲线追踪：
    每个时间步结束后记录当前净值（cash + position market value）。

接口：
    BacktestEngine(feed, broker, strategies, reporter)
    .run() → BacktestResult   运行全流程，返回绩效报告
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from apps.backtest.broker import SimulatedBroker
from apps.backtest.reporter import BacktestReporter
from core.event import (
    EventBus,
    EventType,
    KlineEvent,
    OrderFilledEvent,
    OrderRequestEvent,
)
from core.logger import get_logger
from modules.data.feed import DataFeed

log = get_logger(__name__)

# 策略回调类型：接收 KlineEvent，返回 OrderRequestEvent 列表（可以为空）
StrategyHandler = Callable[[KlineEvent], List[OrderRequestEvent]]


@dataclass
class BacktestConfig:
    """回测配置参数。"""
    initial_cash: float = 100_000.0
    fee_rate: float = 0.001
    slippage_rate: float = 0.001


@dataclass
class BacktestResult:
    """回测结果聚合对象。"""
    metrics: Dict[str, float]
    equity_df: pd.DataFrame
    trade_log: pd.DataFrame
    config: BacktestConfig
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    reporter: Optional[BacktestReporter] = None


class BacktestEngine:
    """
    事件驱动回测引擎。

    工作流程（每个时间步）：
    1. DataFeed.step() → 发布 KlineEvent 到事件总线
    2. 事件总线通知所有策略处理器
    3. 策略处理器产出 OrderRequestEvent（若有信号）
    4. OrderRequestEvent 发布到事件总线
    5. SimulatedBroker 接收并尝试撮合（防未来函数约束）
    6. 成交后发布 OrderFilledEvent
    7. 记录当前时间步的权益快照

    Args:
        feed:       DataFeed 实例（历史数据喂入器）
        broker:     SimulatedBroker 实例
        config:     BacktestConfig 参数对象
        bus:        事件总线（None 时使用全局单例）
    """

    def __init__(
        self,
        feed: DataFeed,
        broker: SimulatedBroker,
        config: BacktestConfig = BacktestConfig(),
        bus: Optional[EventBus] = None,
    ) -> None:
        self.feed = feed
        self.broker = broker
        self.config = config
        self.bus = bus or feed.bus

        self._strategies: List[StrategyHandler] = []
        self._equity_records: List[Dict[str, Any]] = []
        self._latest_prices: Dict[str, float] = {}

        # 订阅 KLINE_UPDATED 事件，内部分发给 broker 和录入最新价格
        self.bus.subscribe(EventType.KLINE_UPDATED, self._on_kline)

    # ────────────────────────────────────────────────────────────
    # 公开接口
    # ────────────────────────────────────────────────────────────

    def add_strategy(self, handler: StrategyHandler) -> None:
        """
        注册策略处理函数。

        策略函数签名：
            def my_strategy(event: KlineEvent) -> List[OrderRequestEvent]:
                ...
        """
        self._strategies.append(handler)
        log.info("注册策略: {}", handler.__name__)

    def run(self) -> BacktestResult:
        """
        运行完整回测流程。

        Returns:
            BacktestResult 对象（含绩效指标、权益曲线、成交日志）
        """
        log.info("回测开始: initial_cash={} 策略数={}", self.config.initial_cash, len(self._strategies))

        start_dt = datetime.now()

        # 加载数据
        self.feed.load()

        # 推进时间流
        total_events = 0
        while self.feed.has_next():
            events = self._process_next_step()
            total_events += len(events)

        end_dt = datetime.now()
        elapsed = (end_dt - start_dt).total_seconds()
        log.info(
            "回测结束: 历时 {:.2f}s 处理 {} 个事件", elapsed, total_events
        )

        # 汇总结果
        equity_df = self._build_equity_df()
        trade_log = self.broker.get_trade_log()

        reporter = BacktestReporter(
            equity_df=equity_df,
            trade_log_df=trade_log,
            initial_cash=self.config.initial_cash,
        )
        metrics = reporter.compute()

        return BacktestResult(
            metrics=metrics,
            equity_df=equity_df,
            trade_log=trade_log,
            config=self.config,
            start_time=start_dt,
            end_time=end_dt,
            reporter=reporter,
        )

    # ────────────────────────────────────────────────────────────
    # 内部事件处理
    # ────────────────────────────────────────────────────────────

    def _process_next_step(self) -> List[KlineEvent]:
        """
        推进一个时间步：
        1. 从 DataFeed 获取当前时间步的所有 KlineEvent
        2. 发布到事件总线（会触发 _on_kline 以及策略回调）
        3. 处理策略产出的订单请求

        Returns:
            本时间步对应的 KlineEvent 列表
        """
        events = list(self.feed.iter_events().__next__())  # 取一步

        # 手动发布每个 KlineEvent（iter_events 不自动发布）
        for event in events:
            self.bus.publish(event)

        # 记录权益快照（每个时间步结束时）
        self._record_equity(events)

        return events

    def _on_kline(self, event: KlineEvent) -> None:
        """
        KlineEvent 事件处理器：
        1. 更新最新价格
        2. 通知 broker 尝试撮合挂单
        3. 驱动所有策略产出 OrderRequest
        """
        # 更新最新价格字典
        self._latest_prices[event.symbol] = float(event.close)

        # Broker 撮合（限价单检查）
        filled_events = self.broker.on_kline(event)
        for fe in filled_events:
            self.bus.publish(fe)

        # 调用所有策略
        for strategy in self._strategies:
            try:
                order_requests = strategy(event)
                for req in (order_requests or []):
                    self.bus.publish(req)
                    self.broker.submit_order(req)
            except Exception:  # noqa: BLE001
                log.exception("策略执行异常（已跳过）: strategy={}", strategy.__name__)

    def _record_equity(self, events: List[KlineEvent]) -> None:
        """在每个时间步结束时记录账户净值快照。"""
        if not events:
            return
        # 取当前时间步最后一个事件的时间戳作为快照时间
        ts = events[-1].timestamp
        equity = float(self.broker.get_equity(self._latest_prices))
        self._equity_records.append({
            "timestamp": ts,
            "equity": equity,
            "cash": float(self.broker.get_cash()),
        })

    def _build_equity_df(self) -> pd.DataFrame:
        """构建权益曲线 DataFrame。"""
        if not self._equity_records:
            return pd.DataFrame(columns=["timestamp", "equity", "cash"])
        df = pd.DataFrame(self._equity_records)
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df
