"""Integration tests for AlphaRuntime runtime contracts and flow."""

from datetime import datetime, timezone
from decimal import Decimal

from core.event import EventType, KlineEvent, OrderRequestEvent
from modules.alpha.contracts import RegimeState, StrategyResult
from modules.alpha.runtime import AlphaRuntime, BaseAlphaAdapter, StrategyRegistry


class DummyStrategy:
    def __init__(self, strategy_id: str, symbol: str = "BTCUSDT") -> None:
        self.strategy_id = strategy_id
        self.symbol = symbol
        self.timeframe = "1h"
        self._bar_count = 0

    def on_kline(self, event: KlineEvent):
        self._bar_count += 1
        return []


def _make_event(symbol: str = "BTCUSDT") -> KlineEvent:
    return KlineEvent(
        event_type=EventType.KLINE_UPDATED,
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        source="test",
        symbol=symbol,
        timeframe="1h",
        open=Decimal("45000"),
        high=Decimal("45100"),
        low=Decimal("44900"),
        close=Decimal("45050"),
        volume=Decimal("100"),
        is_closed=True,
    )


def _make_regime() -> RegimeState:
    return RegimeState(
        bull_prob=0.4,
        bear_prob=0.3,
        sideways_prob=0.3,
        high_vol_prob=0.0,
        confidence=0.7,
        dominant_regime="bull",
    )


def test_strategy_wrapping_and_registration():
    registry = StrategyRegistry()
    dummy = DummyStrategy("MA_TEST")
    wrapped = BaseAlphaAdapter(strategy=dummy)

    registry.register(wrapped)

    retrieved = registry.get_for_symbol("BTCUSDT")
    assert len(retrieved) == 1
    assert retrieved[0].strategy_id == "MA_TEST"


def test_process_kline_event_via_runtime():
    registry = StrategyRegistry()
    runtime = AlphaRuntime(registry=registry, debug_enabled=True)

    registry.register(BaseAlphaAdapter(strategy=DummyStrategy("STRAT1")))

    context, results = runtime.process_bar(
        event=_make_event(),
        latest_prices={"BTCUSDT": 45050.0},
        regime=_make_regime(),
        portfolio_snapshot={"positions": {}, "equity": 10000.0, "entry_prices": {}},
    )

    assert context.symbol == "BTCUSDT"
    assert context.loop_seq == 1
    assert context.trace_id.startswith("BTCUSDT-")
    assert len(results) == 1
    assert results[0].strategy_id == "STRAT1"


def test_collect_order_requests_from_results():
    req = OrderRequestEvent(
        event_type=EventType.SIGNAL_GENERATED,
        timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        source="test",
        symbol="BTCUSDT",
        side="buy",
        order_type="limit",
        quantity=Decimal("0.1"),
        price=Decimal("45000"),
        strategy_id="TEST",
        request_id="req-1",
    )
    result = StrategyResult(
        strategy_id="TEST",
        symbol="BTCUSDT",
        action="BUY",
        order_requests=[req],
    )

    orders = AlphaRuntime.collect_order_requests([result])
    assert len(orders) == 1
    assert orders[0].strategy_id == "TEST"


def test_multiple_strategies_per_symbol():
    registry = StrategyRegistry()
    runtime = AlphaRuntime(registry=registry)

    registry.register(BaseAlphaAdapter(strategy=DummyStrategy("S1")))
    registry.register(BaseAlphaAdapter(strategy=DummyStrategy("S2")))

    _, results = runtime.process_bar(
        event=_make_event(),
        latest_prices={"BTCUSDT": 45050.0},
        regime=_make_regime(),
    )

    ids = {r.strategy_id for r in results}
    assert ids == {"S1", "S2"}
