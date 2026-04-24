"""
modules/data/realtime/replay_feed.py — 订单簿与成交流回放引擎

设计说明：
- 与真实实时流保持同一事件契约（OrderBookDelta / TradeTick），禁止"训练专用假接口"
- 支持按录制顺序（received_at 时间戳）回放 order book + trade tick
- 支持速度倍数控制（1x / 10x / 100x / ∞ = 无延迟）
- 支持时间窗口过滤（start_time / end_time）
- 支持 loop 模式（回放完毕后重新开始）
- 回放状态机：IDLE → RUNNING → PAUSED → STOPPED
- 回调接口与 ExchangeWsClient 完全相同（set_depth_callback / set_trade_callback）

核心解耦要求：
1. ReplayFeed 只产出 OrderBookDelta 和 TradeTick，与 DepthCache + TradeCache 完全解耦
2. 下游消费者无需感知数据来自 live 还是 replay
3. 回放速度控制通过 playback_speed 倍数实现，不修改时间戳

日志标签：[ReplayFeed]
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterator, Optional, Union

from core.logger import get_logger
from modules.data.realtime.orderbook_types import (
    OrderBookDelta,
    TradeTick,
)

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、事件容器（统一的回放事件类型）
# ══════════════════════════════════════════════════════════════

# 回放事件 = 订单簿增量包 或 成交记录
ReplayEvent = Union[OrderBookDelta, TradeTick]


class ReplayState(str, Enum):
    """回放状态机状态。"""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"


# ══════════════════════════════════════════════════════════════
# 二、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class ReplayFeedConfig:
    """
    ReplayFeed 配置。

    Attributes:
        playback_speed:     回放速度倍数（1.0 = 实时，0 = 无延迟）
        loop:               回放完毕后是否循环
        start_time:         回放起始时间过滤（None = 从头开始）
        end_time:           回放结束时间过滤（None = 到末尾）
        emit_depth:         是否触发 depth 回调
        emit_trade:         是否触发 trade 回调
        log_every_n_events: 每 N 个事件打印进度日志（0 = 不打印）
    """

    playback_speed: float = 1.0
    loop: bool = False
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    emit_depth: bool = True
    emit_trade: bool = True
    log_every_n_events: int = 100


# ══════════════════════════════════════════════════════════════
# 三、ReplayFeed 主体
# ══════════════════════════════════════════════════════════════

class ReplayFeed:
    """
    订单簿与成交流回放引擎。

    使用方式：
        feed = ReplayFeed(config)
        feed.load_events(events)          # 加载历史事件序列
        feed.set_depth_callback(fn)       # 注册订单簿增量回调
        feed.set_trade_callback(fn)       # 注册成交流回调
        feed.start()                      # 开始回放（异步线程）
        feed.wait_until_done()            # 阻塞等待回放完成
        feed.stop()                       # 停止回放

    事件契约：与 ExchangeWsClient 完全相同，消费者无感知。
    """

    def __init__(self, config: ReplayFeedConfig = ReplayFeedConfig()) -> None:
        self.config = config
        self._events: list[ReplayEvent] = []
        self._depth_callback: Optional[Callable[[OrderBookDelta], None]] = None
        self._trade_callback: Optional[Callable[[TradeTick], None]] = None
        self._state: ReplayState = ReplayState.IDLE
        self._lock = threading.Lock()
        self._done_event = threading.Event()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # 初始不 paused
        self._thread: Optional[threading.Thread] = None
        self._emit_count: int = 0
        self._loop_count: int = 0

        log.info(
            "[ReplayFeed] 初始化: speed={}x loop={} emit_depth={} emit_trade={}",
            config.playback_speed,
            config.loop,
            config.emit_depth,
            config.emit_trade,
        )

    # ──────────────────────────────────────────────────────────
    # 数据加载接口
    # ──────────────────────────────────────────────────────────

    def load_events(self, events: list[ReplayEvent]) -> None:
        """
        加载回放事件序列（按 received_at 自动排序）。

        Args:
            events: OrderBookDelta 和 TradeTick 的混合列表
        """
        filtered = self._filter_by_time(events)
        self._events = sorted(filtered, key=lambda e: e.received_at)
        log.info(
            "[ReplayFeed] 事件加载完成: total={} filtered={} time_range=[{}, {}]",
            len(events),
            len(self._events),
            self._events[0].received_at.isoformat() if self._events else "N/A",
            self._events[-1].received_at.isoformat() if self._events else "N/A",
        )

    def load_depth_events(self, deltas: list[OrderBookDelta]) -> None:
        """仅加载订单簿增量事件。"""
        self.load_events(deltas)  # type: ignore[arg-type]

    def load_trade_events(self, ticks: list[TradeTick]) -> None:
        """仅加载成交流事件。"""
        self.load_events(ticks)  # type: ignore[arg-type]

    # ──────────────────────────────────────────────────────────
    # 回调注册（与 ExchangeWsClient 相同接口）
    # ──────────────────────────────────────────────────────────

    def set_depth_callback(
        self, callback: Callable[[OrderBookDelta], None]
    ) -> None:
        """注册订单簿增量回调。"""
        self._depth_callback = callback
        log.debug("[ReplayFeed] depth_callback 已注册: {}", callback.__qualname__)

    def set_trade_callback(
        self, callback: Callable[[TradeTick], None]
    ) -> None:
        """注册成交流回调。"""
        self._trade_callback = callback
        log.debug("[ReplayFeed] trade_callback 已注册: {}", callback.__qualname__)

    # ──────────────────────────────────────────────────────────
    # 控制接口
    # ──────────────────────────────────────────────────────────

    def start(self, blocking: bool = False) -> None:
        """
        开始回放。

        Args:
            blocking: True = 阻塞当前线程直到回放完成；False = 后台线程异步运行
        """
        if not self._events:
            log.warning("[ReplayFeed] 事件列表为空，回放跳过")
            return

        with self._lock:
            if self._state == ReplayState.RUNNING:
                log.warning("[ReplayFeed] 已在运行，忽略 start() 调用")
                return
            self._state = ReplayState.RUNNING
            self._done_event.clear()
            self._stop_event.clear()
            self._pause_event.set()

        log.info(
            "[ReplayFeed] 开始回放: total_events={} speed={}x",
            len(self._events),
            self.config.playback_speed,
        )

        if blocking:
            self._run_loop()
        else:
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()

    def pause(self) -> None:
        """暂停回放（可通过 resume() 继续）。"""
        with self._lock:
            if self._state == ReplayState.RUNNING:
                self._state = ReplayState.PAUSED
                self._pause_event.clear()
                log.info("[ReplayFeed] 已暂停")

    def resume(self) -> None:
        """恢复暂停的回放。"""
        with self._lock:
            if self._state == ReplayState.PAUSED:
                self._state = ReplayState.RUNNING
                self._pause_event.set()
                log.info("[ReplayFeed] 已恢复")

    def stop(self) -> None:
        """停止回放（不可恢复，需重新 start）。"""
        with self._lock:
            self._state = ReplayState.STOPPED
            self._stop_event.set()
            self._pause_event.set()  # 唤醒 pause 状态
        log.info("[ReplayFeed] 已停止")

    def wait_until_done(self, timeout: Optional[float] = None) -> bool:
        """
        阻塞等待回放完成。

        Args:
            timeout: 超时时间（秒），None = 无限等待

        Returns:
            True = 正常完成；False = 超时
        """
        return self._done_event.wait(timeout=timeout)

    @property
    def state(self) -> ReplayState:
        with self._lock:
            return self._state

    @property
    def emit_count(self) -> int:
        return self._emit_count

    @property
    def loop_count(self) -> int:
        return self._loop_count

    def diagnostics(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "total_events": len(self._events),
            "emit_count": self._emit_count,
            "loop_count": self._loop_count,
            "playback_speed": self.config.playback_speed,
            "loop": self.config.loop,
        }

    # ──────────────────────────────────────────────────────────
    # 内部回放逻辑
    # ──────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """回放主循环（在后台线程中运行）。"""
        try:
            while True:
                self._replay_once()
                self._loop_count += 1

                if self._stop_event.is_set():
                    log.info("[ReplayFeed] 收到停止信号，退出回放循环")
                    break

                if not self.config.loop:
                    log.info("[ReplayFeed] 单次回放完成: emit_count={}", self._emit_count)
                    break

                log.info(
                    "[ReplayFeed] 循环回放重新开始: loop_count={} emit_count={}",
                    self._loop_count,
                    self._emit_count,
                )
        except Exception:
            log.exception("[ReplayFeed] 回放异常")
        finally:
            with self._lock:
                if self._state != ReplayState.STOPPED:
                    self._state = ReplayState.IDLE
            self._done_event.set()

    def _replay_once(self) -> None:
        """执行一次完整的事件序列回放。"""
        prev_ts: Optional[datetime] = None
        speed = self.config.playback_speed

        for i, event in enumerate(self._events):
            # 检查停止信号
            if self._stop_event.is_set():
                return

            # 等待暂停恢复
            self._pause_event.wait()

            # 时间延迟控制
            if speed > 0 and prev_ts is not None:
                delta_sec = (event.received_at - prev_ts).total_seconds()
                sleep_sec = delta_sec / speed
                if sleep_sec > 0:
                    time.sleep(sleep_sec)

            prev_ts = event.received_at

            # 发布事件
            self._emit_event(event)
            self._emit_count += 1

            # 进度日志
            if (
                self.config.log_every_n_events > 0
                and (i + 1) % self.config.log_every_n_events == 0
            ):
                log.debug(
                    "[ReplayFeed] 回放进度: {}/{} emit_count={} ts={}",
                    i + 1,
                    len(self._events),
                    self._emit_count,
                    event.received_at.isoformat(),
                )

    def _emit_event(self, event: ReplayEvent) -> None:
        """分发单个事件到对应回调。"""
        if isinstance(event, OrderBookDelta):
            if self.config.emit_depth and self._depth_callback is not None:
                try:
                    self._depth_callback(event)
                except Exception:
                    log.exception("[ReplayFeed] depth_callback 异常")
        elif isinstance(event, TradeTick):
            if self.config.emit_trade and self._trade_callback is not None:
                try:
                    self._trade_callback(event)
                except Exception:
                    log.exception("[ReplayFeed] trade_callback 异常")

    def _filter_by_time(self, events: list[ReplayEvent]) -> list[ReplayEvent]:
        """按时间窗口过滤事件。"""
        start = self.config.start_time
        end = self.config.end_time

        if start is None and end is None:
            return events

        result = []
        for event in events:
            ts = event.received_at
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
            result.append(event)

        if start or end:
            log.info(
                "[ReplayFeed] 时间过滤: original={} filtered={} start={} end={}",
                len(events),
                len(result),
                start.isoformat() if start else "N/A",
                end.isoformat() if end else "N/A",
            )
        return result
