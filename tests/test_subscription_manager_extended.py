"""
tests/test_subscription_manager_extended.py — 补充覆盖 subscription_manager.py 缺失分支

Target missed lines: 181-182, 188-195, 199-206, 244-251, 259-260, 281, 285-300,
                     310, 313-318, 342-350, 368-369
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, call, patch

import pytest

from modules.data.realtime.subscription_manager import (
    FeedHealth,
    FeedHealthSnapshot,
    SubscriptionManager,
    SubscriptionManagerConfig,
)
from modules.data.realtime.ws_client import WsConnectionState


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

def _make_client(
    state: WsConnectionState = WsConnectionState.CONNECTED,
    heartbeat_alive: bool = True,
    exchange: str = "binance",
) -> MagicMock:
    client = MagicMock()
    client.config.exchange = exchange
    client.state = state
    client.is_heartbeat_alive.return_value = heartbeat_alive
    client._last_heartbeat_at = datetime.now(tz=timezone.utc)
    return client


def _make_manager(
    *,
    state: WsConnectionState = WsConnectionState.CONNECTED,
    heartbeat_alive: bool = True,
    config: SubscriptionManagerConfig = None,
) -> tuple[SubscriptionManager, MagicMock]:
    client = _make_client(state=state, heartbeat_alive=heartbeat_alive)
    depth_reg = MagicMock()
    trade_reg = MagicMock()
    cfg = config or SubscriptionManagerConfig(
        health_check_interval_sec=0.1,
        reconnect_backoff_base_sec=0.01,
        reconnect_backoff_max_sec=0.05,
        max_reconnect_attempts=2,
    )
    mgr = SubscriptionManager(
        ws_client=client,
        depth_registry=depth_reg,
        trade_registry=trade_reg,
        config=cfg,
    )
    return mgr, client


# ══════════════════════════════════════════════════════════════
# Tests: start / stop
# ══════════════════════════════════════════════════════════════

class TestStartStop:

    def test_start_calls_connect_with_retry(self):
        mgr, client = _make_manager()
        mgr.start(["BTC/USDT"])
        client.connect.assert_called_once_with(["BTC/USDT"])
        mgr.stop()

    def test_stop_calls_disconnect(self):
        mgr, client = _make_manager()
        mgr.start(["BTC/USDT"])
        mgr.stop()
        client.disconnect.assert_called()

    def test_stop_sets_health_to_stopped(self):
        mgr, client = _make_manager()
        mgr.start(["BTC/USDT"])
        mgr.stop()
        assert mgr._health == FeedHealth.STOPPED

    def test_stop_disconnect_exception_handled(self):
        mgr, client = _make_manager()
        client.disconnect.side_effect = RuntimeError("fail")
        mgr.start(["BTC/USDT"])
        # Should not raise
        mgr.stop()
        assert mgr._health == FeedHealth.STOPPED


# ══════════════════════════════════════════════════════════════
# Tests: subscribe / unsubscribe
# ══════════════════════════════════════════════════════════════

class TestSubscribeUnsubscribe:

    def test_subscribe_adds_symbol(self):
        mgr, client = _make_manager()
        mgr.subscribe("ETH/USDT")
        assert "ETH/USDT" in mgr._symbols
        client.subscribe.assert_called_with("ETH/USDT")

    def test_subscribe_duplicate_not_added_twice(self):
        mgr, client = _make_manager()
        mgr._symbols = ["BTC/USDT"]
        mgr.subscribe("BTC/USDT")
        assert mgr._symbols.count("BTC/USDT") == 1

    def test_subscribe_client_exception_handled(self):
        mgr, client = _make_manager()
        client.subscribe.side_effect = RuntimeError("ws error")
        mgr.subscribe("ETH/USDT")  # Should not raise
        assert "ETH/USDT" in mgr._symbols

    def test_unsubscribe_removes_symbol(self):
        mgr, client = _make_manager()
        mgr._symbols = ["BTC/USDT", "ETH/USDT"]
        mgr.unsubscribe("BTC/USDT")
        assert "BTC/USDT" not in mgr._symbols
        client.unsubscribe.assert_called_with("BTC/USDT")

    def test_unsubscribe_not_present_no_error(self):
        mgr, client = _make_manager()
        mgr._symbols = []
        mgr.unsubscribe("BTC/USDT")  # Should not raise

    def test_unsubscribe_client_exception_handled(self):
        mgr, client = _make_manager()
        mgr._symbols = ["BTC/USDT"]
        client.unsubscribe.side_effect = RuntimeError("ws error")
        mgr.unsubscribe("BTC/USDT")  # Should not raise


# ══════════════════════════════════════════════════════════════
# Tests: get_health / diagnostics
# ══════════════════════════════════════════════════════════════

class TestHealthAndDiagnostics:

    def test_get_health_returns_snapshot(self):
        mgr, client = _make_manager()
        snapshot = mgr.get_health()
        assert isinstance(snapshot, FeedHealthSnapshot)
        assert snapshot.exchange == "binance"

    def test_get_health_reflects_current_health(self):
        mgr, client = _make_manager()
        mgr._health = FeedHealth.DEGRADED
        snapshot = mgr.get_health()
        assert snapshot.health == FeedHealth.DEGRADED

    def test_diagnostics_returns_dict(self):
        mgr, client = _make_manager()
        client.state = WsConnectionState.CONNECTED
        d = mgr.diagnostics()
        assert isinstance(d, dict)
        assert "health" in d
        assert "exchange" in d
        assert "reconnect_count" in d

    def test_diagnostics_last_heartbeat_none(self):
        mgr, client = _make_manager()
        client._last_heartbeat_at = None
        d = mgr.diagnostics()
        assert d["last_heartbeat_at"] is None

    def test_diagnostics_last_heartbeat_present(self):
        mgr, client = _make_manager()
        client._last_heartbeat_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        d = mgr.diagnostics()
        assert d["last_heartbeat_at"] is not None


# ══════════════════════════════════════════════════════════════
# Tests: _on_depth / _on_trade callbacks
# ══════════════════════════════════════════════════════════════

class TestCallbackRouting:

    def test_on_depth_routes_to_depth_registry(self):
        mgr, client = _make_manager()
        delta = MagicMock()
        delta.symbol = "BTC/USDT"
        delta.sequence_id = 1
        mgr._depth_registry.apply_delta.return_value = MagicMock()
        mgr._on_depth(delta)
        mgr._depth_registry.apply_delta.assert_called_once_with(delta)

    def test_on_depth_none_snapshot_logged(self):
        """apply_delta 返回 None (gap 状态) 时不应抛出异常。"""
        mgr, client = _make_manager()
        delta = MagicMock()
        delta.symbol = "BTC/USDT"
        delta.sequence_id = 1
        mgr._depth_registry.apply_delta.return_value = None
        mgr._on_depth(delta)  # Should not raise

    def test_on_depth_exception_handled(self):
        mgr, client = _make_manager()
        delta = MagicMock()
        delta.symbol = "BTC/USDT"
        mgr._depth_registry.apply_delta.side_effect = RuntimeError("fail")
        mgr._on_depth(delta)  # Should not raise

    def test_on_trade_routes_to_trade_registry(self):
        mgr, client = _make_manager()
        tick = MagicMock()
        tick.symbol = "BTC/USDT"
        mgr._on_trade(tick)
        mgr._trade_registry.add_tick.assert_called_once_with(tick)

    def test_on_trade_exception_handled(self):
        mgr, client = _make_manager()
        tick = MagicMock()
        tick.symbol = "BTC/USDT"
        mgr._trade_registry.add_tick.side_effect = RuntimeError("fail")
        mgr._on_trade(tick)  # Should not raise


# ══════════════════════════════════════════════════════════════
# Tests: _check_and_recover
# ══════════════════════════════════════════════════════════════

class TestCheckAndRecover:

    def test_healthy_client_sets_health_to_healthy(self):
        mgr, client = _make_manager(
            state=WsConnectionState.CONNECTED, heartbeat_alive=True
        )
        mgr._health = FeedHealth.DEGRADED  # was degraded
        mgr._check_and_recover()
        assert mgr._health == FeedHealth.HEALTHY

    def test_healthy_client_already_healthy_no_change(self):
        mgr, client = _make_manager(
            state=WsConnectionState.CONNECTED, heartbeat_alive=True
        )
        mgr._health = FeedHealth.HEALTHY
        mgr._check_and_recover()
        assert mgr._health == FeedHealth.HEALTHY

    def test_unhealthy_client_triggers_reconnect(self):
        mgr, client = _make_manager(
            state=WsConnectionState.DISCONNECTED, heartbeat_alive=False
        )
        # Make _connect_with_retry succeed immediately
        client.connect.return_value = None
        mgr._check_and_recover()
        assert client.connect.called


# ══════════════════════════════════════════════════════════════
# Tests: _connect_with_retry — max attempts
# ══════════════════════════════════════════════════════════════

class TestConnectWithRetry:

    def test_max_attempts_exceeded_sets_degraded(self):
        cfg = SubscriptionManagerConfig(
            max_reconnect_attempts=2,
            reconnect_backoff_base_sec=0.001,
            reconnect_backoff_max_sec=0.01,
        )
        mgr, client = _make_manager(config=cfg)
        client.connect.side_effect = RuntimeError("always fail")
        mgr._connect_with_retry()
        assert mgr._health == FeedHealth.DEGRADED

    def test_successful_connect_sets_healthy(self):
        mgr, client = _make_manager()
        client.connect.return_value = None
        mgr._connect_with_retry()
        assert mgr._health == FeedHealth.HEALTHY
        assert mgr._reconnect_count >= 1

    def test_stop_event_aborts_retry(self):
        cfg = SubscriptionManagerConfig(
            max_reconnect_attempts=0,  # infinite
            reconnect_backoff_base_sec=0.001,
        )
        mgr, client = _make_manager(config=cfg)
        # fail first attempt then stop
        call_count = [0]
        def _fail_first(symbols):
            call_count[0] += 1
            if call_count[0] == 1:
                mgr._stop_event.set()
                raise RuntimeError("fail")
        client.connect.side_effect = _fail_first
        mgr._connect_with_retry()
        # Should have stopped after first failure + stop event
        assert call_count[0] == 1


# ══════════════════════════════════════════════════════════════
# Tests: health callback
# ══════════════════════════════════════════════════════════════

class TestHealthCallback:

    def test_health_callback_fired_on_change(self):
        mgr, client = _make_manager()
        callback = MagicMock()
        mgr.set_health_callback(callback)
        mgr._set_health(FeedHealth.HEALTHY, detail="test")
        callback.assert_called_once()

    def test_health_callback_not_fired_if_same_state(self):
        mgr, client = _make_manager()
        mgr._health = FeedHealth.HEALTHY
        callback = MagicMock()
        mgr.set_health_callback(callback)
        mgr._set_health(FeedHealth.HEALTHY)
        callback.assert_not_called()

    def test_health_callback_exception_handled(self):
        mgr, client = _make_manager()
        mgr.set_health_callback(MagicMock(side_effect=RuntimeError("cb fail")))
        mgr._set_health(FeedHealth.HEALTHY)  # Should not raise
