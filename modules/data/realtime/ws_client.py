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
import asyncio
import gzip
import json
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Callable, Optional

import aiohttp
import ccxt

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


class HtxMarketWsClient(ExchangeWsClient):
    """HTX 公共行情 WebSocket 客户端。"""

    DEFAULT_WS_URL = "wss://api.huobi.pro/ws"
    _KNOWN_QUOTES = ("usdt", "usdc", "btc", "eth", "ht", "husd")

    def __init__(self, config: WsClientConfig) -> None:
        super().__init__(config)
        self._lock = threading.RLock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._connect_error: Optional[Exception] = None
        self._last_sequence_by_symbol: dict[str, int] = {}
        self._symbol_map: dict[str, str] = {}

    def connect(self, symbols: list[str]) -> None:
        """建立 HTX 公共行情 WebSocket 连接。"""
        if self._thread is not None and self._thread.is_alive():
            if self.state == WsConnectionState.CONNECTED and self._loop is not None:
                for symbol in symbols:
                    self.subscribe(symbol)
                return
            self.disconnect()

        with self._lock:
            self._subscribed = set()
            for symbol in symbols:
                self._register_symbol(symbol)
            self._ready_event.clear()
            self._connect_error = None
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                daemon=True,
                name=f"htx-ws-{self.config.exchange}",
            )
            self._thread.start()

        timeout = max(self.config.heartbeat_interval_sec, 5.0)
        if not self._ready_event.wait(timeout=timeout):
            self.disconnect()
            raise TimeoutError(f"HTX WebSocket 连接超时: {timeout}s")
        if self._connect_error is not None:
            error = self._connect_error
            self.disconnect()
            raise error

    def disconnect(self) -> None:
        """断开 HTX WebSocket 连接。"""
        self._stop_event.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._async_close(), loop)
            try:
                future.result(timeout=5)
            except Exception:
                if not self._stop_event.is_set():
                    log.debug("[WsClient] HTX 异步关闭超时或失败", exc_info=True)

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

        self._thread = None
        self._loop = None
        self._session = None
        self._ws = None
        self._set_state(WsConnectionState.DISCONNECTED)

    def subscribe(self, symbol: str) -> None:
        """追加订阅指定交易对。"""
        with self._lock:
            self._register_symbol(symbol)

        loop = self._loop
        if loop is not None and loop.is_running() and self._ws is not None:
            future = asyncio.run_coroutine_threadsafe(
                self._async_subscribe_symbol(symbol),
                loop,
            )
            future.result(timeout=5)

    def unsubscribe(self, symbol: str) -> None:
        """取消订阅指定交易对。"""
        with self._lock:
            self._subscribed.discard(symbol)

        loop = self._loop
        if loop is not None and loop.is_running() and self._ws is not None:
            future = asyncio.run_coroutine_threadsafe(
                self._async_unsubscribe_symbol(symbol),
                loop,
            )
            future.result(timeout=5)

    def request_snapshot(self, symbol: str) -> Optional[OrderBookDelta]:
        """通过 CCXT REST 拉取 HTX 订单簿快照。"""
        exchange_class = getattr(ccxt, self.config.exchange, None)
        if exchange_class is None:
            log.warning("[WsClient] CCXT 不支持快照恢复: exchange={}", self.config.exchange)
            return None

        exchange = exchange_class({"enableRateLimit": True})
        try:
            order_book = exchange.fetch_order_book(symbol, limit=self.config.depth_levels)
        except Exception as exc:  # noqa: BLE001
            log.warning("[WsClient] 快照拉取失败: symbol={} error={}", symbol, exc)
            return None
        finally:
            if hasattr(exchange, "close"):
                try:
                    exchange.close()
                except Exception:  # noqa: BLE001
                    pass

        bids = [
            DepthLevel(price=float(level[0]), size=float(level[1]))
            for level in order_book.get("bids", [])[: self.config.depth_levels]
        ]
        asks = [
            DepthLevel(price=float(level[0]), size=float(level[1]))
            for level in order_book.get("asks", [])[: self.config.depth_levels]
        ]
        if not bids or not asks:
            return None

        ts_ms = order_book.get("timestamp") or order_book.get("ts")
        exchange_ts = self._millis_to_datetime(ts_ms)
        sequence_id = int(order_book.get("nonce") or ts_ms or time.time() * 1000)
        prev_sequence_id = self._last_sequence_by_symbol.get(symbol, sequence_id - 1)
        self._last_sequence_by_symbol[symbol] = sequence_id

        return OrderBookDelta(
            symbol=symbol,
            exchange=self.config.exchange,
            sequence_id=sequence_id,
            prev_sequence_id=prev_sequence_id,
            bid_updates=bids,
            ask_updates=asks,
            received_at=datetime.now(tz=timezone.utc),
            exchange_ts=exchange_ts,
            is_snapshot=True,
            debug_payload={"source": "ccxt_rest_snapshot"},
        )

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_client())
        finally:
            pending = [task for task in asyncio.all_tasks(self._loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _run_client(self) -> None:
        ws_url = self.config.ws_url or self.DEFAULT_WS_URL
        self._set_state(WsConnectionState.CONNECTING)
        try:
            async with aiohttp.ClientSession() as session:
                self._session = session
                async with session.ws_connect(
                    ws_url,
                    heartbeat=self.config.heartbeat_interval_sec,
                    autoping=True,
                ) as websocket:
                    self._ws = websocket
                    for symbol in self.subscribed_symbols:
                        await self._async_subscribe_symbol(symbol)
                    self._set_state(WsConnectionState.CONNECTED)
                    self._update_heartbeat()
                    self._ready_event.set()

                    while not self._stop_event.is_set():
                        try:
                            message = await websocket.receive(
                                timeout=self.config.heartbeat_interval_sec,
                            )
                        except asyncio.TimeoutError:
                            continue

                        if message.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                            payload = self._decode_message(message.data)
                            if payload is None:
                                continue
                            await self._handle_message(payload)
                            continue

                        if message.type == aiohttp.WSMsgType.ERROR:
                            raise websocket.exception() or ConnectionError("HTX WebSocket error")

                        if message.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                        ):
                            if self._stop_event.is_set():
                                break
                            raise ConnectionError("HTX WebSocket closed")
        except Exception as exc:  # noqa: BLE001
            if not self._ready_event.is_set():
                self._connect_error = exc
                self._ready_event.set()
            if not self._stop_event.is_set():
                log.warning("[WsClient] HTX 行情连接异常: {}", exc)
        finally:
            self._ws = None
            self._session = None
            self._set_state(WsConnectionState.DISCONNECTED)
            if not self._ready_event.is_set():
                self._ready_event.set()

    async def _async_close(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _async_subscribe_symbol(self, symbol: str) -> None:
        if self._ws is None:
            return
        exchange_symbol = self._normalize_symbol(symbol)
        await self._ws.send_json(
            {"sub": f"market.{exchange_symbol}.depth.step0", "id": f"depth-{exchange_symbol}"}
        )
        await self._ws.send_json(
            {"sub": f"market.{exchange_symbol}.trade.detail", "id": f"trade-{exchange_symbol}"}
        )

    async def _async_unsubscribe_symbol(self, symbol: str) -> None:
        if self._ws is None:
            return
        exchange_symbol = self._normalize_symbol(symbol)
        await self._ws.send_json(
            {"unsub": f"market.{exchange_symbol}.depth.step0", "id": f"depth-{exchange_symbol}"}
        )
        await self._ws.send_json(
            {"unsub": f"market.{exchange_symbol}.trade.detail", "id": f"trade-{exchange_symbol}"}
        )

    async def _handle_message(self, payload: dict[str, Any]) -> None:
        if "ping" in payload:
            if self._ws is not None:
                await self._ws.send_json({"pong": payload["ping"]})
            self._update_heartbeat()
            return

        if payload.get("op") == "ping":
            if self._ws is not None:
                await self._ws.send_json({"op": "pong", "ts": payload.get("ts")})
            self._update_heartbeat()
            return

        if payload.get("status") == "ok":
            self._update_heartbeat()
            return

        channel = payload.get("ch", "")
        if ".depth." in channel:
            delta = self._parse_depth_message(payload)
            if delta is not None:
                self._update_heartbeat()
                self._emit_depth(delta)
            return

        if ".trade.detail" in channel:
            ticks = self._parse_trade_message(payload)
            for tick in ticks:
                self._update_heartbeat()
                self._emit_trade(tick)

    def _parse_depth_message(self, payload: dict[str, Any]) -> Optional[OrderBookDelta]:
        channel = str(payload.get("ch", ""))
        tick = payload.get("tick") or {}
        symbol = self._channel_to_symbol(channel)
        bids = [
            DepthLevel(price=float(level[0]), size=float(level[1]))
            for level in tick.get("bids", [])[: self.config.depth_levels]
        ]
        asks = [
            DepthLevel(price=float(level[0]), size=float(level[1]))
            for level in tick.get("asks", [])[: self.config.depth_levels]
        ]
        if not symbol or not bids or not asks:
            return None

        sequence_id = int(
            tick.get("seqNum")
            or tick.get("version")
            or payload.get("ts")
            or time.time() * 1000
        )
        prev_sequence_id = self._last_sequence_by_symbol.get(symbol, sequence_id - 1)
        self._last_sequence_by_symbol[symbol] = sequence_id

        return OrderBookDelta(
            symbol=symbol,
            exchange=self.config.exchange,
            sequence_id=sequence_id,
            prev_sequence_id=prev_sequence_id,
            bid_updates=bids,
            ask_updates=asks,
            received_at=datetime.now(tz=timezone.utc),
            exchange_ts=self._millis_to_datetime(tick.get("ts") or payload.get("ts")),
            is_snapshot=True,
            debug_payload={"channel": channel, "source": "htx_ws"},
        )

    def _parse_trade_message(self, payload: dict[str, Any]) -> list[TradeTick]:
        channel = str(payload.get("ch", ""))
        tick = payload.get("tick") or {}
        symbol = self._channel_to_symbol(channel)
        if not symbol:
            return []

        result: list[TradeTick] = []
        for trade in tick.get("data", []):
            price = float(trade.get("price", 0.0))
            size = float(trade.get("amount", 0.0))
            if price <= 0.0 or size <= 0.0:
                continue
            side_raw = str(trade.get("direction", "")).lower()
            if side_raw == "buy":
                side = TradeSide.BUY
            elif side_raw == "sell":
                side = TradeSide.SELL
            else:
                side = TradeSide.UNKNOWN

            trade_id = str(
                trade.get("tradeId")
                or trade.get("id")
                or f"htx-{tick.get('id', '0')}"
            )
            result.append(
                TradeTick(
                    symbol=symbol,
                    exchange=self.config.exchange,
                    trade_id=trade_id,
                    side=side,
                    price=price,
                    size=size,
                    notional=price * size,
                    received_at=datetime.now(tz=timezone.utc),
                    exchange_ts=self._millis_to_datetime(
                        trade.get("ts") or payload.get("ts")
                    ),
                    debug_payload={"channel": channel, "source": "htx_ws"},
                )
            )
        return result

    def _register_symbol(self, symbol: str) -> None:
        self._subscribed.add(symbol)
        self._symbol_map[self._normalize_symbol(symbol)] = symbol

    def _channel_to_symbol(self, channel: str) -> str:
        parts = channel.split(".")
        if len(parts) < 2:
            return ""
        exchange_symbol = parts[1].lower()
        known = self._symbol_map.get(exchange_symbol)
        if known is not None:
            return known
        return self._denormalize_symbol(exchange_symbol)

    def _normalize_symbol(self, symbol: str) -> str:
        return "".join(ch for ch in symbol.lower() if ch.isalnum())

    def _denormalize_symbol(self, exchange_symbol: str) -> str:
        for quote in self._KNOWN_QUOTES:
            if exchange_symbol.endswith(quote) and len(exchange_symbol) > len(quote):
                base = exchange_symbol[: -len(quote)]
                return f"{base.upper()}/{quote.upper()}"
        return exchange_symbol.upper()

    def _decode_message(self, raw: Any) -> Optional[dict[str, Any]]:
        if isinstance(raw, str):
            text = raw
        elif isinstance(raw, (bytes, bytearray)):
            try:
                text = gzip.decompress(bytes(raw)).decode("utf-8")
            except OSError:
                text = bytes(raw).decode("utf-8")
        else:
            return None

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.debug("[WsClient] HTX 消息解析失败: {}", text)
            return None

    def _millis_to_datetime(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None


def create_exchange_ws_client(
    provider: str,
    ws_config: WsClientConfig,
    *,
    mock_price_drift: float = 0.0008,
    mock_seed: int = 42,
) -> ExchangeWsClient:
    """按 provider 构建统一的实时行情客户端。"""
    normalized = (provider or "mock").strip().lower()

    if normalized == "htx":
        if ws_config.exchange != "htx":
            log.warning(
                "[WsClient] provider=htx 与 exchange={} 不匹配，回退 MockWsClient",
                ws_config.exchange,
            )
        else:
            return HtxMarketWsClient(ws_config)

    if normalized != "mock":
        log.warning(
            "[WsClient] 未知 realtime provider={}，回退 MockWsClient",
            normalized,
        )

    return MockWsClient(
        MockWsClientConfig(
            base_config=ws_config,
            price_drift=mock_price_drift,
            seed=mock_seed,
        )
    )
