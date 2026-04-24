"""
tests/test_phase3_w15.py — Phase 3 W15-W16 实时数据层单元测试

覆盖模块：
    - modules/data/realtime/orderbook_types  (OrderBookSnapshot, TradeTick, OrderBookDelta, DepthLevel, GapStatus)
    - modules/data/realtime/ws_client        (MockWsClient, WsConnectionState)
    - modules/data/realtime/depth_cache      (DepthCache, DepthCacheRegistry)
    - modules/data/realtime/trade_cache      (TradeCache, TradeCacheRegistry, TradeFlowStats)
    - modules/data/realtime/feature_builder  (MicroFeatureBuilder)
    - modules/data/realtime/replay_feed      (ReplayFeed)
    - modules/data/realtime/subscription_manager (SubscriptionManager)
    - core/event.py                          (新增 Phase 3 事件类型)

测试总数目标：~50 个
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import List

import pytest

# ── 被测模块
from modules.data.realtime.orderbook_types import (
    DepthLevel,
    GapStatus,
    OrderBookDelta,
    OrderBookSnapshot,
    TradeSide,
    TradeTick,
)
from modules.data.realtime.ws_client import (
    HtxMarketWsClient,
    MockWsClient,
    MockWsClientConfig,
    WsClientConfig,
    WsConnectionState,
    create_exchange_ws_client,
)
from modules.data.realtime.verification import verify_realtime_feed
from modules.data.realtime.depth_cache import (
    DepthCache,
    DepthCacheConfig,
    DepthCacheRegistry,
)
from modules.data.realtime.trade_cache import (
    TradeCache,
    TradeCacheConfig,
    TradeCacheRegistry,
    TradeFlowStats,
)
from modules.data.realtime.feature_builder import (
    MICRO_FEATURE_COLS,
    MicroFeatureBuilder,
    MicroFeatureBuilderConfig,
    MicroFeatureFrame,
)
from modules.data.realtime.replay_feed import (
    ReplayFeed,
    ReplayFeedConfig,
    ReplayState,
)
from modules.data.realtime.subscription_manager import (
    FeedHealth,
    FeedHealthSnapshot,
    SubscriptionManager,
    SubscriptionManagerConfig,
)
from core.event import (
    EventType,
    OrderBookSnapshotEvent,
    TradeTickEvent,
    FeedHealthChangedEvent,
    MicroFeatureComputedEvent,
)


# ══════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════

def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _make_delta(
    symbol: str = "BTC/USDT",
    sequence_id: int = 1,
    prev_sequence_id: int = 0,
    mid: float = 50000.0,
    is_snapshot: bool = False,
) -> OrderBookDelta:
    half = mid * 0.0001
    bids = [DepthLevel(price=mid - half - i * 0.5, size=1.0 + i * 0.1) for i in range(5)]
    asks = [DepthLevel(price=mid + half + i * 0.5, size=1.0 + i * 0.1) for i in range(5)]
    return OrderBookDelta(
        symbol=symbol,
        exchange="mock",
        sequence_id=sequence_id,
        prev_sequence_id=prev_sequence_id,
        bid_updates=bids,
        ask_updates=asks,
        received_at=_now(),
        is_snapshot=is_snapshot,
    )


def _make_tick(
    symbol: str = "BTC/USDT",
    price: float = 50000.0,
    size: float = 0.01,
    side: TradeSide = TradeSide.BUY,
    trade_id: str = "t001",
) -> TradeTick:
    return TradeTick(
        symbol=symbol,
        exchange="mock",
        trade_id=trade_id,
        side=side,
        price=price,
        size=size,
        notional=price * size,
        received_at=_now(),
    )


# ══════════════════════════════════════════════════════════════
# 一、OrderBookTypes 测试（10 个）
# ══════════════════════════════════════════════════════════════

class TestOrderBookTypes:
    def test_depth_level_notional(self):
        lv = DepthLevel(price=50000.0, size=0.5)
        assert lv.notional() == 25000.0

    def test_depth_level_is_empty(self):
        assert DepthLevel(price=100.0, size=0.0).is_empty()
        assert not DepthLevel(price=100.0, size=0.001).is_empty()

    def test_gap_status_healthy(self):
        assert GapStatus.OK.is_healthy()
        assert GapStatus.RECOVERED.is_healthy()
        assert not GapStatus.GAP_DETECTED.is_healthy()
        assert not GapStatus.RECOVERING.is_healthy()
        assert not GapStatus.FATAL.is_healthy()

    def test_trade_tick_create_mock(self):
        tick = TradeTick.create_mock(price=49000.0, size=0.1)
        assert tick.notional == pytest.approx(4900.0)
        assert tick.side == TradeSide.BUY
        assert tick.exchange == "mock"

    def test_order_book_snapshot_create_mock(self):
        snap = OrderBookSnapshot.create_mock(mid_price=50000.0, spread_bps=2.0, depth=5)
        assert snap.is_healthy()
        assert snap.best_bid < snap.best_ask
        assert snap.spread_bps == pytest.approx(2.0, abs=0.01)
        assert len(snap.bids) == 5
        assert len(snap.asks) == 5

    def test_order_book_snapshot_is_healthy_gap(self):
        snap = OrderBookSnapshot.create_mock()
        # 使用 replace 无法用于 frozen，改用构造函数传 gap_status
        snap_gap = OrderBookSnapshot(
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
        assert not snap_gap.is_healthy()

    def test_order_book_snapshot_latency_ms(self):
        snap = OrderBookSnapshot.create_mock()
        latency = snap.latency_ms()
        # exchange_ts == received_at for mock
        assert latency is not None
        assert abs(latency) < 100  # 近似 0ms

    def test_order_book_snapshot_no_exchange_ts(self):
        snap = OrderBookSnapshot.create_mock()
        snap_no_ts = OrderBookSnapshot(
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
            exchange_ts=None,
        )
        assert snap_no_ts.latency_ms() is None

    def test_order_book_delta_expected_sequence(self):
        delta = _make_delta(sequence_id=42, prev_sequence_id=41)
        assert delta.expected_sequence() == 43

    def test_trade_side_values(self):
        assert TradeSide.BUY.value == "buy"
        assert TradeSide.SELL.value == "sell"
        assert TradeSide.UNKNOWN.value == "unknown"


# ══════════════════════════════════════════════════════════════
# 二、MockWsClient 测试（8 个）
# ══════════════════════════════════════════════════════════════

class TestMockWsClient:
    def _make_client(self, **kwargs) -> MockWsClient:
        cfg = MockWsClientConfig(
            base_config=WsClientConfig(exchange="mock"),
            **kwargs,
        )
        return MockWsClient(cfg)

    def test_connect_subscribe(self):
        client = self._make_client()
        client.connect(["BTC/USDT", "ETH/USDT"])
        assert client.state == WsConnectionState.CONNECTED
        assert "BTC/USDT" in client.subscribed_symbols
        assert "ETH/USDT" in client.subscribed_symbols

    def test_disconnect(self):
        client = self._make_client()
        client.connect(["BTC/USDT"])
        client.disconnect()
        assert client.state == WsConnectionState.DISCONNECTED

    def test_fail_on_connect(self):
        client = self._make_client(fail_on_connect=True)
        with pytest.raises(ConnectionError):
            client.connect(["BTC/USDT"])
        assert client.state == WsConnectionState.DISCONNECTED

    def test_push_depth_triggers_callback(self):
        client = self._make_client()
        client.connect(["BTC/USDT"])
        received = []
        client.set_depth_callback(received.append)
        delta = _make_delta()
        client.push_depth(delta)
        assert len(received) == 1
        assert received[0].symbol == "BTC/USDT"

    def test_push_trade_triggers_callback(self):
        client = self._make_client()
        client.connect(["BTC/USDT"])
        received = []
        client.set_trade_callback(received.append)
        tick = _make_tick()
        client.push_trade(tick)
        assert len(received) == 1
        assert received[0].side == TradeSide.BUY

    def test_generate_depth_update(self):
        client = self._make_client(seed=42)
        client.connect(["BTC/USDT"])
        delta = client.generate_depth_update("BTC/USDT")
        assert delta is not None
        assert delta.symbol == "BTC/USDT"
        assert len(delta.bid_updates) > 0
        assert len(delta.ask_updates) > 0

    def test_generate_depth_update_force_gap(self):
        client = self._make_client(seed=1)
        client.connect(["BTC/USDT"])
        d1 = client.generate_depth_update("BTC/USDT")
        d2 = client.generate_depth_update("BTC/USDT", force_gap=True)
        # force_gap 跳过一个序列号
        assert d2.sequence_id > d1.sequence_id + 1

    def test_diagnostics(self):
        client = self._make_client()
        client.connect(["BTC/USDT"])
        diag = client.diagnostics()
        assert diag["exchange"] == "mock"
        assert diag["state"] == "connected"


class TestHtxMarketWsClient:
    def _make_client(self, **kwargs) -> HtxMarketWsClient:
        return HtxMarketWsClient(WsClientConfig(exchange="htx", depth_levels=5, **kwargs))

    def test_parse_depth_message_builds_snapshot_delta(self):
        client = self._make_client()
        payload = {
            "ch": "market.btcusdt.depth.step0",
            "ts": 1710000001000,
            "tick": {
                "seqNum": 321,
                "bids": [[50000.0, 1.2], [49999.5, 0.8]],
                "asks": [[50001.0, 1.1], [50001.5, 0.7]],
            },
        }

        delta = client._parse_depth_message(payload)

        assert delta is not None
        assert delta.symbol == "BTC/USDT"
        assert delta.exchange == "htx"
        assert delta.sequence_id == 321
        assert delta.is_snapshot is True
        assert delta.bid_updates[0].price == pytest.approx(50000.0)
        assert delta.ask_updates[0].size == pytest.approx(1.1)

    def test_parse_trade_message_builds_ticks(self):
        client = self._make_client()
        payload = {
            "ch": "market.ethusdt.trade.detail",
            "ts": 1710000002000,
            "tick": {
                "id": 99,
                "data": [
                    {
                        "tradeId": 123,
                        "amount": 0.25,
                        "price": 3000.5,
                        "direction": "buy",
                        "ts": 1710000002001,
                    },
                    {
                        "tradeId": 124,
                        "amount": 0.10,
                        "price": 3000.0,
                        "direction": "sell",
                        "ts": 1710000002002,
                    },
                ],
            },
        }

        ticks = client._parse_trade_message(payload)

        assert len(ticks) == 2
        assert ticks[0].symbol == "ETH/USDT"
        assert ticks[0].side == TradeSide.BUY
        assert ticks[1].side == TradeSide.SELL
        assert ticks[0].trade_id == "123"
        assert ticks[1].notional == pytest.approx(300.0)

    def test_request_snapshot_uses_ccxt_rest(self, monkeypatch):
        from modules.data.realtime import ws_client as ws_mod

        class FakeExchange:
            def __init__(self, config):
                self.config = config

            def fetch_order_book(self, symbol, limit=None):
                assert symbol == "BTC/USDT"
                assert limit == 5
                return {
                    "nonce": 777,
                    "timestamp": 1710000003000,
                    "bids": [[50000.0, 1.5], [49999.5, 1.0]],
                    "asks": [[50001.0, 1.4], [50001.5, 0.9]],
                }

            def close(self):
                return None

        monkeypatch.setattr(ws_mod.ccxt, "htx", FakeExchange)
        client = self._make_client()

        delta = client.request_snapshot("BTC/USDT")

        assert delta is not None
        assert delta.is_snapshot is True
        assert delta.sequence_id == 777
        assert delta.bid_updates[0].price == pytest.approx(50000.0)
        assert delta.ask_updates[1].size == pytest.approx(0.9)


class TestWsClientFactory:
    def test_create_exchange_ws_client_uses_htx_provider(self):
        client = create_exchange_ws_client("htx", WsClientConfig(exchange="htx"))
        assert isinstance(client, HtxMarketWsClient)

    def test_create_exchange_ws_client_unknown_provider_falls_back_to_mock(self):
        client = create_exchange_ws_client("unknown", WsClientConfig(exchange="htx"))
        assert isinstance(client, MockWsClient)


class TestRealtimeFeedVerification:
    def test_verify_realtime_feed_mock_provider(self):
        result = verify_realtime_feed(
            provider="mock",
            exchange="mock",
            symbols=["BTC/USDT"],
            timeout_sec=1.0,
            depth_levels=5,
            heartbeat_timeout_sec=5.0,
            health_check_interval_sec=1.0,
            poll_interval_sec=0.01,
        )

        assert result.success is True
        assert result.health == "healthy"
        assert len(result.statuses) == 1
        assert result.statuses[0].symbol == "BTC/USDT"
        assert result.statuses[0].has_snapshot is True
        assert result.statuses[0].trade_count > 0


# ══════════════════════════════════════════════════════════════
# 三、DepthCache 测试（12 个）
# ══════════════════════════════════════════════════════════════

class TestDepthCache:
    def _make_cache(self, **kwargs) -> DepthCache:
        cfg = DepthCacheConfig(**kwargs)
        return DepthCache(symbol="BTC/USDT", exchange="mock", config=cfg)

    def test_first_apply_returns_snapshot(self):
        cache = self._make_cache()
        delta = _make_delta(sequence_id=1, prev_sequence_id=0)
        snap = cache.apply(delta)
        assert snap is not None
        assert snap.symbol == "BTC/USDT"
        assert snap.best_bid > 0
        assert snap.best_ask > snap.best_bid

    def test_sequential_apply(self):
        cache = self._make_cache()
        cache.apply(_make_delta(sequence_id=1, prev_sequence_id=0))
        snap = cache.apply(_make_delta(sequence_id=2, prev_sequence_id=1))
        assert snap is not None
        assert cache.gap_status == GapStatus.OK
        assert cache.sequence_id == 2

    def test_gap_detection_returns_none(self):
        cache = self._make_cache()
        cache.apply(_make_delta(sequence_id=1, prev_sequence_id=0))
        # 跳过 seq=2，直接发 seq=5
        snap = cache.apply(_make_delta(sequence_id=5, prev_sequence_id=4))
        assert snap is None
        assert cache.gap_status == GapStatus.GAP_DETECTED

    def test_gap_recovery_via_snapshot(self):
        cache = self._make_cache()
        cache.apply(_make_delta(sequence_id=1, prev_sequence_id=0))
        # 触发 gap
        cache.apply(_make_delta(sequence_id=5, prev_sequence_id=4))
        assert cache.gap_status == GapStatus.GAP_DETECTED
        # 发送全量快照恢复
        recovery = _make_delta(sequence_id=5, prev_sequence_id=4, is_snapshot=True)
        snap = cache.apply(recovery)
        assert snap is not None
        assert snap.is_gap_recovered
        assert cache.gap_status.is_healthy()

    def test_get_snapshot_returns_none_on_gap(self):
        cache = self._make_cache()
        cache.apply(_make_delta(sequence_id=1, prev_sequence_id=0))
        cache.apply(_make_delta(sequence_id=5, prev_sequence_id=4))  # gap
        assert cache.get_snapshot() is None

    def test_get_snapshot_returns_healthy(self):
        cache = self._make_cache()
        cache.apply(_make_delta(sequence_id=1, prev_sequence_id=0))
        cache.apply(_make_delta(sequence_id=2, prev_sequence_id=1))
        snap = cache.get_snapshot()
        assert snap is not None
        assert snap.is_healthy()

    def test_spread_bps_computed(self):
        cache = self._make_cache()
        snap = cache.apply(_make_delta(sequence_id=1, prev_sequence_id=0, mid=50000.0))
        assert snap is not None
        assert snap.spread_bps > 0

    def test_imbalance_range(self):
        cache = self._make_cache()
        snap = cache.apply(_make_delta(sequence_id=1, prev_sequence_id=0))
        assert snap is not None
        assert -1.0 <= snap.imbalance <= 1.0

    def test_mid_price_between_bid_ask(self):
        cache = self._make_cache()
        snap = cache.apply(_make_delta(sequence_id=1, prev_sequence_id=0, mid=50000.0))
        assert snap.best_bid < snap.mid_price < snap.best_ask

    def test_reset_clears_state(self):
        cache = self._make_cache()
        cache.apply(_make_delta(sequence_id=1, prev_sequence_id=0))
        cache.reset()
        assert cache.sequence_id is None
        assert cache.gap_status == GapStatus.OK
        assert cache.get_snapshot() is None

    def test_depth_cache_registry(self):
        registry = DepthCacheRegistry()
        delta = _make_delta(sequence_id=1, prev_sequence_id=0)
        snap = registry.apply_delta(delta)
        assert snap is not None
        # 同一 key 重用
        delta2 = _make_delta(sequence_id=2, prev_sequence_id=1)
        snap2 = registry.apply_delta(delta2)
        assert snap2 is not None

    def test_diagnostics(self):
        cache = self._make_cache()
        cache.apply(_make_delta(sequence_id=1, prev_sequence_id=0))
        diag = cache.diagnostics()
        assert diag["symbol"] == "BTC/USDT"
        assert diag["update_count"] == 1
        assert diag["gap_count"] == 0


# ══════════════════════════════════════════════════════════════
# 四、TradeCache 测试（8 个）
# ══════════════════════════════════════════════════════════════

class TestTradeCache:
    def _make_cache(self) -> TradeCache:
        return TradeCache("BTC/USDT", "mock", TradeCacheConfig(max_trades=100))

    def test_add_and_stats(self):
        cache = self._make_cache()
        cache.add(_make_tick(side=TradeSide.BUY, price=50000.0, size=0.5, trade_id="t1"))
        cache.add(_make_tick(side=TradeSide.SELL, price=50000.0, size=0.3, trade_id="t2"))
        stats = cache.compute_stats()
        assert stats.trade_count == 2
        assert stats.buy_volume == pytest.approx(0.5)
        assert stats.sell_volume == pytest.approx(0.3)

    def test_dedup(self):
        cache = self._make_cache()
        tick = _make_tick(trade_id="dup")
        r1 = cache.add(tick)
        r2 = cache.add(tick)
        assert r1 is True
        assert r2 is False
        stats = cache.compute_stats()
        assert stats.trade_count == 1

    def test_trade_flow_imbalance_all_buy(self):
        cache = self._make_cache()
        for i in range(10):
            cache.add(_make_tick(side=TradeSide.BUY, size=1.0, trade_id=f"b{i}"))
        stats = cache.compute_stats()
        assert stats.trade_flow_imbalance == pytest.approx(1.0)

    def test_trade_flow_imbalance_all_sell(self):
        cache = self._make_cache()
        for i in range(10):
            cache.add(_make_tick(side=TradeSide.SELL, size=1.0, trade_id=f"s{i}"))
        stats = cache.compute_stats()
        assert stats.trade_flow_imbalance == pytest.approx(-1.0)

    def test_vwap(self):
        cache = self._make_cache()
        cache.add(_make_tick(price=50000.0, size=1.0, trade_id="v1"))
        cache.add(_make_tick(price=50200.0, size=1.0, trade_id="v2"))
        stats = cache.compute_stats()
        assert stats.vwap == pytest.approx(50100.0)

    def test_empty_stats(self):
        cache = self._make_cache()
        stats = cache.compute_stats()
        assert stats.is_empty()
        assert stats.vwap is None
        assert stats.latest_price is None

    def test_clear(self):
        cache = self._make_cache()
        cache.add(_make_tick(trade_id="x1"))
        cache.clear()
        assert cache.compute_stats().is_empty()

    def test_registry(self):
        registry = TradeCacheRegistry()
        tick = _make_tick()
        registry.add_tick(tick)
        stats = registry.get_stats("BTC/USDT", "mock")
        assert stats is not None
        assert stats.trade_count == 1


# ══════════════════════════════════════════════════════════════
# 五、MicroFeatureBuilder 测试（10 个）
# ══════════════════════════════════════════════════════════════

class TestMicroFeatureBuilder:
    def _make_builder(self) -> MicroFeatureBuilder:
        return MicroFeatureBuilder(MicroFeatureBuilderConfig(log_build_time=False))

    def _make_trade_stats(
        self,
        buy_vol: float = 5.0,
        sell_vol: float = 3.0,
        vwap: float = 50000.0,
        trade_count: int = 10,
    ) -> TradeFlowStats:
        from modules.data.realtime.trade_cache import TradeFlowStats as TFS
        total_vol = buy_vol + sell_vol
        imbalance = (buy_vol - sell_vol) / total_vol if total_vol > 0 else 0.0
        return TFS(
            symbol="BTC/USDT",
            trade_count=trade_count,
            buy_volume=buy_vol,
            sell_volume=sell_vol,
            trade_flow_imbalance=imbalance,
            vwap=vwap,
            total_notional=vwap * (buy_vol + sell_vol),
            liquidation_count=0,
            latest_price=vwap,
        )

    def test_build_healthy_snapshot(self):
        builder = self._make_builder()
        snap = OrderBookSnapshot.create_mock(mid_price=50000.0, spread_bps=2.0)
        frame = builder.build(snap)
        assert frame.is_book_healthy
        assert not math.isnan(frame.mb_spread_bps)
        assert not math.isnan(frame.mb_order_imbalance)

    def test_build_none_snapshot_all_nan(self):
        builder = self._make_builder()
        frame = builder.build(None)
        assert not frame.is_book_healthy
        assert math.isnan(frame.mb_spread_bps)
        assert math.isnan(frame.mb_order_imbalance)
        assert math.isnan(frame.mb_micro_price)

    def test_build_gap_snapshot_nan(self):
        builder = self._make_builder()
        snap = OrderBookSnapshot.create_mock()
        snap_gap = OrderBookSnapshot(
            symbol=snap.symbol, exchange=snap.exchange,
            sequence_id=snap.sequence_id, best_bid=snap.best_bid,
            best_ask=snap.best_ask, bids=snap.bids, asks=snap.asks,
            spread_bps=snap.spread_bps, mid_price=snap.mid_price,
            imbalance=snap.imbalance, received_at=snap.received_at,
            gap_status=GapStatus.GAP_DETECTED,
        )
        frame = builder.build(snap_gap)
        assert math.isnan(frame.mb_spread_bps)

    def test_spread_bps_clip(self):
        builder = MicroFeatureBuilder(MicroFeatureBuilderConfig(spread_clip_bps=50.0))
        snap = OrderBookSnapshot.create_mock(spread_bps=300.0)
        frame = builder.build(snap)
        assert frame.mb_spread_bps <= 50.0

    def test_trade_flow_imbalance(self):
        builder = self._make_builder()
        snap = OrderBookSnapshot.create_mock()
        stats = self._make_trade_stats(buy_vol=8.0, sell_vol=2.0)
        frame = builder.build(snap, stats)
        assert frame.mb_trade_flow_imbalance == pytest.approx(0.6, abs=0.01)

    def test_trade_flow_nan_when_empty(self):
        builder = self._make_builder()
        snap = OrderBookSnapshot.create_mock()
        from modules.data.realtime.trade_cache import TradeFlowStats as TFS
        empty_stats = TFS(
            symbol="BTC/USDT", trade_count=0,
            buy_volume=0.0, sell_volume=0.0,
            trade_flow_imbalance=0.0, vwap=None,
            total_notional=0.0, liquidation_count=0, latest_price=None,
        )
        frame = builder.build(snap, empty_stats)
        assert math.isnan(frame.mb_trade_flow_imbalance)

    def test_spread_tightness(self):
        builder = self._make_builder()
        snap = OrderBookSnapshot.create_mock(spread_bps=2.0)
        frame = builder.build(snap)
        # tightness = 1 / (1 + spread_bps), always in (0, 1]
        assert not math.isnan(frame.mb_spread_tightness)
        assert 0.0 < frame.mb_spread_tightness <= 1.0

    def test_to_series_cols(self):
        builder = self._make_builder()
        snap = OrderBookSnapshot.create_mock()
        frame = builder.build(snap)
        series = frame.to_series()
        assert list(series.index) == MICRO_FEATURE_COLS

    def test_to_dataframe_shape(self):
        builder = self._make_builder()
        snap = OrderBookSnapshot.create_mock()
        frame = builder.build(snap)
        df = frame.to_dataframe()
        assert df.shape == (1, len(MICRO_FEATURE_COLS))

    def test_feature_names_constant(self):
        assert len(MICRO_FEATURE_COLS) == 8
        for col in MICRO_FEATURE_COLS:
            assert col.startswith("mb_")


# ══════════════════════════════════════════════════════════════
# 六、ReplayFeed 测试（7 个）
# ══════════════════════════════════════════════════════════════

class TestReplayFeed:
    def _make_events(self, n: int = 10) -> list:
        events = []
        base_ts = _now()
        for i in range(n):
            from datetime import timedelta
            ts = datetime(
                base_ts.year, base_ts.month, base_ts.day,
                base_ts.hour, base_ts.minute, base_ts.second,
                tzinfo=timezone.utc,
            )
            ts = ts.replace(microsecond=i * 1000)
            delta = _make_delta(sequence_id=i + 1, prev_sequence_id=i)
            # 替换 received_at
            delta = OrderBookDelta(
                symbol=delta.symbol, exchange=delta.exchange,
                sequence_id=delta.sequence_id,
                prev_sequence_id=delta.prev_sequence_id,
                bid_updates=delta.bid_updates, ask_updates=delta.ask_updates,
                received_at=ts,
            )
            events.append(delta)
        return events

    def test_load_and_replay_blocking(self):
        cfg = ReplayFeedConfig(playback_speed=0)  # 0 = 无延迟
        feed = ReplayFeed(cfg)
        events = self._make_events(5)
        received = []
        feed.load_events(events)
        feed.set_depth_callback(received.append)
        feed.start(blocking=True)
        assert len(received) == 5

    def test_replay_async(self):
        cfg = ReplayFeedConfig(playback_speed=0)
        feed = ReplayFeed(cfg)
        events = self._make_events(3)
        received = []
        feed.load_events(events)
        feed.set_depth_callback(received.append)
        feed.start(blocking=False)
        done = feed.wait_until_done(timeout=5.0)
        assert done
        assert len(received) == 3

    def test_replay_trade_callback(self):
        cfg = ReplayFeedConfig(playback_speed=0)
        feed = ReplayFeed(cfg)
        ticks = [_make_tick(trade_id=f"t{i}") for i in range(4)]
        received = []
        feed.load_events(ticks)  # type: ignore
        feed.set_trade_callback(received.append)
        feed.start(blocking=True)
        assert len(received) == 4

    def test_stop_cancels_replay(self):
        cfg = ReplayFeedConfig(playback_speed=0.001)  # 非常慢，便于测试停止
        feed = ReplayFeed(cfg)
        events = self._make_events(100)
        received = []
        feed.load_events(events)
        feed.set_depth_callback(received.append)
        feed.start(blocking=False)
        time.sleep(0.05)
        feed.stop()
        done = feed.wait_until_done(timeout=2.0)
        assert done
        # 被停止，收到事件数 < 100
        assert len(received) < 100

    def test_empty_events_no_start(self):
        feed = ReplayFeed(ReplayFeedConfig(playback_speed=0))
        feed.start(blocking=True)
        assert feed.emit_count == 0

    def test_diagnostics(self):
        feed = ReplayFeed(ReplayFeedConfig(playback_speed=0))
        feed.load_events(self._make_events(3))
        feed.start(blocking=True)
        diag = feed.diagnostics()
        assert diag["emit_count"] == 3
        assert diag["state"] == "idle"

    def test_time_filter(self):
        from datetime import timedelta
        cfg = ReplayFeedConfig(
            playback_speed=0,
            start_time=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            end_time=datetime(2024, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        )
        feed = ReplayFeed(cfg)
        # 模拟事件在 2024-01-01 00:00:00.000 ~ 00:00:02.000
        base = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        events = [
            OrderBookDelta(
                symbol="BTC/USDT", exchange="mock",
                sequence_id=i + 1, prev_sequence_id=i,
                bid_updates=[], ask_updates=[],
                received_at=datetime(2024, 1, 1, 0, 0, i, tzinfo=timezone.utc),
            )
            for i in range(3)
        ]
        received = []
        feed.load_events(events)  # type: ignore
        feed.set_depth_callback(received.append)
        feed.start(blocking=True)
        # 只有 ts=0s 和 ts=1s 在范围内
        assert len(received) == 2


# ══════════════════════════════════════════════════════════════
# 七、SubscriptionManager 测试（5 个）
# ══════════════════════════════════════════════════════════════

class TestSubscriptionManager:
    def _make_manager(
        self,
        fail_on_connect: bool = False,
    ) -> tuple[SubscriptionManager, MockWsClient]:
        client = MockWsClient(
            MockWsClientConfig(
                base_config=WsClientConfig(exchange="mock"),
                fail_on_connect=fail_on_connect,
            )
        )
        depth_reg = DepthCacheRegistry()
        trade_reg = TradeCacheRegistry()
        cfg = SubscriptionManagerConfig(
            health_check_interval_sec=60.0,  # 禁用自动健康检查
            max_reconnect_attempts=1,
        )
        mgr = SubscriptionManager(client, depth_reg, trade_reg, cfg)
        return mgr, client

    def test_start_healthy(self):
        mgr, _ = self._make_manager()
        mgr.start(["BTC/USDT"])
        health = mgr.get_health()
        assert health.health == FeedHealth.HEALTHY
        assert "BTC/USDT" in health.subscribed_symbols
        mgr.stop()

    def test_depth_routed_to_cache(self):
        mgr, client = self._make_manager()
        depth_reg = mgr._depth_registry
        mgr.start(["BTC/USDT"])
        delta = _make_delta(sequence_id=1, prev_sequence_id=0)
        client.push_depth(delta)
        cache = depth_reg.get("BTC/USDT", "mock")
        assert cache is not None
        assert cache.sequence_id == 1
        mgr.stop()

    def test_trade_routed_to_cache(self):
        mgr, client = self._make_manager()
        trade_reg = mgr._trade_registry
        mgr.start(["BTC/USDT"])
        tick = _make_tick()
        client.push_trade(tick)
        stats = trade_reg.get_stats("BTC/USDT", "mock")
        assert stats is not None
        assert stats.trade_count == 1
        mgr.stop()

    def test_health_callback_triggered(self):
        mgr, _ = self._make_manager()
        callbacks = []
        mgr.set_health_callback(callbacks.append)
        mgr.start(["BTC/USDT"])
        assert len(callbacks) == 1  # HEALTHY on connect
        mgr.stop()
        assert callbacks[-1].health == FeedHealth.STOPPED

    def test_diagnostics(self):
        mgr, _ = self._make_manager()
        mgr.start(["BTC/USDT"])
        diag = mgr.diagnostics()
        assert diag["health"] == "healthy"
        assert diag["exchange"] == "mock"
        mgr.stop()


# ══════════════════════════════════════════════════════════════
# 八、core/event.py Phase 3 事件类型测试（4 个）
# ══════════════════════════════════════════════════════════════

class TestPhase3Events:
    def test_event_types_exist(self):
        assert hasattr(EventType, "ORDER_BOOK_SNAPSHOT")
        assert hasattr(EventType, "TRADE_TICK")
        assert hasattr(EventType, "FEED_HEALTH_CHANGED")
        assert hasattr(EventType, "MICRO_FEATURE_COMPUTED")

    def test_order_book_snapshot_event(self):
        evt = OrderBookSnapshotEvent(
            event_type=EventType.ORDER_BOOK_SNAPSHOT,
            timestamp=_now(),
            source="depth_cache",
            symbol="BTC/USDT",
            exchange="binance",
            sequence_id=42,
            best_bid=49999.0,
            best_ask=50001.0,
            spread_bps=0.4,
            mid_price=50000.0,
            imbalance=0.1,
            depth_levels=40,
        )
        assert evt.symbol == "BTC/USDT"
        assert evt.spread_bps == pytest.approx(0.4)

    def test_trade_tick_event(self):
        evt = TradeTickEvent(
            event_type=EventType.TRADE_TICK,
            timestamp=_now(),
            source="ws_client",
            symbol="ETH/USDT",
            exchange="binance",
            trade_id="t-001",
            side="buy",
            price=3000.0,
            size=0.5,
            notional=1500.0,
        )
        assert evt.side == "buy"
        assert evt.notional == pytest.approx(1500.0)

    def test_feed_health_changed_event(self):
        evt = FeedHealthChangedEvent(
            event_type=EventType.FEED_HEALTH_CHANGED,
            timestamp=_now(),
            source="subscription_manager",
            exchange="binance",
            health="degraded",
            subscribed_symbols=["BTC/USDT"],
            reconnect_count=3,
            detail="心跳超时",
        )
        assert evt.health == "degraded"
        assert evt.reconnect_count == 3
