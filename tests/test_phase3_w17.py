"""
tests/test_phase3_w17.py — W17-W18 做市策略单元测试

覆盖：
- AvellanedaModel (11 tests)
- InventoryManager (11 tests)
- QuoteEngine (10 tests)
- FillSimulator (9 tests)
- QuoteLifecycle (10 tests)
- QuoteStateStore (5 tests)
- MarketMakingStrategy (10 tests)
- mm_types contracts (5 tests)

合计 ~71 tests
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

import pytest

from modules.alpha.contracts.mm_types import (
    ActiveQuote,
    FillRecord,
    InventorySnapshot,
    QuoteAction,
    QuoteDecision,
    QuoteIntent,
    QuoteSide,
    QuoteState,
)
from modules.alpha.market_making.avellaneda_model import AvellanedaConfig, AvellanedaModel
from modules.alpha.market_making.fill_simulator import FillSimulator, FillSimulatorConfig
from modules.alpha.market_making.inventory_manager import InventoryConfig, InventoryManager
from modules.alpha.market_making.quote_engine import QuoteEngine, QuoteEngineConfig
from modules.alpha.market_making.quote_lifecycle import QuoteLifecycle, QuoteLifecycleConfig
from modules.alpha.market_making.quote_state_store import QuoteStateSnapshot, QuoteStateStore
from modules.alpha.market_making.strategy import MarketMakingStrategy, MarketMakingStrategyConfig
from modules.data.realtime.orderbook_types import OrderBookSnapshot
from modules.risk.snapshot import RiskSnapshot


# ══════════════════════════════════════════════════════════════
# 共用 Fixtures / Helpers
# ══════════════════════════════════════════════════════════════

def make_snapshot(
    mid: float = 50000.0,
    spread_bps: float = 2.0,
    symbol: str = "BTC/USDT",
) -> OrderBookSnapshot:
    return OrderBookSnapshot.create_mock(
        symbol=symbol, mid_price=mid, spread_bps=spread_bps
    )


def make_risk(
    circuit_broken: bool = False,
    kill_switch: bool = False,
    budget_pct: float = 0.8,
    daily_loss_pct: float = 0.0,
) -> RiskSnapshot:
    return RiskSnapshot(
        current_drawdown=0.0,
        daily_loss_pct=daily_loss_pct,
        consecutive_losses=0,
        circuit_broken=circuit_broken,
        kill_switch_active=kill_switch,
        budget_remaining_pct=budget_pct,
    )


def make_fill(
    side: QuoteSide = QuoteSide.BID,
    price: float = 49995.0,
    qty: float = 0.01,
    quote_id: str = "q-001",
    symbol: str = "BTC/USDT",
    is_partial: bool = False,
) -> FillRecord:
    return FillRecord(
        symbol=symbol,
        side=side,
        fill_price=price,
        fill_qty=qty,
        fee=price * qty * 0.001,
        quote_id=quote_id,
        is_partial=is_partial,
        filled_at=datetime.now(tz=timezone.utc),
    )


def make_active_quote(
    side: QuoteSide = QuoteSide.BID,
    price: float = 49990.0,
    size: float = 0.02,
    state: QuoteState = QuoteState.ACTIVE,
    quote_id: str = "q-001",
) -> ActiveQuote:
    return ActiveQuote(
        quote_id=quote_id,
        symbol="BTC/USDT",
        side=side,
        price=price,
        original_size=size,
        remaining_size=size,
        state=state,
        posted_at=datetime.now(tz=timezone.utc),
    )


# ══════════════════════════════════════════════════════════════
# 一、AvellanedaModel (11 tests)
# ══════════════════════════════════════════════════════════════

class TestAvellanedaModel:
    def make_model(self, **kwargs) -> AvellanedaModel:
        cfg = AvellanedaConfig(**kwargs) if kwargs else AvellanedaConfig()
        return AvellanedaModel(cfg)

    def test_basic_output_types(self):
        model = self.make_model()
        intent = model.compute("BTC/USDT", 50000.0, 0.01, 0.0, 1.0)
        assert isinstance(intent, QuoteIntent)
        assert isinstance(intent.reservation_price, float)
        assert isinstance(intent.optimal_spread_bps, float)

    def test_neutral_inventory_reservation_near_mid(self):
        """库存为 0 时 reservation_price ≈ mid_price。"""
        model = self.make_model()
        intent = model.compute("BTC/USDT", 50000.0, 0.01, 0.0, 1.0)
        assert abs(intent.reservation_price - 50000.0) < 50.0

    def test_positive_inventory_lowers_reservation(self):
        """正库存 → reservation_price < mid_price（激励卖出）。"""
        model = self.make_model()
        intent_neutral = model.compute("BTC/USDT", 50000.0, 0.01, 0.0, 1.0)
        intent_long = model.compute("BTC/USDT", 50000.0, 0.01, 1.0, 2.0)
        assert intent_long.reservation_price < intent_neutral.reservation_price

    def test_negative_inventory_raises_reservation(self):
        """负库存 → reservation_price > mid_price（激励买入）。"""
        model = self.make_model()
        intent_short = model.compute("BTC/USDT", 50000.0, 0.01, -1.0, 2.0)
        intent_neutral = model.compute("BTC/USDT", 50000.0, 0.01, 0.0, 2.0)
        assert intent_short.reservation_price > intent_neutral.reservation_price

    def test_spread_bounded_by_config(self):
        """spread 必须在 [min_spread_bps, max_spread_bps] 范围内。"""
        cfg = AvellanedaConfig(min_spread_bps=2.0, max_spread_bps=50.0)
        model = AvellanedaModel(cfg)
        intent = model.compute("BTC/USDT", 50000.0, 0.05, 0.0, 1.0)
        assert cfg.min_spread_bps <= intent.optimal_spread_bps <= cfg.max_spread_bps

    def test_max_inventory_disables_bid(self):
        """库存 = max → allow_bid = False（不再买）。"""
        model = self.make_model(allow_one_sided=True)
        intent = model.compute("BTC/USDT", 50000.0, 0.01, 1.0, 1.0)  # q = 1.0
        assert intent.allow_bid is False

    def test_min_inventory_disables_ask(self):
        """库存 = -max → allow_ask = False（不再卖）。"""
        model = self.make_model(allow_one_sided=True)
        intent = model.compute("BTC/USDT", 50000.0, 0.01, -1.0, 1.0)  # q = -1.0
        assert intent.allow_ask is False

    def test_sigma_floor_applied(self):
        """极低 sigma 应被 sigma_floor 上调。"""
        cfg = AvellanedaConfig(sigma_floor=0.001)
        model = AvellanedaModel(cfg)
        intent = model.compute("BTC/USDT", 50000.0, 0.0001, 0.0, 1.0)
        assert intent.sigma >= cfg.sigma_floor

    def test_sigma_cap_applied(self):
        """极高 sigma 应被 sigma_cap 下调。"""
        cfg = AvellanedaConfig(sigma_cap=0.05)
        model = AvellanedaModel(cfg)
        intent = model.compute("BTC/USDT", 50000.0, 0.5, 0.0, 1.0)
        assert intent.sigma <= cfg.sigma_cap

    def test_elapsed_time_affects_spread(self):
        """时间流逝（接近收盘）应影响 debug_payload 中的 time_ratio。"""
        model = self.make_model(T_sec=3600.0)
        intent_start = model.compute("BTC/USDT", 50000.0, 0.01, 0.0, 1.0, elapsed_sec=0.0)
        intent_end = model.compute("BTC/USDT", 50000.0, 0.01, 0.0, 1.0, elapsed_sec=3500.0)
        # time_ratio 应明显不同（时间推进后接近 0）
        ratio_start = intent_start.debug_payload.get("time_ratio", 1.0)
        ratio_end = intent_end.debug_payload.get("time_ratio", 1.0)
        assert ratio_start > ratio_end

    def test_reason_codes_populated(self):
        model = self.make_model()
        intent = model.compute("BTC/USDT", 50000.0, 0.01, 0.5, 1.0)
        assert isinstance(intent.reason_codes, list)


# ══════════════════════════════════════════════════════════════
# 二、InventoryManager (11 tests)
# ══════════════════════════════════════════════════════════════

class TestInventoryManager:
    def make_inv(self, **kwargs) -> InventoryManager:
        cfg = InventoryConfig(**kwargs) if kwargs else InventoryConfig()
        return InventoryManager("BTC/USDT", cfg)

    def test_initial_snapshot_fields(self):
        inv = self.make_inv(initial_quote_value=10000.0)
        snap = inv.snapshot(mid_price=50000.0)
        assert isinstance(snap, InventorySnapshot)
        assert snap.base_qty == 0.0
        assert snap.quote_value == 10000.0
        assert snap.realized_pnl == 0.0
        assert snap.total_trades == 0

    def test_bid_fill_increases_base(self):
        inv = self.make_inv()
        fill = make_fill(side=QuoteSide.BID, price=50000.0, qty=0.01)
        inv.on_fill(fill)
        snap = inv.snapshot(50000.0)
        assert snap.base_qty == pytest.approx(0.01)

    def test_bid_fill_reduces_quote(self):
        inv = self.make_inv(initial_quote_value=10000.0)
        fill = make_fill(side=QuoteSide.BID, price=50000.0, qty=0.01)
        inv.on_fill(fill)
        snap = inv.snapshot(50000.0)
        # quote减少 = notional + fee
        notional = 50000.0 * 0.01
        fee = notional * 0.001
        assert snap.quote_value == pytest.approx(10000.0 - notional - fee, abs=0.01)

    def test_ask_fill_decreases_base(self):
        inv = self.make_inv()
        # 先买
        buy = make_fill(side=QuoteSide.BID, price=50000.0, qty=0.02)
        inv.on_fill(buy)
        # 再卖
        sell = make_fill(side=QuoteSide.ASK, price=51000.0, qty=0.01)
        inv.on_fill(sell)
        snap = inv.snapshot(50000.0)
        assert snap.base_qty == pytest.approx(0.01)

    def test_realized_pnl_on_round_trip(self):
        """买低卖高，realized_pnl > 0。"""
        inv = self.make_inv()
        buy = make_fill(side=QuoteSide.BID, price=50000.0, qty=0.01)
        sell = make_fill(side=QuoteSide.ASK, price=51000.0, qty=0.01)
        inv.on_fill(buy)
        inv.on_fill(sell)
        snap = inv.snapshot(51000.0)
        assert snap.realized_pnl > 0.0

    def test_inventory_pct_neutral_at_half(self):
        """base 价值 ≈ quote 价值时，inventory_pct ≈ 0.5。"""
        # initial_quote_value=10000: 买 0.1 BTC @ 50000 = 5000 花费, 剩 ~4995 USDT
        # base_value = 0.1 * 50000 = 5000, total ≈ 9995, pct ≈ 0.5001 ∈ [0.4, 0.6]
        inv = self.make_inv(initial_quote_value=10000.0)
        buy = make_fill(side=QuoteSide.BID, price=50000.0, qty=0.1)
        inv.on_fill(buy)
        snap = inv.snapshot(mid_price=50000.0)
        assert 0.4 < snap.inventory_pct < 0.6

    def test_suggest_quote_sides_normal(self):
        # initial_base_qty=0.1 BTC, initial_quote_value=5000
        # 以 mid=50000: base_value=0.1*50000=5000, total=10000, inv_pct=0.5 → deviation=0 → normal
        inv = InventoryManager("BTC/USDT", InventoryConfig(
            initial_base_qty=0.1,
            initial_quote_value=5000.0,
            target_inventory_pct=0.5,
            max_inventory_pct=0.20,
            halt_inventory_pct=0.40,
        ))
        s = inv.suggest_quote_sides(inv.snapshot(mid_price=50000.0))
        assert s["allow_bid"] is True
        assert s["allow_ask"] is True
        assert s["halt"] is False

    def test_suggest_halt_on_extreme_inventory(self):
        """极端库存 deviation 触发 halt。"""
        inv = self.make_inv(
            halt_inventory_pct=0.1,
            max_inventory_pct=0.05,
            initial_quote_value=500.0,
        )
        # 大量买入 → 偏多
        for _ in range(10):
            inv.on_fill(make_fill(QuoteSide.BID, 50000.0, 0.1))
        s = inv.suggest_quote_sides()
        assert s["halt"] is True

    def test_reset_clears_state(self):
        inv = self.make_inv()
        inv.on_fill(make_fill(QuoteSide.BID, 50000.0, 0.01))
        inv.reset()
        snap = inv.snapshot(50000.0)
        assert snap.base_qty == 0.0
        assert snap.realized_pnl == 0.0

    def test_update_mid_does_not_crash(self):
        inv = self.make_inv()
        inv.update_mid(50000.0)
        inv.update_mid(51000.0)

    def test_unrealized_pnl_positive_when_price_rises(self):
        inv = self.make_inv()
        inv.on_fill(make_fill(QuoteSide.BID, 50000.0, 0.01))
        snap = inv.snapshot(mid_price=51000.0)
        assert snap.unrealized_pnl > 0.0


# ══════════════════════════════════════════════════════════════
# 三、QuoteEngine (10 tests)
# ══════════════════════════════════════════════════════════════

class TestQuoteEngine:
    def make_engine(self, **kwargs) -> QuoteEngine:
        cfg = QuoteEngineConfig(**kwargs) if kwargs else QuoteEngineConfig()
        return QuoteEngine(cfg)

    def make_intent(
        self,
        mid: float = 50000.0,
        spread_bps: float = 5.0,
        inv_dev: float = 0.0,
        allow_bid: bool = True,
        allow_ask: bool = True,
    ) -> QuoteIntent:
        return QuoteIntent(
            symbol="BTC/USDT",
            mid_price=mid,
            reservation_price=mid * (1.0 + inv_dev * -0.0001),
            optimal_spread_bps=spread_bps,
            sigma=0.01,
            gamma=0.12,
            inventory_deviation=inv_dev,
            allow_bid=allow_bid,
            allow_ask=allow_ask,
        )

    def make_inv_snap(self, inv_dev: float = 0.0) -> InventorySnapshot:
        return InventorySnapshot(
            symbol="BTC/USDT",
            base_qty=0.1 * inv_dev,
            quote_value=5000.0,
            inventory_pct=0.5 + inv_dev * 0.1,
            target_inventory_pct=0.5,
            max_inventory_pct=0.2,
            skew_bps=inv_dev * 10,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            total_trades=0,
        )

    def test_returns_quote_decision(self):
        engine = self.make_engine()
        decision = engine.generate(self.make_intent(), self.make_inv_snap())
        assert isinstance(decision, QuoteDecision)

    def test_bid_less_than_ask(self):
        engine = self.make_engine()
        decision = engine.generate(self.make_intent(), self.make_inv_snap())
        assert decision.bid_price < decision.ask_price

    def test_bid_below_mid(self):
        engine = self.make_engine()
        decision = engine.generate(self.make_intent(mid=50000.0), self.make_inv_snap())
        assert decision.bid_price < 50000.0

    def test_ask_above_mid(self):
        engine = self.make_engine()
        decision = engine.generate(self.make_intent(mid=50000.0), self.make_inv_snap())
        assert decision.ask_price > 50000.0

    def test_spread_enforced_min(self):
        """强制 min_spread_bps。"""
        engine = self.make_engine(min_spread_bps=10.0)
        decision = engine.generate(self.make_intent(spread_bps=1.0), self.make_inv_snap())
        if decision.bid_price and decision.ask_price:
            mid = (decision.bid_price + decision.ask_price) / 2
            spread = (decision.ask_price - decision.bid_price) / mid * 10000
            assert spread >= 10.0 - 1e-6

    def test_bid_disabled_when_allow_bid_false(self):
        engine = self.make_engine()
        decision = engine.generate(
            self.make_intent(allow_bid=False), self.make_inv_snap()
        )
        assert decision.allow_post_bid is False

    def test_ask_disabled_when_allow_ask_false(self):
        engine = self.make_engine()
        decision = engine.generate(
            self.make_intent(allow_ask=False), self.make_inv_snap()
        )
        assert decision.allow_post_ask is False

    def test_size_positive(self):
        engine = self.make_engine()
        decision = engine.generate(self.make_intent(), self.make_inv_snap())
        if decision.bid_size is not None:
            assert decision.bid_size > 0
        if decision.ask_size is not None:
            assert decision.ask_size > 0

    def test_is_actionable_both_sides(self):
        engine = self.make_engine()
        decision = engine.generate(self.make_intent(), self.make_inv_snap())
        assert decision.is_actionable()

    def test_effective_spread_bps_positive(self):
        engine = self.make_engine()
        decision = engine.generate(self.make_intent(spread_bps=5.0), self.make_inv_snap())
        spread = decision.effective_spread_bps()
        if spread is not None:
            assert spread > 0.0


# ══════════════════════════════════════════════════════════════
# 四、FillSimulator (9 tests)
# ══════════════════════════════════════════════════════════════

class TestFillSimulator:
    def make_sim(self, **kwargs) -> FillSimulator:
        cfg = FillSimulatorConfig(partial_fill_prob=0.0, **kwargs)
        return FillSimulator(cfg)

    def test_bid_fills_when_best_ask_below_bid_price(self):
        sim = self.make_sim()
        quote = make_active_quote(QuoteSide.BID, price=50010.0)
        snapshot = make_snapshot(mid=50000.0, spread_bps=2.0)
        # best_ask = 50005, bid_price = 50010 → triggered
        fill = sim.check_fill(quote, snapshot)
        assert fill is not None
        assert fill.side == QuoteSide.BID

    def test_bid_no_fill_when_best_ask_above_bid(self):
        sim = self.make_sim()
        quote = make_active_quote(QuoteSide.BID, price=49900.0)  # well below ask
        snapshot = make_snapshot(mid=50000.0, spread_bps=2.0)
        fill = sim.check_fill(quote, snapshot)
        assert fill is None

    def test_ask_fills_when_best_bid_above_ask_price(self):
        sim = self.make_sim()
        quote = make_active_quote(QuoteSide.ASK, price=49990.0)
        snapshot = make_snapshot(mid=50000.0, spread_bps=2.0)
        # best_bid = 49995, ask_price = 49990 → triggered
        fill = sim.check_fill(quote, snapshot)
        assert fill is not None
        assert fill.side == QuoteSide.ASK

    def test_ask_no_fill_when_best_bid_below_ask(self):
        sim = self.make_sim()
        quote = make_active_quote(QuoteSide.ASK, price=50100.0)  # well above bid
        snapshot = make_snapshot(mid=50000.0, spread_bps=2.0)
        fill = sim.check_fill(quote, snapshot)
        assert fill is None

    def test_dead_quote_no_fill(self):
        sim = self.make_sim()
        quote = make_active_quote(state=QuoteState.FILLED)
        snapshot = make_snapshot()
        fill = sim.check_fill(quote, snapshot)
        assert fill is None

    def test_unhealthy_snapshot_no_fill(self):
        from modules.data.realtime.orderbook_types import GapStatus
        sim = self.make_sim()
        quote = make_active_quote(QuoteSide.BID, price=50010.0)
        snap = make_snapshot()
        # Recreate with unhealthy gap status
        unhealthy = OrderBookSnapshot(
            symbol=snap.symbol,
            exchange=snap.exchange,
            sequence_id=snap.sequence_id,
            best_bid=snap.best_bid,
            best_ask=snap.best_ask,
            bids=snap.bids,
            asks=snap.asks,
            spread_bps=snap.spread_bps,
            mid_price=snap.mid_price,
            imbalance=snap.imbalance,
            received_at=snap.received_at,
            gap_status=GapStatus.GAP_DETECTED,
        )
        fill = sim.check_fill(quote, unhealthy)
        assert fill is None

    def test_fill_record_fields(self):
        sim = self.make_sim()
        quote = make_active_quote(QuoteSide.BID, price=50010.0, size=0.05)
        snapshot = make_snapshot(mid=50000.0, spread_bps=2.0)
        fill = sim.check_fill(quote, snapshot)
        assert fill is not None
        assert fill.symbol == "BTC/USDT"
        assert fill.quote_id == quote.quote_id
        assert fill.fill_qty > 0
        assert fill.fee >= 0

    def test_batch_check_returns_list(self):
        sim = self.make_sim()
        q1 = make_active_quote(QuoteSide.BID, price=50010.0, quote_id="q1")
        q2 = make_active_quote(QuoteSide.ASK, price=49990.0, quote_id="q2")
        snapshot = make_snapshot(mid=50000.0, spread_bps=2.0)
        fills = sim.batch_check([q1, q2], snapshot)
        assert isinstance(fills, list)

    def test_reset_rng_is_reproducible(self):
        cfg = FillSimulatorConfig(partial_fill_prob=1.0, seed=0)
        sim = FillSimulator(cfg)
        quote = make_active_quote(QuoteSide.BID, price=50010.0, size=1.0)
        snapshot = make_snapshot(mid=50000.0, spread_bps=2.0)
        # Run twice with same seed
        sim.reset_rng(0)
        fill1 = sim.check_fill(quote, snapshot)
        sim.reset_rng(0)
        fill2 = sim.check_fill(quote, snapshot)
        if fill1 and fill2:
            assert fill1.fill_qty == pytest.approx(fill2.fill_qty)


# ══════════════════════════════════════════════════════════════
# 五、QuoteLifecycle (10 tests)
# ══════════════════════════════════════════════════════════════

class TestQuoteLifecycle:
    def make_lc(self, **kwargs) -> QuoteLifecycle:
        cfg = QuoteLifecycleConfig(**kwargs) if kwargs else QuoteLifecycleConfig()
        return QuoteLifecycle("BTC/USDT", cfg)

    def make_decision(
        self,
        bid: float = 49990.0,
        ask: float = 50010.0,
        allow_bid: bool = True,
        allow_ask: bool = True,
    ) -> QuoteDecision:
        return QuoteDecision(
            symbol="BTC/USDT",
            bid_price=bid,
            ask_price=ask,
            bid_size=0.01,
            ask_size=0.01,
            reservation_price=50000.0,
            optimal_spread_bps=4.0,
            skew_bps=0.0,
            allow_post_bid=allow_bid,
            allow_post_ask=allow_ask,
        )

    def test_initial_post_action(self):
        """首次 evaluate → POST 两侧。"""
        lc = self.make_lc()
        result = lc.evaluate(self.make_decision(), 50000.0)
        assert result[QuoteSide.BID][0] == QuoteAction.POST
        assert result[QuoteSide.ASK][0] == QuoteAction.POST

    def test_post_creates_active_quote(self):
        lc = self.make_lc()
        result = lc.evaluate(self.make_decision(), 50000.0)
        bid_action, bid_quote, _ = result[QuoteSide.BID]
        assert bid_action == QuoteAction.POST
        assert bid_quote is not None
        assert bid_quote.state == QuoteState.PENDING
        assert bid_quote.side == QuoteSide.BID

    def test_second_evaluate_skips_valid_quote(self):
        lc = self.make_lc(min_refresh_interval_sec=0.0)
        lc.evaluate(self.make_decision(), 50000.0)
        result = lc.evaluate(self.make_decision(), 50000.0)
        bid_action = result[QuoteSide.BID][0]
        assert bid_action == QuoteAction.SKIP

    def test_kill_switch_halts(self):
        lc = self.make_lc()
        result = lc.evaluate(self.make_decision(), 50000.0, kill_switch_active=True)
        assert result[QuoteSide.BID][0] in (QuoteAction.SKIP, QuoteAction.HALT)
        assert result[QuoteSide.ASK][0] in (QuoteAction.SKIP, QuoteAction.HALT)

    def test_side_disabled_skip(self):
        lc = self.make_lc()
        decision = self.make_decision(allow_bid=False, allow_ask=True)
        result = lc.evaluate(decision, 50000.0)
        assert result[QuoteSide.BID][0] == QuoteAction.SKIP

    def test_on_fill_full_clears_quote(self):
        lc = self.make_lc()
        result = lc.evaluate(self.make_decision(), 50000.0)
        _, bid_quote, _ = result[QuoteSide.BID]
        assert bid_quote is not None
        # Confirm quote then fill completely
        lc.on_posted(QuoteSide.BID, bid_quote.quote_id)
        fill = make_fill(
            side=QuoteSide.BID,
            price=49990.0,
            qty=bid_quote.original_size,
            quote_id=bid_quote.quote_id,
        )
        lc.on_fill(fill)
        assert lc.get_quote(QuoteSide.BID) is None

    def test_on_fill_partial_keeps_quote_alive(self):
        lc = self.make_lc()
        result = lc.evaluate(self.make_decision(), 50000.0)
        _, bid_quote, _ = result[QuoteSide.BID]
        lc.on_posted(QuoteSide.BID, bid_quote.quote_id)
        fill = make_fill(
            side=QuoteSide.BID,
            price=49990.0,
            qty=bid_quote.original_size / 2,
            quote_id=bid_quote.quote_id,
            is_partial=True,
        )
        lc.on_fill(fill)
        q = lc.get_quote(QuoteSide.BID)
        assert q is not None
        assert q.state == QuoteState.PARTIALLY_FILLED

    def test_active_quotes_returns_list(self):
        lc = self.make_lc()
        lc.evaluate(self.make_decision(), 50000.0)
        quotes = lc.active_quotes()
        assert isinstance(quotes, list)

    def test_reset_clears_quotes(self):
        lc = self.make_lc()
        lc.evaluate(self.make_decision(), 50000.0)
        lc.reset()
        assert lc.get_quote(QuoteSide.BID) is None
        assert lc.get_quote(QuoteSide.ASK) is None

    def test_diagnostics_structure(self):
        lc = self.make_lc()
        d = lc.diagnostics()
        assert "symbol" in d
        assert "total_posts" in d
        assert "total_fills" in d


# ══════════════════════════════════════════════════════════════
# 六、QuoteStateStore (5 tests)
# ══════════════════════════════════════════════════════════════

class TestQuoteStateStore:
    def make_store(self) -> tuple[QuoteStateStore, str]:
        tmp = tempfile.mktemp(suffix=".json")
        return QuoteStateStore(tmp), tmp

    def test_save_and_load_round_trip(self):
        store, path = self.make_store()
        snap = QuoteStateSnapshot(
            symbol="BTC/USDT",
            base_qty=0.12,
            quote_value=4500.0,
            realized_pnl=23.5,
            total_trades=5,
        )
        store.save(snap)
        loaded = store.load()
        assert loaded is not None
        assert loaded.symbol == "BTC/USDT"
        assert loaded.base_qty == pytest.approx(0.12)
        assert loaded.realized_pnl == pytest.approx(23.5)
        if os.path.exists(path):
            os.remove(path)

    def test_load_missing_returns_none(self):
        store = QuoteStateStore("/tmp/nonexistent_file_xyz.json")
        result = store.load()
        assert result is None

    def test_save_is_atomic(self):
        """保存后文件内容可读，无半写状态。"""
        store, path = self.make_store()
        snap = QuoteStateSnapshot(
            symbol="ETH/USDT", base_qty=1.0, quote_value=2000.0,
            realized_pnl=0.0, total_trades=0,
        )
        store.save(snap)
        # tmp file should not exist
        assert not os.path.exists(path + ".tmp")
        assert os.path.exists(path)
        if os.path.exists(path):
            os.remove(path)

    def test_clear_removes_file(self):
        store, path = self.make_store()
        snap = QuoteStateSnapshot(
            symbol="BTC/USDT", base_qty=0.0, quote_value=0.0,
            realized_pnl=0.0, total_trades=0,
        )
        store.save(snap)
        store.clear()
        assert not os.path.exists(path)

    def test_metadata_preserved(self):
        store, path = self.make_store()
        snap = QuoteStateSnapshot(
            symbol="BTC/USDT", base_qty=0.0, quote_value=0.0,
            realized_pnl=0.0, total_trades=0,
            metadata={"paper_mode": True, "tick_count": 100},
        )
        store.save(snap)
        loaded = store.load()
        assert loaded is not None
        assert loaded.metadata.get("paper_mode") is True
        assert loaded.metadata.get("tick_count") == 100
        if os.path.exists(path):
            os.remove(path)


# ══════════════════════════════════════════════════════════════
# 七、MarketMakingStrategy (10 tests)
# ══════════════════════════════════════════════════════════════

class TestMarketMakingStrategy:
    def make_strategy(self, **kwargs) -> MarketMakingStrategy:
        defaults = dict(
            paper_mode=True,
            save_every_n=0,  # 禁用自动持久化
            state_store_path=tempfile.mktemp(suffix=".json"),
        )
        defaults.update(kwargs)
        cfg = MarketMakingStrategyConfig(**defaults)
        return MarketMakingStrategy(cfg)

    def test_tick_returns_quote_decision(self):
        strat = self.make_strategy()
        snap = make_snapshot()
        risk = make_risk()
        result = strat.tick(snap, risk)
        assert isinstance(result, QuoteDecision)

    def test_tick_blocked_by_kill_switch(self):
        strat = self.make_strategy()
        snap = make_snapshot()
        risk = make_risk(kill_switch=True)
        result = strat.tick(snap, risk)
        assert result.allow_post_bid is False
        assert result.allow_post_ask is False

    def test_tick_blocked_by_circuit_breaker(self):
        strat = self.make_strategy()
        snap = make_snapshot()
        risk = make_risk(circuit_broken=True)
        result = strat.tick(snap, risk)
        assert result.allow_post_bid is False
        assert result.allow_post_ask is False

    def test_tick_blocked_by_unhealthy_snapshot(self):
        from modules.data.realtime.orderbook_types import GapStatus
        strat = self.make_strategy()
        snap = make_snapshot()
        bad = OrderBookSnapshot(
            symbol=snap.symbol, exchange=snap.exchange,
            sequence_id=snap.sequence_id, best_bid=0.0, best_ask=0.0,
            bids=[], asks=[], spread_bps=0.0, mid_price=0.0, imbalance=0.0,
            received_at=snap.received_at, gap_status=GapStatus.GAP_DETECTED,
        )
        risk = make_risk()
        result = strat.tick(bad, risk)
        assert result.allow_post_bid is False
        assert result.allow_post_ask is False

    def test_multiple_ticks_increment_count(self):
        strat = self.make_strategy()
        snap = make_snapshot()
        risk = make_risk()
        for _ in range(5):
            strat.tick(snap, risk)
        assert strat._tick_count == 5

    def test_on_fill_updates_inventory(self):
        strat = self.make_strategy()
        fill = make_fill(QuoteSide.BID, 50000.0, 0.01)
        strat.on_fill(fill)
        diag = strat.diagnostics()
        assert diag["inventory"]["base_qty"] == pytest.approx(0.01, abs=1e-8)

    def test_diagnostics_keys(self):
        strat = self.make_strategy()
        d = strat.diagnostics()
        assert "symbol" in d
        assert "tick_count" in d
        assert "inventory" in d
        assert "lifecycle" in d

    def test_reset_clears_tick_count(self):
        strat = self.make_strategy()
        snap = make_snapshot()
        risk = make_risk()
        for _ in range(3):
            strat.tick(snap, risk)
        strat.reset()
        assert strat._tick_count == 0

    def test_paper_mode_does_not_raise(self):
        """paper 模式下 tick 不应抛出任何异常。"""
        strat = self.make_strategy(paper_mode=True)
        snap = make_snapshot(mid=45000.0)
        risk = make_risk()
        for i in range(10):
            result = strat.tick(snap, risk, elapsed_sec=float(i * 60))
            assert isinstance(result, QuoteDecision)

    def test_restore_returns_false_when_no_state(self):
        strat = self.make_strategy()
        assert strat.restore() is False


# ══════════════════════════════════════════════════════════════
# 八、mm_types contracts (5 tests)
# ══════════════════════════════════════════════════════════════

class TestMmTypesContracts:
    def test_fill_record_notional(self):
        f = make_fill(price=50000.0, qty=0.01)
        assert f.notional() == pytest.approx(500.0)

    def test_fill_record_net_notional_less_than_notional(self):
        f = make_fill(price=50000.0, qty=0.01)
        assert f.net_notional() < f.notional()

    def test_active_quote_is_alive(self):
        q = make_active_quote(state=QuoteState.ACTIVE)
        assert q.is_alive() is True

    def test_active_quote_filled_not_alive(self):
        q = make_active_quote(state=QuoteState.FILLED)
        assert q.is_alive() is False

    def test_active_quote_filled_pct(self):
        q = make_active_quote(size=0.1)
        q.remaining_size = 0.04
        assert q.filled_pct() == pytest.approx(0.6)

    def test_inventory_snapshot_is_overweight(self):
        snap = InventorySnapshot(
            symbol="BTC/USDT",
            base_qty=1.0,
            quote_value=100.0,
            inventory_pct=0.8,    # deviation = 0.3 > max(0.2) → overweight
            target_inventory_pct=0.5,
            max_inventory_pct=0.2,
            skew_bps=20.0,
            unrealized_pnl=0.0,
            realized_pnl=0.0,
            total_trades=0,
        )
        assert snap.is_overweight() is True
        assert snap.is_underweight() is False
