"""
modules/data/realtime/ws_client.py — 交易所 WebSocket 适配层

设计说明：
- 定义统一的 WebSocket 客户端抽象基类 ExchangeWsClient
- 每个交易所实现一个子类，只在 _parse_depth_update / _parse_trade 中处理原始格式
- 解耦要求：原始包解析只能在此文件及其子类中进行
- MockWsClient：纯内存模拟，供测试和回放使用，不依赖任何网络 IO
- 事件回调：depth_callback(delta: OrderBookDelta) / trade_callback(tick: TradeTick)
- 连接状态机：DISCONNECTED → CONNECTING → CONNECTED → RECOVERING → DISCONNECTED

日志标签：[WsClient]
"""

from __future__ import annotations

import abc
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Callable, Optional

from core.logger import get_logger
from modules.data.realtime.orderbook_types import (
    DepthLevel,
    GapStatus,
    OrderBookDelta,
    TradeSide,
    TradeTick,
)

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、连接状态
# ══════════════════════════════════════════════════════════════

class WsConnectionState(str, Enum):
    """WebSocket 连接状态机状态。"""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECOVERING = "recovering"  # 序列号缺口，正在回补


# ══════════════════════════════════════════════════════════════
# 二、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class WsClientConfig:
    """
    WebSocket 客户端配置。

    Attributes:
        exchange:             交易所名称
        reconnect_delay_sec:  断开后重连等待时间（秒）
        max_reconnect_attempts: 最大重连次数（0 = 无限）
        heartbeat_interval_sec: 心跳检测间隔（秒）
        heartbeat_timeout_sec:  心跳超时判定时间（秒，超过则认为断流）
        depth_levels:         订单簿深度档位数量（top-N）
        ws_url:               WebSocket 服务地址（None = 交易所默认）
    """

    exchange: str = "mock"
    reconnect_delay_sec: float = 2.0
    max_reconnect_attempts: int = 10
    heartbeat_interval_sec: float = 10.0
    heartbeat_timeout_sec: float = 30.0
    depth_levels: int = 20
    ws_url: Optional[str] = None


# ══════════════════════════════════════════════════════════════
# 三、抽象基类
# ══════════════════════════════════════════════════════════════

