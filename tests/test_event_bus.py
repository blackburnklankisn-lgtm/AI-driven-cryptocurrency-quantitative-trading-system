"""
tests/test_event_bus.py — 事件总线单元测试

覆盖项：
- 同步发布与订阅
- 异步发布与订阅
- 消费者异常隔离（不崩溃总线）
- 重复订阅防护
- 取消订阅
- 无订阅者时的静默行为
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from core.event import (
    BaseEvent,
    EventBus,
    EventType,
    KlineEvent,
    SignalEvent,
    get_event_bus,
)


# ── 测试工具 ───────────────────────────────────────────────

def make_kline(symbol: str = "BTC/USDT") -> KlineEvent:
    return KlineEvent(
        event_type=EventType.KLINE_UPDATED,
        timestamp=datetime.now(tz=timezone.utc),
        source="test",
        symbol=symbol,
        timeframe="1h",
        open=Decimal("60000"),
        high=Decimal("61000"),
        low=Decimal("59000"),
        close=Decimal("60500"),
        volume=Decimal("1234.56"),
    )


def make_signal(symbol: str = "BTC/USDT") -> SignalEvent:
    return SignalEvent(
        event_type=EventType.SIGNAL_GENERATED,
        timestamp=datetime.now(tz=timezone.utc),
        source="test_strategy",
        symbol=symbol,
        direction="long",
        strength=0.8,
        confidence=0.75,
        strategy_id="test_ma_cross",
    )


# ══════════════════════════════════════════════════════════════
# 一、同步 EventBus 测试
# ══════════════════════════════════════════════════════════════

class TestSyncEventBus:
    def setup_method(self) -> None:
        self.bus = EventBus()
        self.received: list[BaseEvent] = []

    def _collect(self, event: BaseEvent) -> None:
        self.received.append(event)

    def test_subscribe_and_publish(self) -> None:
        """订阅后发布，消费者应收到事件。"""
        self.bus.subscribe(EventType.KLINE_UPDATED, self._collect)
        evt = make_kline()
        self.bus.publish(evt)
        assert len(self.received) == 1
        assert self.received[0] is evt

    def test_multiple_subscribers(self) -> None:
        """同一事件多个订阅者均应收到。"""
        received_b: list[BaseEvent] = []
        self.bus.subscribe(EventType.KLINE_UPDATED, self._collect)
        self.bus.subscribe(EventType.KLINE_UPDATED, received_b.append)

        self.bus.publish(make_kline())
        assert len(self.received) == 1
        assert len(received_b) == 1

    def test_different_event_types_isolated(self) -> None:
        """不同事件类型的订阅者不互相收到对方的事件。"""
        self.bus.subscribe(EventType.KLINE_UPDATED, self._collect)
        # 发布一个 SIGNAL_GENERATED，订阅者不应收到
        self.bus.publish(make_signal())
        assert len(self.received) == 0

    def test_consumer_exception_does_not_crash_bus(self) -> None:
        """消费者抛出异常，总线继续处理其他消费者。"""
        second_received: list[BaseEvent] = []

        def bad_handler(event: BaseEvent) -> None:  # noqa: ARG001
            raise RuntimeError("消费者故意抛出异常")

        self.bus.subscribe(EventType.KLINE_UPDATED, bad_handler)
        self.bus.subscribe(EventType.KLINE_UPDATED, second_received.append)

        self.bus.publish(make_kline())
        # bad_handler 失败，但 second_received 仍然应该收到
        assert len(second_received) == 1

    def test_duplicate_subscription_prevented(self) -> None:
        """同一 handler 重复注册到同一事件类型，应只被调用一次。"""
        self.bus.subscribe(EventType.KLINE_UPDATED, self._collect)
        self.bus.subscribe(EventType.KLINE_UPDATED, self._collect)  # 重复注册

        self.bus.publish(make_kline())
        assert len(self.received) == 1

    def test_unsubscribe(self) -> None:
        """取消订阅后不再收到事件。"""
        self.bus.subscribe(EventType.KLINE_UPDATED, self._collect)
        self.bus.unsubscribe(EventType.KLINE_UPDATED, self._collect)
        self.bus.publish(make_kline())
        assert len(self.received) == 0

    def test_no_subscribers_silent(self) -> None:
        """无订阅者时发布事件不抛出异常。"""
        self.bus.publish(make_kline())  # 不应抛出

    def test_event_immutability(self) -> None:
        """事件对象为 frozen dataclass，尝试修改应抛出 FrozenInstanceError。"""
        evt = make_kline()
        with pytest.raises((AttributeError, TypeError)):
            evt.symbol = "ETH/USDT"  # type: ignore[misc]

    def test_subscriber_count(self) -> None:
        """订阅者数量报告应准确。"""
        self.bus.subscribe(EventType.KLINE_UPDATED, self._collect)
        count = self.bus.subscriber_count
        assert count.get("KLINE_UPDATED") == 1

    def test_clear(self) -> None:
        """clear() 之后所有订阅应被清空。"""
        self.bus.subscribe(EventType.KLINE_UPDATED, self._collect)
        self.bus.clear()
        self.bus.publish(make_kline())
        assert len(self.received) == 0


# ══════════════════════════════════════════════════════════════
# 二、异步 EventBus 测试
# ══════════════════════════════════════════════════════════════

class TestAsyncEventBus:
    def setup_method(self) -> None:
        self.bus = EventBus()
        self.received: list[BaseEvent] = []

    async def _async_collect(self, event: BaseEvent) -> None:
        await asyncio.sleep(0)  # 确认真正经过 await 调度
        self.received.append(event)

    @pytest.mark.asyncio
    async def test_async_publish(self) -> None:
        """异步发布，异步消费者应收到事件。"""
        self.bus.subscribe(EventType.SIGNAL_GENERATED, self._async_collect)
        await self.bus.publish_async(make_signal())
        assert len(self.received) == 1

    @pytest.mark.asyncio
    async def test_async_consumer_exception_isolated(self) -> None:
        """异步消费者异常不阻塞其他消费者。"""
        second_received: list[BaseEvent] = []

        async def bad_async_handler(event: BaseEvent) -> None:  # noqa: ARG001
            raise ValueError("异步消费者故意失败")

        async def good_handler(event: BaseEvent) -> None:
            second_received.append(event)

        self.bus.subscribe(EventType.SIGNAL_GENERATED, bad_async_handler)
        self.bus.subscribe(EventType.SIGNAL_GENERATED, good_handler)

        await self.bus.publish_async(make_signal())
        assert len(second_received) == 1


# ══════════════════════════════════════════════════════════════
# 三、全局单例总线测试
# ══════════════════════════════════════════════════════════════

def test_global_bus_singleton() -> None:
    """get_event_bus() 每次返回同一实例。"""
    bus1 = get_event_bus()
    bus2 = get_event_bus()
    assert bus1 is bus2
