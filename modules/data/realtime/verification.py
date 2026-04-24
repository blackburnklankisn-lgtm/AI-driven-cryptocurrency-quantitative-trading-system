"""
modules/data/realtime/verification.py — Phase 3 realtime feed 验证工具

用于在不启动完整 LiveTrader 的情况下，验证 provider -> ws client ->
SubscriptionManager -> DepthCacheRegistry / TradeCacheRegistry 这条链路。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from core.logger import get_logger
from modules.data.realtime.depth_cache import DepthCacheConfig, DepthCacheRegistry
from modules.data.realtime.subscription_manager import (
    SubscriptionManager,
    SubscriptionManagerConfig,
)
from modules.data.realtime.trade_cache import TradeCacheConfig, TradeCacheRegistry
from modules.data.realtime.ws_client import (
    ExchangeWsClient,
    MockWsClient,
    WsClientConfig,
    create_exchange_ws_client,
)

log = get_logger(__name__)


@dataclass(frozen=True)
class FeedVerificationSymbolStatus:
    symbol: str
    has_snapshot: bool
    trade_count: int
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None


@dataclass(frozen=True)
class FeedVerificationResult:
    success: bool
    provider: str
    exchange: str
    symbols: list[str]
    elapsed_sec: float
    health: str
    statuses: list[FeedVerificationSymbolStatus]
    error: str = ""


def verify_realtime_feed(
    *,
    provider: str,
    exchange: str,
    symbols: list[str],
    timeout_sec: float = 15.0,
    depth_levels: int = 20,
    reconnect_backoff_sec: float = 2.0,
    heartbeat_timeout_sec: float = 15.0,
    health_check_interval_sec: float = 5.0,
    ws_url: Optional[str] = None,
    mock_seed: int = 42,
    mock_price_drift: float = 0.0008,
    poll_interval_sec: float = 0.25,
) -> FeedVerificationResult:
    """验证指定 provider 的 realtime feed 能否产出快照和成交。"""
    start = time.monotonic()
    ws_config = WsClientConfig(
        exchange=exchange,
        reconnect_delay_sec=reconnect_backoff_sec,
        heartbeat_timeout_sec=heartbeat_timeout_sec,
        depth_levels=depth_levels,
        ws_url=ws_url,
    )
    client = create_exchange_ws_client(
        provider,
        ws_config,
        mock_price_drift=mock_price_drift,
        mock_seed=mock_seed,
    )
    depth_registry = DepthCacheRegistry(DepthCacheConfig(max_depth=depth_levels))
    trade_registry = TradeCacheRegistry(TradeCacheConfig())
    manager = SubscriptionManager(
        client,
        depth_registry,
        trade_registry,
        SubscriptionManagerConfig(
            health_check_interval_sec=health_check_interval_sec,
            reconnect_backoff_base_sec=reconnect_backoff_sec,
            heartbeat_timeout_sec=heartbeat_timeout_sec,
        ),
    )

    try:
        manager.start(symbols)
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - start
        return FeedVerificationResult(
            success=False,
            provider=provider,
            exchange=exchange,
            symbols=list(symbols),
            elapsed_sec=elapsed,
            health="stopped",
            statuses=[],
            error=str(exc),
        )

    try:
        deadline = start + timeout_sec
        while time.monotonic() < deadline:
            if isinstance(client, MockWsClient):
                _pump_mock_client(client, symbols)

            statuses = _collect_statuses(depth_registry, trade_registry, exchange, symbols)
            if statuses and all(status.has_snapshot and status.trade_count > 0 for status in statuses):
                elapsed = time.monotonic() - start
                health = manager.get_health().health.value
                return FeedVerificationResult(
                    success=True,
                    provider=provider,
                    exchange=exchange,
                    symbols=list(symbols),
                    elapsed_sec=elapsed,
                    health=health,
                    statuses=statuses,
                )

            time.sleep(poll_interval_sec)

        statuses = _collect_statuses(depth_registry, trade_registry, exchange, symbols)
        elapsed = time.monotonic() - start
        health = manager.get_health().health.value
        return FeedVerificationResult(
            success=False,
            provider=provider,
            exchange=exchange,
            symbols=list(symbols),
            elapsed_sec=elapsed,
            health=health,
            statuses=statuses,
            error="timeout waiting for snapshot/trade data",
        )
    finally:
        try:
            manager.stop()
        except Exception:  # noqa: BLE001
            log.debug("[RealtimeVerify] manager.stop() failed", exc_info=True)


def _pump_mock_client(client: MockWsClient, symbols: list[str]) -> None:
    for symbol in symbols:
        depth_delta = client.generate_depth_update(symbol)
        if depth_delta is not None:
            client.push_depth(depth_delta)

        trade_tick = client.generate_trade(symbol)
        if trade_tick is not None:
            client.push_trade(trade_tick)


def _collect_statuses(
    depth_registry: DepthCacheRegistry,
    trade_registry: TradeCacheRegistry,
    exchange: str,
    symbols: list[str],
) -> list[FeedVerificationSymbolStatus]:
    statuses: list[FeedVerificationSymbolStatus] = []
    for symbol in symbols:
        cache = depth_registry.get(symbol, exchange)
        snapshot = cache.get_snapshot() if cache is not None else None
        trade_stats = trade_registry.get_stats(symbol, exchange)
        statuses.append(
            FeedVerificationSymbolStatus(
                symbol=symbol,
                has_snapshot=snapshot is not None,
                trade_count=0 if trade_stats is None else int(trade_stats.trade_count),
                best_bid=None if snapshot is None else snapshot.best_bid,
                best_ask=None if snapshot is None else snapshot.best_ask,
            )
        )
    return statuses