class ExchangeWsClient(abc.ABC):
    """
    交易所 WebSocket 客户端抽象基类。

    子类只需实现 _parse_depth_update / _parse_trade 和连接管理方法。
    业务层通过 set_depth_callback / set_trade_callback 注册回调。

    接口：
        connect(symbol)         → 建立 WebSocket 连接并订阅
        disconnect()            → 断开连接并清理资源
        subscribe(symbol)       → 在已连接时追加订阅
        unsubscribe(symbol)     → 取消订阅
        set_depth_callback(fn)  → 注册订单簿增量回调
        set_trade_callback(fn)  → 注册成交流回调
        state                   → 当前连接状态
        subscribed_symbols      → 已订阅交易对列表
    """

    def __init__(self, config: WsClientConfig) -> None:
        self.config = config
        self._state: WsConnectionState = WsConnectionState.DISCONNECTED
        self._subscribed: set[str] = set()
        self._depth_callback: Optional[Callable[[OrderBookDelta], None]] = None
        self._trade_callback: Optional[Callable[[TradeTick], None]] = None
        self._reconnect_count: int = 0
        self._last_heartbeat_at: Optional[datetime] = None

        log.info(
            "[WsClient] 初始化完成: exchange={} depth_levels={}",
            config.exchange,
            config.depth_levels,
        )

    # ──────────────────────────────────────────────────────────
    # 公开接口
    # ──────────────────────────────────────────────────────────

    @property
    def state(self) -> WsConnectionState:
        return self._state

    @property
    def subscribed_symbols(self) -> list[str]:
        return sorted(self._subscribed)

    def set_depth_callback(
        self, callback: Callable[[OrderBookDelta], None]
    ) -> None:
        """注册订单簿增量更新回调。"""
        self._depth_callback = callback
        log.debug("[WsClient] depth_callback 已注册: {}", callback.__qualname__)

    def set_trade_callback(
        self, callback: Callable[[TradeTick], None]
    ) -> None:
        """注册成交流回调。"""
        self._trade_callback = callback
        log.debug("[WsClient] trade_callback 已注册: {}", callback.__qualname__)

    @abc.abstractmethod
    def connect(self, symbols: list[str]) -> None:
        """
        建立 WebSocket 连接并订阅指定交易对列表。

        Args:
            symbols: 要订阅的交易对列表，如 ["BTC/USDT", "ETH/USDT"]
        """

    @abc.abstractmethod
    def disconnect(self) -> None:
        """断开连接并释放资源。"""

    @abc.abstractmethod
    def subscribe(self, symbol: str) -> None:
        """在已连接状态下追加订阅新交易对。"""

    @abc.abstractmethod
    def unsubscribe(self, symbol: str) -> None:
        """取消订阅指定交易对。"""

    @abc.abstractmethod
    def request_snapshot(self, symbol: str) -> Optional[OrderBookDelta]:
        """
        通过 REST API 拉取全量快照（用于序列缺口回补）。
        返回一个 is_snapshot=True 的 OrderBookDelta。
        """

    # ──────────────────────────────────────────────────────────
    # 内部工具（子类可选覆盖）
    # ──────────────────────────────────────────────────────────

    def _emit_depth(self, delta: OrderBookDelta) -> None:
        """向上层发布一个订单簿增量包（子类解析完成后调用）。"""
        if self._depth_callback is not None:
            try:
                self._depth_callback(delta)
            except Exception:
                log.exception(
                    "[WsClient] depth_callback 异常: symbol={} seq={}",
                    delta.symbol,
                    delta.sequence_id,
                )

    def _emit_trade(self, tick: TradeTick) -> None:
        """向上层发布一笔成交记录（子类解析完成后调用）。"""
        if self._trade_callback is not None:
            try:
                self._trade_callback(tick)
            except Exception:
                log.exception(
                    "[WsClient] trade_callback 异常: symbol={} trade_id={}",
                    tick.symbol,
                    tick.trade_id,
                )

    def _set_state(self, new_state: WsConnectionState) -> None:
        if self._state != new_state:
            log.info(
                "[WsClient] 状态变更: {} → {} exchange={}",
                self._state.value,
                new_state.value,
                self.config.exchange,
            )
            self._state = new_state

    def _update_heartbeat(self) -> None:
        self._last_heartbeat_at = datetime.now(tz=timezone.utc)

    def is_heartbeat_alive(self) -> bool:
        """检查心跳是否存活（上次心跳在超时时间内）。"""
        if self._last_heartbeat_at is None:
            return False
        elapsed = (
            datetime.now(tz=timezone.utc) - self._last_heartbeat_at
        ).total_seconds()
        return elapsed < self.config.heartbeat_timeout_sec

    def diagnostics(self) -> dict[str, Any]:
        """返回当前客户端诊断信息。"""
        return {
            "exchange": self.config.exchange,
            "state": self._state.value,
            "subscribed_symbols": self.subscribed_symbols,
            "reconnect_count": self._reconnect_count,
            "last_heartbeat_at": (
                self._last_heartbeat_at.isoformat()
                if self._last_heartbeat_at
                else None
            ),
            "heartbeat_alive": self.is_heartbeat_alive(),
        }


# ══════════════════════════════════════════════════════════════
# 四、MockWsClient — 纯内存模拟（供测试与 replay 使用）
# ══════════════════════════════════════════════════════════════

@dataclass
class MockWsClientConfig:
    """
    MockWsClient 配置。

    Attributes:
        base_config:          基础 WsClientConfig
        tick_interval_sec:    每次 tick 推送间隔（秒）；0 = 不自动推送
        price_drift:          价格随机游走步长（相对比例，如 0.001 = 0.1%）
        fail_on_connect:      模拟连接失败（测试重连逻辑）
        gap_at_sequence:      在此序列号强制插入 gap（0 = 不插入）
        snapshot_delay_sec:   请求快照的模拟延迟
        seed:                 随机种子（保证测试可复现）
    """

    base_config: WsClientConfig = field(default_factory=lambda: WsClientConfig(exchange="mock"))
    tick_interval_sec: float = 0.0
    price_drift: float = 0.001
    fail_on_connect: bool = False
    gap_at_sequence: int = 0
    snapshot_delay_sec: float = 0.0
    seed: int = 42


