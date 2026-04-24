"""
core/event.py — 事件定义与事件总线（EventBus）

设计原则：
- 事件驱动架构（EDA）的核心枢纽
- 回测模式：本地内存队列，单线程顺序消费，保证时间顺序
- 实盘模式：可切换为 Redis Pub/Sub 或 asyncio Queue
- 所有事件均为不可变数据类（dataclass/frozen），禁止在回调中修改事件
- 事件类型使用 Enum 统一管理，禁止魔法字符串

失败模式：
- 消费者抛出异常时，记录错误日志后继续其他消费者（隔离性）
- 不允许单个消费者阻塞整个总线
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum, auto
from typing import Any, Callable, Coroutine, Dict, List, Union

from core.logger import get_logger

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、事件类型枚举
# ══════════════════════════════════════════════════════════════

class EventType(Enum):
    """系统内所有事件类型的统一枚举。新增事件必须在此声明。"""

    # 数据层事件
    TICK_RECEIVED = auto()          # 收到单个 Ticker 行情
    KLINE_UPDATED = auto()          # K 线数据更新（含收线）
    ORDER_BOOK_UPDATED = auto()     # 盘口深度更新
    DATA_VALIDATION_FAILED = auto() # 数据校验不通过

    # Alpha 层事件
    SIGNAL_GENERATED = auto()       # 策略 / AI 产出信号
    FEATURE_COMPUTED = auto()       # 特征向量计算完成

    # 风控层事件
    RISK_CHECK_PASSED = auto()      # 风控通过，允许下单
    RISK_LIMIT_BREACHED = auto()    # 风控硬拦截，订单被拒绝
    CIRCUIT_BREAKER_TRIGGERED = auto()  # 熔断器触发

    # 执行层事件
    ORDER_REQUESTED = auto()        # 申请下单（来自策略）
    ORDER_SUBMITTED = auto()        # 订单提交到交易所
    ORDER_FILLED = auto()           # 订单成交（部分或全部）
    ORDER_CANCELLED = auto()        # 订单撤销
    ORDER_FAILED = auto()           # 订单失败

    # 分析层事件
    POSITION_UPDATED = auto()       # 持仓变动后的状态更新
    EQUITY_SNAPSHOT = auto()        # 账户净值快照

    # 系统事件
    SYSTEM_STARTUP = auto()         # 系统启动
    SYSTEM_SHUTDOWN = auto()        # 系统关机
    HEARTBEAT = auto()              # 心跳（定时触发）

    # Phase 3 实时数据层事件
    ORDER_BOOK_SNAPSHOT = auto()    # 订单簿快照（序列一致后发布）
    TRADE_TICK = auto()             # 单笔成交记录
    FEED_HEALTH_CHANGED = auto()    # 数据流健康状态变化（HEALTHY / DEGRADED）
    MICRO_FEATURE_COMPUTED = auto() # 微观结构特征计算完成


# ══════════════════════════════════════════════════════════════
# 二、事件基类与具体事件数据类
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class BaseEvent:
    """
    所有事件的基类。frozen=True 保证不可变性。

    Attributes:
        event_type: 事件类型
        timestamp:  事件发生时间（UTC）
        source:     产生事件的模块名称，用于审计追溯
    """
    event_type: EventType
    timestamp: datetime
    source: str


@dataclass(frozen=True)
class KlineEvent(BaseEvent):
    """K 线收线事件。"""
    symbol: str        # 如 "BTC/USDT"
    timeframe: str     # 如 "1h"
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_closed: bool = True  # True = 已收线，False = 实时推送中


@dataclass(frozen=True)
class SignalEvent(BaseEvent):
    """Alpha 引擎产出的交易信号事件。"""
    symbol: str
    direction: str           # "long" | "short" | "flat"
    strength: float          # [-1.0, 1.0]，正为做多意愿，负为做空
    confidence: float        # [0.0, 1.0]
    strategy_id: str         # 产出此信号的策略 ID
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderRequestEvent(BaseEvent):
    """策略层向执行层申请下单的事件。必须先经过风控层。"""
    symbol: str
    side: str            # "buy" | "sell"
    order_type: str      # "limit" | "market"
    quantity: Decimal
    price: Decimal | None  # None 时为市价单
    strategy_id: str
    request_id: str      # 唯一请求 ID，用于追溯


@dataclass(frozen=True)
class OrderFilledEvent(BaseEvent):
    """订单部分或完全成交的回报事件。"""
    order_id: str
    symbol: str
    side: str
    filled_qty: Decimal
    avg_price: Decimal
    fee: Decimal
    fee_currency: str
    is_partial: bool = False


@dataclass(frozen=True)
class RiskBreachedEvent(BaseEvent):
    """风控拦截事件，携带触发的规则名和原始请求 ID。"""
    rule: str
    request_id: str
    detail: str


@dataclass(frozen=True)
class HeartbeatEvent(BaseEvent):
    """系统心跳事件，由调度器定期发出。"""
    sequence: int


# ── Phase 3 实时数据层事件 ──────────────────────────────────

@dataclass(frozen=True)
class OrderBookSnapshotEvent(BaseEvent):
    """
    订单簿快照事件（序列一致性保证后发布）。

    策略层消费此事件时，gap_status 保证为 OK 或 RECOVERED。
    """
    symbol: str
    exchange: str
    sequence_id: int
    best_bid: float
    best_ask: float
    spread_bps: float
    mid_price: float
    imbalance: float            # ∈ [-1, 1]，正值偏买
    depth_levels: int           # 有效档位数量（bid + ask 合计）
    is_gap_recovered: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TradeTickEvent(BaseEvent):
    """
    单笔成交记录事件（taker 成交方向）。
    """
    symbol: str
    exchange: str
    trade_id: str
    side: str               # "buy" | "sell" | "unknown"
    price: float
    size: float
    notional: float         # price × size
    is_liquidation: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FeedHealthChangedEvent(BaseEvent):
    """
    数据流健康状态变化事件。

    由 SubscriptionManager 在 HEALTHY ↔ DEGRADED 切换时发出。
    上层可据此降级到低频数据，避免使用脏快照。
    """
    exchange: str
    health: str             # "healthy" | "degraded" | "stopped"
    subscribed_symbols: list[str]
    reconnect_count: int
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MicroFeatureComputedEvent(BaseEvent):
    """
    微观结构特征计算完成事件。

    feature_vector 与 feature_names 一一对应，NaN 表示特征缺失。
    """
    symbol: str
    mb_spread_bps: float
    mb_order_imbalance: float
    mb_micro_price: float
    mb_book_pressure_ratio: float
    mb_trade_flow_imbalance: float
    mb_vwap_vs_mid: float
    mb_liq_ratio: float
    mb_spread_tightness: float
    is_book_healthy: bool
    meta: dict[str, Any] = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════
# 三、事件总线（EventBus）
# ══════════════════════════════════════════════════════════════

# 处理器类型定义
SyncHandler = Callable[["BaseEvent"], None]
AsyncHandler = Callable[["BaseEvent"], Coroutine[Any, Any, None]]
Handler = Union[SyncHandler, AsyncHandler]


class EventBus:
    """
    内存事件总线，支持同步与异步消费者混用。

    回测模式：同步调用，时间顺序保证，零 IO 延迟。
    实盘模式：asyncio 驱动，通过 publish_async 异步派发。

    接口:
        subscribe(event_type, handler)  → 注册消费者
        publish(event)                  → 同步发布（回测推荐）
        publish_async(event)            → 异步发布（实盘推荐）
        clear()                         → 清空所有订阅（测试用）
    """

    def __init__(self) -> None:
        self._handlers: Dict[EventType, List[Handler]] = defaultdict(list)

    def subscribe(
        self,
        event_type: EventType,
        handler: Handler,
    ) -> None:
        """注册事件消费者。同一 handler 不能重复注册到同一 event_type。"""
        existing = self._handlers[event_type]
        if handler in existing:
            log.warning("重复订阅被忽略: event_type={} handler={}", event_type, handler)
            return
        existing.append(handler)
        log.debug("已订阅: event_type={} handler={}", event_type, handler.__qualname__)

    def unsubscribe(self, event_type: EventType, handler: Handler) -> None:
        """取消注册指定消费者。"""
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    def publish(self, event: BaseEvent) -> None:
        """
        同步发布事件（适用于回测场景）。

        消费者异常将被捕获并记录，不会中断其他消费者。
        """
        handlers = self._handlers.get(event.event_type, [])
        if not handlers:
            log.trace("事件无订阅者: {}", event.event_type)
            return

        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    # 在同步上下文中运行异步 handler
                    asyncio.get_event_loop().run_until_complete(handler(event))
                else:
                    handler(event)  # type: ignore[call-arg]
            except Exception:  # noqa: BLE001
                log.exception(
                    "事件消费者异常，已跳过: event={} handler={}",
                    event.event_type,
                    handler.__qualname__,
                )

    async def publish_async(self, event: BaseEvent) -> None:
        """
        异步发布事件（适用于实盘场景）。

        所有 handler 通过 asyncio.gather 并发执行，单个异常不影响其他 handler。
        """
        handlers = self._handlers.get(event.event_type, [])
        if not handlers:
            return

        async def _safe_call(h: Handler) -> None:
            try:
                if asyncio.iscoroutinefunction(h):
                    await h(event)
                else:
                    h(event)  # type: ignore[call-arg]
            except Exception:  # noqa: BLE001
                log.exception(
                    "异步事件消费者异常: event={} handler={}",
                    event.event_type,
                    h.__qualname__,
                )

        await asyncio.gather(*[_safe_call(h) for h in handlers])

    def clear(self) -> None:
        """清空所有订阅。仅在测试中使用。"""
        self._handlers.clear()

    @property
    def subscriber_count(self) -> dict[str, int]:
        """返回各事件类型的当前订阅者数量，用于调试。"""
        return {et.name: len(hs) for et, hs in self._handlers.items() if hs}


# ── 全局单例总线（可在模块间共享） ─────────────────────────
# 单例模式用于回测和简单实盘场景；
# 如需多租户或多策略隔离，可在各 App 层实例化独立的 EventBus。
_default_bus = EventBus()


def get_event_bus() -> EventBus:
    """返回全局默认事件总线实例。"""
    return _default_bus
