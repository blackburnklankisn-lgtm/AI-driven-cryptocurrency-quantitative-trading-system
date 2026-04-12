"""
modules/alpha/base.py — Alpha 策略基类

设计说明：
- 所有策略必须继承此基类，确保接口统一
- 策略只负责"产出信号"（方向 + 置信度），不直接下单
- 下单意图通过 OrderRequestEvent 传递给风控层处理
- 策略维护内部滑动窗口，禁止直接访问未来数据

策略生命周期：
    init()     → on_kline(event) → ...重复... → teardown()

接口约定：
    on_kline(event: KlineEvent) → List[OrderRequestEvent]
        每根 K 线到来时调用，返回本 K 线产出的订单请求列表（可为空）

关键防护：
    - 策略内部应只使用已收线的 K 线数据（is_closed=True）
    - 禁止在策略层访问当前 K 线收线前的价格预测
    - 所有参数应通过构造函数显式注入，不允许全局变量
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from core.event import EventType, KlineEvent, OrderRequestEvent
from core.logger import get_logger

log = get_logger(__name__)


class BaseAlpha(ABC):
    """
    Alpha 策略基类。

    所有策略需实现 on_kline() 方法，返回 OrderRequestEvent 列表。
    基类提供辅助工具：日志、订单构建、策略 ID 管理。

    Args:
        strategy_id: 唯一策略标识符（用于追溯审计）
        symbol:      目标交易对
        timeframe:   目标 K 线周期
    """

    def __init__(
        self,
        strategy_id: str,
        symbol: str,
        timeframe: str = "1h",
    ) -> None:
        self.strategy_id = strategy_id
        self.symbol = symbol
        self.timeframe = timeframe
        self._bar_count = 0  # 收到的 K 线计数（用于预热判断）
        log.info(
            "策略初始化: id={} symbol={} timeframe={}",
            strategy_id,
            symbol,
            timeframe,
        )

    @abstractmethod
    def on_kline(self, event: KlineEvent) -> List[OrderRequestEvent]:
        """
        处理新 K 线事件，返回订单请求列表。

        实现要求：
        - 只使用 event.is_closed=True 的已收线 K 线
        - 保持计算的幂等性（相同输入精确产出相同输出）
        - 抛出的任何异常将被引擎捕获并记录，不会崩溃系统

        Args:
            event: KlineEvent（包含 OHLCV 数据）

        Returns:
            订单请求列表；无信号时返回空列表 []
        """

    def init(self) -> None:
        """策略初始化钩子（可选实现）。回测开始前调用。"""

    def teardown(self) -> None:
        """策略销毁钩子（可选实现）。回测结束后调用。"""

    # ────────────────────────────────────────────────────────────
    # 受保护工具方法（供子类使用）
    # ────────────────────────────────────────────────────────────

    def _make_market_order(
        self,
        event: KlineEvent,
        side: str,
        quantity: Decimal,
    ) -> OrderRequestEvent:
        """
        构建市价单请求。

        Args:
            event:    触发信号的 KlineEvent（用于时间戳和 symbol 来源）
            side:     "buy" | "sell"
            quantity: 下单数量

        Returns:
            OrderRequestEvent（市价单，价格为 None）
        """
        return OrderRequestEvent(
            event_type=EventType.ORDER_REQUESTED,
            timestamp=event.timestamp,
            source=self.strategy_id,
            symbol=event.symbol,
            side=side,
            order_type="market",
            quantity=quantity,
            price=None,
            strategy_id=self.strategy_id,
            request_id=str(uuid.uuid4()),
        )

    def _make_limit_order(
        self,
        event: KlineEvent,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> OrderRequestEvent:
        """
        构建限价单请求。

        Args:
            event:    触发信号的 KlineEvent
            side:     "buy" | "sell"
            quantity: 下单数量
            price:    限价

        Returns:
            OrderRequestEvent（限价单）
        """
        return OrderRequestEvent(
            event_type=EventType.ORDER_REQUESTED,
            timestamp=event.timestamp,
            source=self.strategy_id,
            symbol=event.symbol,
            side=side,
            order_type="limit",
            quantity=quantity,
            price=price,
            strategy_id=self.strategy_id,
            request_id=str(uuid.uuid4()),
        )

    def _increment_bar(self, event: KlineEvent) -> None:
        """递增 K 线计数（仅统计已收线的 K 线）。"""
        if event.is_closed:
            self._bar_count += 1

    def _is_warming_up(self, min_bars: int) -> bool:
        """
        检查策略是否处于预热阶段（历史数据不足够）。

        在预热期间，策略不应产出信号。

        Args:
            min_bars: 策略所需的最少历史 K 线数量
        """
        return self._bar_count < min_bars