class MockWsClient(ExchangeWsClient):
    """
    纯内存 WebSocket 客户端模拟，用于测试和 replay。

    特性：
    - 不依赖任何网络 IO
    - 支持主动 push_depth / push_trade（测试时注入事件）
    - 支持模拟序列缺口（gap_at_sequence）
    - 支持模拟连接失败（fail_on_connect）
    - 线程安全（使用 threading.Lock）
    """

    def __init__(self, config: MockWsClientConfig = MockWsClientConfig()) -> None:
        super().__init__(config.base_config)
        self._mock_config = config
        self._rng = random.Random(config.seed)
        self._lock = threading.Lock()
        self._sequence: int = 0
        self._mid_prices: dict[str, float] = {}  # symbol -> 当前 mid price
        self._snapshot_store: dict[str, OrderBookDelta] = {}

        log.info(
            "[WsClient] MockWsClient 初始化: seed={} gap_at_seq={}",
            config.seed,
            config.gap_at_sequence,
        )

    # ──────────────────────────────────────────────────────────
    # 抽象方法实现
    # ──────────────────────────────────────────────────────────

    def connect(self, symbols: list[str]) -> None:
        """模拟建立 WebSocket 连接。"""
        if self._mock_config.fail_on_connect:
            log.warning("[WsClient] 模拟连接失败（fail_on_connect=True）")
            self._set_state(WsConnectionState.DISCONNECTED)
            raise ConnectionError("MockWsClient: 模拟连接失败")

        self._set_state(WsConnectionState.CONNECTING)
        log.info("[WsClient] 模拟连接建立: exchange={}", self.config.exchange)

        for symbol in symbols:
            self._subscribe_internal(symbol)

        self._set_state(WsConnectionState.CONNECTED)
        self._update_heartbeat()
        log.info(
            "[WsClient] 连接完成: exchange={} symbols={}",
            self.config.exchange,
            self.subscribed_symbols,
        )

    def disconnect(self) -> None:
        """模拟断开连接。"""
        self._set_state(WsConnectionState.DISCONNECTED)
        log.info("[WsClient] 连接已断开: exchange={}", self.config.exchange)

    def subscribe(self, symbol: str) -> None:
        """追加订阅新交易对。"""
        with self._lock:
            self._subscribe_internal(symbol)
        log.info("[WsClient] 追加订阅: symbol={}", symbol)

    def unsubscribe(self, symbol: str) -> None:
        """取消订阅。"""
        with self._lock:
            self._subscribed.discard(symbol)
            self._mid_prices.pop(symbol, None)
        log.info("[WsClient] 取消订阅: symbol={}", symbol)

    def request_snapshot(self, symbol: str) -> Optional[OrderBookDelta]:
        """返回存储的快照（模拟 REST 快照回补）。"""
        if self._mock_config.snapshot_delay_sec > 0:
            time.sleep(self._mock_config.snapshot_delay_sec)

        snapshot = self._snapshot_store.get(symbol)
        if snapshot is None:
            log.warning("[WsClient] 快照不存在: symbol={}", symbol)
            return None

        log.info("[WsClient] 快照回补完成: symbol={} seq={}", symbol, snapshot.sequence_id)
        return snapshot

    # ──────────────────────────────────────────────────────────
    # 测试接口：主动注入事件
    # ──────────────────────────────────────────────────────────

    def push_depth(self, delta: OrderBookDelta) -> None:
        """
        测试专用：直接注入一个订单簿增量包（触发 depth_callback）。

        Args:
            delta: 要注入的增量包
        """
        log.debug(
            "[WsClient] push_depth: symbol={} seq={} is_snapshot={}",
            delta.symbol,
            delta.sequence_id,
            delta.is_snapshot,
        )
        self._update_heartbeat()
        self._emit_depth(delta)

    def push_trade(self, tick: TradeTick) -> None:
        """
        测试专用：直接注入一笔成交记录（触发 trade_callback）。

        Args:
            tick: 要注入的成交记录
        """
        log.debug(
            "[WsClient] push_trade: symbol={} trade_id={} price={}",
            tick.symbol,
            tick.trade_id,
            tick.price,
        )
        self._update_heartbeat()
        self._emit_trade(tick)

    def register_snapshot(self, symbol: str, snapshot: OrderBookDelta) -> None:
        """注册快照用于 request_snapshot() 回补（测试用）。"""
        self._snapshot_store[symbol] = snapshot
        log.debug("[WsClient] 快照已注册: symbol={} seq={}", symbol, snapshot.sequence_id)

    def generate_depth_update(
        self,
        symbol: str,
        force_gap: bool = False,
    ) -> Optional[OrderBookDelta]:
        """
        生成一个随机订单簿增量包（不自动 push，由调用者决定是否 push）。

        Args:
            symbol:     交易对
            force_gap:  是否强制制造序列缺口（跳过 sequence_id）

        Returns:
            生成的 OrderBookDelta，或 None（symbol 未订阅）
        """
        with self._lock:
            if symbol not in self._subscribed:
                return None

            mid = self._mid_prices.get(symbol, 50000.0)
            drift = self._rng.uniform(-self._mock_config.price_drift, self._mock_config.price_drift)
            mid = mid * (1 + drift)
            self._mid_prices[symbol] = mid

            prev_seq = self._sequence
            if force_gap:
                # 制造序列缺口：跳过一个序列号
                self._sequence += 2
                log.warning(
                    "[WsClient] 模拟序列缺口: symbol={} prev={} curr={}",
                    symbol,
                    prev_seq,
                    self._sequence,
                )
            else:
                self._sequence += 1

            half_spread = mid * 0.0001
            now = datetime.now(tz=timezone.utc)

            bid_updates = [
                DepthLevel(price=round(mid - half_spread - i * 0.5, 2), size=round(self._rng.uniform(0.5, 5.0), 4))
                for i in range(5)
            ]
            ask_updates = [
                DepthLevel(price=round(mid + half_spread + i * 0.5, 2), size=round(self._rng.uniform(0.5, 5.0), 4))
                for i in range(5)
            ]

            return OrderBookDelta(
                symbol=symbol,
                exchange=self.config.exchange,
                sequence_id=self._sequence,
                prev_sequence_id=prev_seq,
                bid_updates=bid_updates,
                ask_updates=ask_updates,
                received_at=now,
                exchange_ts=now,
                debug_payload={"generated": True, "force_gap": force_gap},
            )

    def generate_trade(self, symbol: str) -> Optional[TradeTick]:
        """生成一笔随机成交记录（不自动 push）。"""
        with self._lock:
            if symbol not in self._subscribed:
                return None
            mid = self._mid_prices.get(symbol, 50000.0)
            price = mid * (1 + self._rng.uniform(-0.0005, 0.0005))
            size = round(self._rng.uniform(0.001, 0.5), 6)
            side = TradeSide.BUY if self._rng.random() > 0.5 else TradeSide.SELL
            now = datetime.now(tz=timezone.utc)
            self._sequence += 1
            return TradeTick(
                symbol=symbol,
                exchange=self.config.exchange,
                trade_id=f"mock-{self._sequence}",
                side=side,
                price=round(price, 2),
                size=size,
                notional=round(price * size, 4),
                received_at=now,
                exchange_ts=now,
                debug_payload={"generated": True},
            )

    # ──────────────────────────────────────────────────────────
    # 内部工具
    # ──────────────────────────────────────────────────────────

    def _subscribe_internal(self, symbol: str) -> None:
        """内部订阅处理（已持锁环境调用）。"""
        self._subscribed.add(symbol)
        if symbol not in self._mid_prices:
            self._mid_prices[symbol] = self._rng.uniform(40000.0, 60000.0)
        log.debug("[WsClient] symbol 已订阅: {}", symbol)
