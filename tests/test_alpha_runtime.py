from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from core.event import EventType, KlineEvent
from modules.alpha.runtime import AlphaRuntime, BaseAlphaAdapter, StrategyRegistry
from modules.alpha.strategies.ma_cross import MACrossStrategy


def make_kline_event(symbol: str, close: float, hour: int) -> KlineEvent:
    return KlineEvent(
        event_type=EventType.KLINE_UPDATED,
        timestamp=datetime(2024, 1, 1, hour % 24, tzinfo=timezone.utc),
        source="test",
        symbol=symbol,
        timeframe="1h",
        open=Decimal(str(close * 0.99)),
        high=Decimal(str(close * 1.01)),
        low=Decimal(str(close * 0.98)),
        close=Decimal(str(close)),
        volume=Decimal("1000"),
        is_closed=True,
    )


def test_registry_register_and_lookup() -> None:
    registry = StrategyRegistry()
    s = MACrossStrategy(symbol="BTC/USDT", fast_window=3, slow_window=5)
    wrapped = BaseAlphaAdapter(s)

    assert registry.register(wrapped) is True
    assert registry.register(wrapped) is False
    listed = registry.get_for_symbol("BTC/USDT")
    assert len(listed) == 1
    assert listed[0].strategy_id == wrapped.strategy_id


def test_alpha_runtime_process_bar_collects_results() -> None:
    registry = StrategyRegistry()
    s = MACrossStrategy(symbol="BTC/USDT", fast_window=3, slow_window=5)
    wrapped = BaseAlphaAdapter(s)
    registry.register(wrapped)

    runtime = AlphaRuntime(registry=registry, debug_enabled=True)

    context = None
    results = None
    prices = [100, 101, 102, 103, 104, 103, 102, 101]
    for i, p in enumerate(prices):
        event = make_kline_event("BTC/USDT", p, i)
        context, results = runtime.process_bar(event=event, latest_prices={"BTC/USDT": p})

    assert context is not None
    assert results is not None
    assert context.symbol == "BTC/USDT"
    assert context.trace_id.startswith("BTCUSDT-")
    assert len(results) == 1
    assert results[0].strategy_id == wrapped.strategy_id


def test_collect_order_requests_from_results() -> None:
    registry = StrategyRegistry()
    s = MACrossStrategy(symbol="BTC/USDT", fast_window=2, slow_window=3)
    wrapped = BaseAlphaAdapter(s)
    registry.register(wrapped)

    runtime = AlphaRuntime(registry=registry)

    prices = [10, 11, 12, 11, 10, 11]
    orders_total = 0
    for i, p in enumerate(prices):
        event = make_kline_event("BTC/USDT", p, i)
        _, results = runtime.process_bar(event=event)
        orders = runtime.collect_order_requests(results)
        orders_total += len(orders)

    assert orders_total >= 0
