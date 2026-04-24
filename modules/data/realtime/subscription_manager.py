"""
modules/data/realtime/subscription_manager.py — WebSocket 订阅与重连管理

设计说明：
- 管理 ExchangeWsClient 的订阅生命周期（连接、重连、心跳监测、去重订阅）
- 主动健康检查：定时检测 WsClient 心跳，断流时触发自动重连
- 重连策略：指数退避（Exponential Backoff），最大重连次数可配置
- 事件路由：将 depth / trade 回调透传到 DepthCacheRegistry 和 TradeCacheRegistry
- 状态机：STOPPED → RUNNING → RECONNECTING → RUNNING / STOPPED
- 健康降级：断流超时后发出 FeedHealthEvent（DEGRADED），恢复后发出 HEALTHY

日志标签：[SubscriptionManager]
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

from core.logger import get_logger
from modules.data.realtime.depth_cache import DepthCacheRegistry
from modules.data.realtime.orderbook_types import (
    OrderBookDelta,
    TradeTick,
)
from modules.data.realtime.trade_cache import TradeCacheRegistry
from modules.data.realtime.ws_client import (
    ExchangeWsClient,
    WsConnectionState,
)

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、健康状态
# ══════════════════════════════════════════════════════════════

class FeedHealth(str, Enum):
    """数据流健康状态。"""

    HEALTHY = "healthy"           # 正常运行，心跳正常
    DEGRADED = "degraded"         # 心跳超时或正在重连
    STOPPED = "stopped"           # 已停止


@dataclass(frozen=True)
class FeedHealthSnapshot:
    """数据流健康快照（用于上层监控和降级决策）。"""

    health: FeedHealth
    exchange: str
    subscribed_symbols: list[str]
    reconnect_count: int
    last_heartbeat_at: Optional[datetime]
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    detail: str = ""


# ══════════════════════════════════════════════════════════════
# 二、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class SubscriptionManagerConfig:
    """
    SubscriptionManager 配置。

    Attributes:
        health_check_interval_sec:  健康检查间隔（秒）
        reconnect_backoff_base_sec: 重连退避基础时间（秒，每次翻倍）
        reconnect_backoff_max_sec:  重连退避最大等待时间（秒）
        max_reconnect_attempts:     最大自动重连次数（0 = 无限）
        heartbeat_timeout_sec:      心跳超时判断阈值（秒）
    """

    health_check_interval_sec: float = 5.0
    reconnect_backoff_base_sec: float = 1.0
    reconnect_backoff_max_sec: float = 30.0
    max_reconnect_attempts: int = 10
    heartbeat_timeout_sec: float = 30.0


# ══════════════════════════════════════════════════════════════
# 三、SubscriptionManager 主体
# ══════════════════════════════════════════════════════════════

class SubscriptionManager:
    """
    WebSocket 订阅与重连管理器。

    负责：
    1. 启动 WsClient 连接并订阅初始交易对列表
    2. 将 depth / trade 回调路由到 DepthCacheRegistry / TradeCacheRegistry
    3. 定时健康检查 → 心跳超时触发自动重连
    4. 指数退避重连策略
    5. 提供 health snapshot 供上层降级判断
    6. 可选注册外部健康变化回调（on_health_change）

    Args:
        ws_client:         ExchangeWsClient 实例
        depth_registry:    DepthCacheRegistry
        trade_registry:    TradeCacheRegistry
        config:            SubscriptionManagerConfig
    """

    def __init__(
        self,
        ws_client: ExchangeWsClient,
        depth_registry: DepthCacheRegistry,
        trade_registry: TradeCacheRegistry,
        config: SubscriptionManagerConfig = SubscriptionManagerConfig(),
    ) -> None:
        self._client = ws_client
        self._depth_registry = depth_registry
        self._trade_registry = trade_registry
        self._config = config

        self._symbols: list[str] = []
        self._health: FeedHealth = FeedHealth.STOPPED
        self._reconnect_count: int = 0
        self._on_health_change: Optional[Callable[[FeedHealthSnapshot], None]] = None

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._health_thread: Optional[threading.Thread] = None

        # 注册回调
        ws_client.set_depth_callback(self._on_depth)
        ws_client.set_trade_callback(self._on_trade)

        log.info(
            "[SubscriptionManager] 初始化: exchange={}",
            ws_client.config.exchange,
        )

    # ──────────────────────────────────────────────────────────
    # 公开接口
    # ──────────────────────────────────────────────────────────

    def set_health_callback(
        self, callback: Callable[[FeedHealthSnapshot], None]
    ) -> None:
        """注册健康状态变化回调（每次 HEALTHY ↔ DEGRADED 时触发）。"""
        self._on_health_change = callback

    def start(self, symbols: list[str]) -> None:
        """
        启动管理器：连接 WsClient 并订阅交易对列表。

        Args:
            symbols: 要订阅的交易对列表
        """
        with self._lock:
            self._symbols = list(symbols)

        log.info(
            "[SubscriptionManager] 启动: symbols={} exchange={}",
            symbols,
            self._client.config.exchange,
        )

        self._connect_with_retry()

        # 启动健康检查后台线程
        self._stop_event.clear()
        self._health_thread = threading.Thread(
            target=self._health_check_loop, daemon=True
        )
        self._health_thread.start()

    def stop(self) -> None:
        """停止管理器并断开 WsClient。"""
        self._stop_event.set()
        try:
            self._client.disconnect()
        except Exception:
            log.exception("[SubscriptionManager] 断开连接时异常")
        self._set_health(FeedHealth.STOPPED, detail="手动停止")
        log.info("[SubscriptionManager] 已停止")

    def subscribe(self, symbol: str) -> None:
        """动态追加订阅新交易对。"""
        with self._lock:
            if symbol not in self._symbols:
                self._symbols.append(symbol)
        try:
            self._client.subscribe(symbol)
            log.info("[SubscriptionManager] 动态订阅: symbol={}", symbol)
        except Exception:
            log.exception("[SubscriptionManager] 动态订阅失败: symbol={}", symbol)

    def unsubscribe(self, symbol: str) -> None:
        """取消订阅。"""
        with self._lock:
            if symbol in self._symbols:
                self._symbols.remove(symbol)
        try:
            self._client.unsubscribe(symbol)
            log.info("[SubscriptionManager] 取消订阅: symbol={}", symbol)
        except Exception:
            log.exception("[SubscriptionManager] 取消订阅失败: symbol={}", symbol)

    def get_health(self) -> FeedHealthSnapshot:
        """获取当前健康快照。"""
        with self._lock:
            symbols = list(self._symbols)
        return FeedHealthSnapshot(
            health=self._health,
            exchange=self._client.config.exchange,
            subscribed_symbols=symbols,
            reconnect_count=self._reconnect_count,
            last_heartbeat_at=self._client._last_heartbeat_at,
        )

    def diagnostics(self) -> dict[str, Any]:
        snapshot = self.get_health()
        return {
            "health": snapshot.health.value,
            "exchange": snapshot.exchange,
            "subscribed_symbols": snapshot.subscribed_symbols,
            "reconnect_count": snapshot.reconnect_count,
            "last_heartbeat_at": (
                snapshot.last_heartbeat_at.isoformat()
                if snapshot.last_heartbeat_at
                else None
            ),
            "ws_state": self._client.state.value,
        }

    # ──────────────────────────────────────────────────────────
    # 内部回调路由
    # ──────────────────────────────────────────────────────────

    def _on_depth(self, delta: OrderBookDelta) -> None:
        """将订单簿增量包路由到 DepthCacheRegistry。"""
        try:
            snapshot = self._depth_registry.apply_delta(delta)
            if snapshot is None:
                log.debug(
                    "[SubscriptionManager] DepthCache 返回 None（gap 状态）: "
                    "symbol={} seq={}",
                    delta.symbol,
                    delta.sequence_id,
                )
        except Exception:
            log.exception(
                "[SubscriptionManager] depth 路由异常: symbol={}", delta.symbol
            )

    def _on_trade(self, tick: TradeTick) -> None:
        """将成交记录路由到 TradeCacheRegistry。"""
        try:
            self._trade_registry.add_tick(tick)
        except Exception:
            log.exception(
                "[SubscriptionManager] trade 路由异常: symbol={}", tick.symbol
            )

    # ──────────────────────────────────────────────────────────
    # 健康检查与重连
    # ──────────────────────────────────────────────────────────

    def _health_check_loop(self) -> None:
        """健康检查后台循环。"""
        log.info(
            "[SubscriptionManager] 健康检查线程启动: interval={}s",
            self._config.health_check_interval_sec,
        )

        while not self._stop_event.is_set():
            time.sleep(self._config.health_check_interval_sec)

            if self._stop_event.is_set():
                break

            self._check_and_recover()

    def _check_and_recover(self) -> None:
        """检查心跳状态，必要时触发重连。"""
        is_alive = self._client.is_heartbeat_alive()
        client_state = self._client.state

        if client_state == WsConnectionState.CONNECTED and is_alive:
            if self._health != FeedHealth.HEALTHY:
                self._set_health(FeedHealth.HEALTHY, detail="心跳正常")
            return

        # 心跳超时或连接断开
        log.warning(
            "[SubscriptionManager] 健康检查异常: state={} heartbeat_alive={} → 触发重连",
            client_state.value,
            is_alive,
        )
        self._set_health(FeedHealth.DEGRADED, detail=f"心跳超时或断连: state={client_state.value}")
        self._connect_with_retry()

    def _connect_with_retry(self) -> None:
        """带指数退避重连的连接逻辑。"""
        max_attempts = self._config.max_reconnect_attempts
        backoff = self._config.reconnect_backoff_base_sec

        attempt = 0
        while True:
            if self._stop_event.is_set():
                return

            if max_attempts > 0 and attempt >= max_attempts:
                log.error(
                    "[SubscriptionManager] 已达最大重连次数: max_attempts={}",
                    max_attempts,
                )
                self._set_health(FeedHealth.DEGRADED, detail="超过最大重连次数")
                return

            attempt += 1
            with self._lock:
                symbols = list(self._symbols)

            try:
                log.info(
                    "[SubscriptionManager] 尝试连接: attempt={} symbols={}",
                    attempt,
                    symbols,
                )
                self._client.connect(symbols)
                with self._lock:
                    self._reconnect_count += 1

                self._set_health(FeedHealth.HEALTHY, detail=f"连接成功 attempt={attempt}")
                log.info(
                    "[SubscriptionManager] 连接成功: exchange={} reconnect_count={}",
                    self._client.config.exchange,
                    self._reconnect_count,
                )
                return

            except Exception as e:
                log.warning(
                    "[SubscriptionManager] 连接失败: attempt={} error={} 等待 {}s 重试",
                    attempt,
                    str(e),
                    backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, self._config.reconnect_backoff_max_sec)

    def _set_health(self, new_health: FeedHealth, detail: str = "") -> None:
        """更新健康状态并触发回调。"""
        old_health = self._health
        self._health = new_health

        if old_health != new_health:
            log.info(
                "[SubscriptionManager] 健康状态变更: {} → {} detail={}",
                old_health.value,
                new_health.value,
                detail,
            )
            if self._on_health_change is not None:
                try:
                    snapshot = self.get_health()
                    self._on_health_change(snapshot)
                except Exception:
                    log.exception("[SubscriptionManager] 健康回调异常")
