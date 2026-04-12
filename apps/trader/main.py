"""
apps/trader/main.py — 实盘/模拟盘主控程序

设计说明：
- 这是系统的"顶层入口"，整合所有六层组件
- 支持三种运行模式（由 TRADING_MODE 环境变量控制）：
    paper:    模拟盘（与实盘相同代码路径，不真实下单）
    live:     实盘（调用交易所 API 真实发单）
    backtest: 不使用此入口，走 BacktestEngine

主控循环设计（同步版本，简单可靠）：
1. 初始化所有组件（配置 → 日志 → 指标 → 网关 → 风控 → 策略）
2. 启动 Prometheus HTTP 服务
3. 进入主循环：
    a. 通过 CCXT REST 轮询最新 K 线
    b. 发布 KlineEvent 到事件总线
    c. 策略处理，产出 OrderRequestEvent（若有信号）
    d. RiskManager 审核
    e. OrderManager 提交发单
    f. 轮询成交回报，更新持仓状态
    g. 更新 Prometheus 指标
    h. 睡眠到下一个轮询间隔

安全措施：
- 捕获 KeyboardInterrupt 优雅退出
- 任何未捕获异常都触发邮件/Slack 告警（可扩展）
- 主循环内任何单次异常不崩溃整个进程（记录并继续）

运行方式：
    TRADING_MODE=paper python -m apps.trader.main
"""

from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional
import threading
import uvicorn
import asyncio
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.config import load_config
from core.event import EventBus, EventType, KlineEvent, OrderRequestEvent
from core.logger import audit_log, get_logger, setup_logging
from modules.execution.gateway import CCXTGateway
from modules.execution.order_manager import OrderManager
from modules.monitoring.metrics import SystemMetrics
from modules.risk.manager import RiskConfig, RiskManager
from apps.api.server import app as fast_app, set_trader_instance, WebsocketLogSink

log = get_logger(__name__)

# 将 WebsocketLogSink 挂载到 loguru
from loguru import logger
logger.add(WebsocketLogSink(), format="{time:HH:mm:ss} | {level} | {message}", level="INFO")

class LiveTrader:
    """
    实盘/模拟盘主控类。

    负责协调所有组件的初始化和主循环驱动。
    策略以插件方式注册，与实盘代码完全解耦。

    Args:
        config_path: 系统配置文件路径
    """

    def __init__(self, config_path: str = "configs/system.yaml") -> None:
        # ── 1. 加载系统配置 ──────────────────────────────────────
        self.sys_config = load_config(config_path)
        self.mode = self.sys_config.trading_mode

        if self.mode == "backtest":
            raise RuntimeError("LiveTrader 不支持 backtest 模式，请使用 BacktestEngine")

        # ── 2. 初始化日志 ────────────────────────────────────────
        setup_logging(
            log_dir=self.sys_config.logging.log_dir,
            log_level=self.sys_config.logging.log_level,
        )

        # ── 3. 系统组件 ──────────────────────────────────────────
        self.bus = EventBus()
        self.metrics = SystemMetrics(
            exchange_id=self.sys_config.exchange.exchange_id,
            mode=self.mode,
        )

        # ── 4. 交易所网关 ────────────────────────────────────────
        exc_cfg = self.sys_config.exchange
        self.gateway = CCXTGateway(
            exchange_id=exc_cfg.exchange_id,
            mode=self.mode,
            api_key=exc_cfg.binance_api_key,
            secret=exc_cfg.binance_secret,
            timeout_ms=exc_cfg.request_timeout_ms,
        )
        self.order_manager = OrderManager(self.gateway, fill_timeout_s=300)

        # ── 5. 风控管理器 ────────────────────────────────────────
        risk_cfg = self.sys_config.risk
        self.risk_manager = RiskManager(RiskConfig(
            max_position_pct=float(risk_cfg.max_position_pct),
            max_portfolio_drawdown=float(risk_cfg.max_portfolio_drawdown),
            max_daily_loss=float(risk_cfg.max_daily_loss),
            max_consecutive_losses=risk_cfg.max_consecutive_losses,
        ))

        # ── 6. 策略列表（外部注入） ───────────────────────────────
        self._strategies = []

        # 运行状态
        self._running = False
        self._positions: Dict[str, Decimal] = {}
        self._latest_prices: Dict[str, float] = {}
        self._current_equity: float = 0.0

        # 轮询间隔（根据 timeframe 动态调整，1h K 线每 60s 轮询一次）
        self._poll_interval_s: float = 60.0

        log.info(
            "LiveTrader 初始化完成: exchange={} mode={} symbols={}",
            exc_cfg.exchange_id,
            self.mode,
            self.sys_config.data.default_symbols,
        )

    # ────────────────────────────────────────────────────────────
    # 策略注册接口
    # ────────────────────────────────────────────────────────────

    def add_strategy(self, strategy_obj) -> None:
        """
        注册策略对象（需实现 on_kline(event) → List[OrderRequestEvent]）。
        """
        self._strategies.append(strategy_obj)
        log.info("注册实盘策略: {}", getattr(strategy_obj, "strategy_id", type(strategy_obj).__name__))

    # ────────────────────────────────────────────────────────────
    # 主控流程
    # ────────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        启动主控程序。现在 Uvicorn 占用主线程，交易逻辑在守护线程运行。
        """
        set_trader_instance(self)
        
        # 将原主循环丢入子线程
        trader_thread = threading.Thread(target=self._run_loop, daemon=True)
        trader_thread.start()
        
        # 主线程阻塞运行 API
        log.info("启动本地通信层 API: http://127.0.0.1:8000")
        uvicorn.run(fast_app, host="127.0.0.1", port=8000, log_level="info")

    def _run_loop(self) -> None:
        """原有的交易主引擎循环"""
        # 启动 Prometheus 指标服务
        SystemMetrics.start_http_server(port=8001)  # 改为 8001 避免与 FastAPI 冲突

        audit_log("SYSTEM_STARTUP", mode=self.mode)
        log.info("=" * 50)
        log.info("系统启动: mode={} exchange={}", self.mode, self.gateway.exchange_id)
        log.info("=" * 50)

        self._running = True
        heartbeat_seq = 0

        while self._running:
            loop_start = time.monotonic()
            heartbeat_seq += 1

            try:
                self._main_loop_step(heartbeat_seq)
            except KeyboardInterrupt:
                break
            except Exception:  # noqa: BLE001
                log.exception("主循环异常（已记录，继续运行）: seq={}", heartbeat_seq)

            # 精确等待到下一个轮询间隔
            elapsed = time.monotonic() - loop_start
            sleep_time = max(0, self._poll_interval_s - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

        self._shutdown()

    # ────────────────────────────────────────────────────────────
    # 单次循环步骤
    # ────────────────────────────────────────────────────────────

    def _main_loop_step(self, seq: int) -> None:
        """
        一次完整的主控循环，包含以下步骤：
        1. 心跳记录
        2. 轮询最新 K 线行情
        3. 发布 KlineEvent → 策略处理
        4. 风控审核 → 发单
        5. 轮询成交回报 + 超时撤单
        6. 更新账户快照 + 指标上报
        7. 每日重置检查
        """
        now = datetime.now(tz=timezone.utc)
        self.metrics.record_heartbeat()
        log.debug("主循环心跳: seq={} ts={}", seq, now.isoformat())

        symbols = self.sys_config.data.default_symbols

        # Step 1: 拉取最新行情，构建 KlineEvent
        kline_events = self._fetch_latest_klines(symbols)

        # Step 2: 驱动策略
        for event in kline_events:
            self._latest_prices[event.symbol] = float(event.close)
            self._process_kline_event(event)

        # Step 3: 轮询成交回报
        fills = self.order_manager.poll_fills()
        for fill in fills:
            self._on_fill(fill)

        # Step 4: 撤销超时订单
        cancelled = self.order_manager.cancel_timed_out_orders()
        if cancelled > 0:
            log.warning("超时撤单 {} 笔", cancelled)

        # Step 5: 更新账户快照与指标
        self._update_account_snapshot()

        # Step 6: 每日重置检测（UTC 00:00 后的第一次循环）
        self._check_daily_reset(now)

    def _fetch_latest_klines(self, symbols: List[str]) -> List[KlineEvent]:
        """
        通过 CCXT 轮询最新 K 线（REST API）。

        返回最新收线的 K 线事件列表。
        Paper 模式下也会真实调用行情接口（行情读取不需要 API Key）。
        """
        events = []
        for symbol in symbols:
            try:
                fetch_start = time.monotonic()
                ticker = self.gateway.fetch_ticker(symbol)
                latency_ms = (time.monotonic() - fetch_start) * 1000
                self.metrics.record_data_latency(latency_ms)

                # ticker 只含 last/bid/ask，构建简化的 KlineEvent
                last_price = ticker.get("last", 0)
                if not last_price:
                    continue

                # 注意：ticker 事件不是完整 K 线，仅用于驱动策略的简化版
                # 生产环境建议改用 WebSocket 订阅完整 OHLCV
                event = KlineEvent(
                    event_type=EventType.KLINE_UPDATED,
                    timestamp=datetime.now(tz=timezone.utc),
                    source="live_feed",
                    symbol=symbol,
                    timeframe=self.sys_config.data.default_timeframe,
                    open=Decimal(str(last_price)),
                    high=Decimal(str(ticker.get("high", last_price))),
                    low=Decimal(str(ticker.get("low", last_price))),
                    close=Decimal(str(last_price)),
                    volume=Decimal(str(ticker.get("baseVolume", 0))),
                    is_closed=False,  # ticker 数据不是已收线 K 线
                )
                events.append(event)

            except Exception as exc:  # noqa: BLE001
                log.warning("获取行情失败: symbol={} error={}", symbol, exc)

        return events

    def _process_kline_event(self, event: KlineEvent) -> None:
        """
        处理 KlineEvent：通知策略 → 风控审核 → 提交订单。
        """
        # 发布到事件总线（其他订阅者可扩展）
        self.bus.publish(event)

        # 更新风控状态
        equity = self._current_equity or float(self.sys_config.risk.max_position_pct) * 100_000
        self.risk_manager.update_equity(equity)

        # 驱动所有策略
        for strategy in self._strategies:
            try:
                order_requests = strategy.on_kline(event)
                for req in (order_requests or []):
                    self._process_order_request(req, equity)
            except Exception:  # noqa: BLE001
                log.exception("策略异常: strategy={}", getattr(strategy, "strategy_id", "unknown"))

    def _process_order_request(self, req: OrderRequestEvent, equity: float) -> None:
        """
        处理单个订单请求：风控审核 → 提交到 OrderManager。
        """
        # 记录信号指标
        self.metrics.record_signal(req.strategy_id, req.side)

        # 风控审核
        allowed, reason = self.risk_manager.check(
            side=req.side,
            symbol=req.symbol,
            quantity=req.quantity,
            price=float(req.price or 0),
            current_equity=equity,
            positions=dict(self._positions),
        )

        if not allowed:
            self.metrics.record_order_rejected(req.symbol, reason)
            return

        # 通过风控，提交订单
        try:
            self.metrics.record_order_submitted(req.symbol, req.side, req.order_type)
            self.order_manager.submit(
                symbol=req.symbol,
                side=req.side,
                order_type=req.order_type,
                quantity=req.quantity,
                price=req.price,
                strategy_id=req.strategy_id,
                request_id=req.request_id,
            )
        except Exception as exc:
            log.error("订单提交失败: {} {} 原因={}", req.symbol, req.side, exc)

    def _on_fill(self, fill) -> None:
        """处理成交回报：更新持仓，记录指标。"""
        rec = fill.order_record
        if rec.side == "buy":
            self._positions[rec.symbol] = (
                self._positions.get(rec.symbol, Decimal("0")) + fill.new_filled_qty
            )
        elif rec.side == "sell":
            current = self._positions.get(rec.symbol, Decimal("0"))
            self._positions[rec.symbol] = max(
                Decimal("0"), current - fill.new_filled_qty
            )

        notional = float(fill.new_filled_qty * fill.avg_price)
        self.metrics.record_order_filled(
            rec.symbol, rec.side, float(fill.new_filled_qty), notional, fee=notional * 0.001
        )

    def _update_account_snapshot(self) -> None:
        """查询账户余额，更新净值和持仓指标。"""
        try:
            balance = self.gateway.fetch_balance()
            usdt_free = balance.get("USDT", {}).get("free", 0) if isinstance(balance.get("USDT"), dict) else 0
            positions_value = sum(
                float(qty) * self._latest_prices.get(sym, 0)
                for sym, qty in self._positions.items()
            )
            self._current_equity = usdt_free + positions_value
            self.metrics.update_equity(self._current_equity)

            for sym, qty in self._positions.items():
                notional = float(qty) * self._latest_prices.get(sym, 0)
                self.metrics.update_position(sym, float(qty), notional)

        except Exception as exc:  # noqa: BLE001
            log.warning("更新账户快照失败: {}", exc)

    def _check_daily_reset(self, now: datetime) -> None:
        """检查是否需要执行每日重置（UTC 00:00 ~ 00:01 之间的第一次循环）。"""
        if now.hour == 0 and now.minute == 0:
            self.risk_manager.reset_daily(self._current_equity)
            log.info("每日风控重置完成")

    # ────────────────────────────────────────────────────────────
    # 优雅退出
    # ────────────────────────────────────────────────────────────

    def _signal_handler(self, signum, frame) -> None:
        log.warning("收到信号 {}，准备优雅退出...", signum)
        self._running = False

    def _shutdown(self) -> None:
        """优雅关机：取消所有挂单，记录审计日志，关闭连接。"""
        log.info("系统关机中...")

        # 取消所有未完成订单
        open_orders = self.order_manager.get_open_orders()
        for order in open_orders:
            try:
                self.gateway.cancel_order(order.exchange_id, order.symbol)
                log.info("关机撤单: {}", order.exchange_id)
            except Exception as exc:
                log.error("关机撤单失败: {} error={}", order.exchange_id, exc)

        self.gateway.close()

        audit_log(
            "SYSTEM_SHUTDOWN",
            mode=self.mode,
            open_orders_cancelled=len(open_orders),
            final_equity=self._current_equity,
        )
        log.info("系统已安全退出。最终净值: {:.2f} USDT", self._current_equity)


def main() -> None:
    """程序入口（通过 pyproject.toml scripts 注册）。"""
    config_path = os.environ.get("CONFIG_PATH", "configs/system.yaml")
    trader = LiveTrader(config_path=config_path)

    # 注册策略（动态导入，按需配置）
    from modules.alpha.strategies.ma_cross import MACrossStrategy

    symbols = trader.sys_config.data.default_symbols
    for symbol in symbols:
        strategy = MACrossStrategy(
            symbol=symbol,
            fast_window=10,
            slow_window=30,
            order_qty=0.001,  # 极小仓位，paper 模式安全演示
            volume_filter=True,
            timeframe=trader.sys_config.data.default_timeframe,
        )
        trader.add_strategy(strategy)

    trader.run()


if __name__ == "__main__":
    main()
