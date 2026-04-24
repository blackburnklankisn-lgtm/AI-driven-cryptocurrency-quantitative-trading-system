"""
modules/data/realtime/depth_cache.py — 订单簿增量合并与序列一致性保护

设计说明：
- 消费 OrderBookDelta 增量包，维护当前完整订单簿状态
- 核心职责：基于 sequence_id 检测 gap，防止脏快照流出
- gap 检测后立即进入 RECOVERING 状态，禁止向下游发布快照
- 支持通过全量快照包（is_snapshot=True）重置订单簿并恢复序列
- 线程安全（threading.RLock）
- 每次合并后预计算派生字段（spread_bps, mid_price, imbalance）

不变式：
- 只有 GapStatus.is_healthy() 时 get_snapshot() 才返回快照
- gap 状态下 get_snapshot() 返回 None
- 序列号必须严格单调递增（is_snapshot=True 的包除外）

日志标签：[DepthCache]
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.data.realtime.orderbook_types import (
    DepthLevel,
    GapStatus,
    OrderBookDelta,
    OrderBookSnapshot,
)

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class DepthCacheConfig:
    """
    DepthCache 配置。

    Attributes:
        max_depth:             订单簿最大保留档位数（超出时截断）
        imbalance_top_n:       计算 imbalance 时使用的 top-N 档位
        allow_reset_on_snapshot: 收到全量快照包时是否允许重置（用于 gap 回补）
        log_every_n_updates:   每 N 次 apply() 打印一次 DEBUG 日志（0 = 每次都打）
    """

    max_depth: int = 20
    imbalance_top_n: int = 5
    allow_reset_on_snapshot: bool = True
    log_every_n_updates: int = 100


# ══════════════════════════════════════════════════════════════
# 二、内部订单簿状态
# ══════════════════════════════════════════════════════════════

class _BookSide:
    """
    单侧订单簿（买盘或卖盘）内部状态。

    内部用 dict[float, float] 保存 {price: size}，
    合并时 size=0 删除该档位，size>0 更新或插入。
    输出时按 price 排序为 list[DepthLevel]。
    """

    def __init__(self, ascending: bool = True, max_depth: int = 20) -> None:
        """
        Args:
            ascending: True = asks（价格升序），False = bids（价格降序）
            max_depth: 最大保留档位数
        """
        self._levels: dict[float, float] = {}
        self._ascending = ascending
        self._max_depth = max_depth

    def apply_updates(self, updates: list[DepthLevel]) -> None:
        """合并一批增量更新到当前订单簿侧。"""
        for update in updates:
            if update.size <= 0.0:
                self._levels.pop(update.price, None)
            else:
                self._levels[update.price] = update.size

        # 截断超出 max_depth 的档位
        if len(self._levels) > self._max_depth:
            sorted_prices = sorted(self._levels.keys(), reverse=not self._ascending)
            keep = sorted_prices[: self._max_depth]
            self._levels = {p: self._levels[p] for p in keep}

    def reset(self, updates: list[DepthLevel]) -> None:
        """重置为全量快照（is_snapshot=True 时使用）。"""
        self._levels = {}
        self.apply_updates(updates)

    def to_depth_levels(self) -> list[DepthLevel]:
        """输出排序后的档位列表。"""
        sorted_prices = sorted(self._levels.keys(), reverse=not self._ascending)
        return [DepthLevel(price=p, size=self._levels[p]) for p in sorted_prices]

    def best_price(self) -> Optional[float]:
        """返回最优价格（买盘 = 最高买价，卖盘 = 最低卖价）。"""
        if not self._levels:
            return None
        if self._ascending:
            return min(self._levels.keys())  # asks: 最低卖价
        else:
            return max(self._levels.keys())  # bids: 最高买价

    def total_qty(self, top_n: Optional[int] = None) -> float:
        """计算 top-N 档位的总挂单量。"""
        levels = self.to_depth_levels()
        if top_n is not None:
            levels = levels[:top_n]
        return sum(lv.size for lv in levels)

    def is_empty(self) -> bool:
        return len(self._levels) == 0


# ══════════════════════════════════════════════════════════════
# 三、DepthCache 主体
# ══════════════════════════════════════════════════════════════

class DepthCache:
    """
    订单簿增量缓存，维护单个交易对的完整订单簿状态。

    设计不变式：
    1. 只有 GapStatus.is_healthy() 时才允许向下游发布快照
    2. gap 检测后立即禁止发布，直到成功 apply 全量快照才恢复
    3. sequence_id 必须严格单调递增（全量快照包除外，它重置序列）

    线程安全：所有写操作（apply）和读操作（get_snapshot）均持 RLock
    """

    def __init__(
        self,
        symbol: str,
        exchange: str,
        config: DepthCacheConfig = DepthCacheConfig(),
    ) -> None:
        self.symbol = symbol
        self.exchange = exchange
        self.config = config

        self._bids = _BookSide(ascending=False, max_depth=config.max_depth)
        self._asks = _BookSide(ascending=True, max_depth=config.max_depth)
        self._sequence_id: Optional[int] = None  # 最后一次成功 apply 的 sequence_id
        self._gap_status: GapStatus = GapStatus.OK
        self._last_snapshot: Optional[OrderBookSnapshot] = None
        self._update_count: int = 0
        self._gap_count: int = 0
        self._lock = threading.RLock()

        log.info(
            "[DepthCache] 初始化: symbol={} exchange={} max_depth={}",
            symbol,
            exchange,
            config.max_depth,
        )

    # ──────────────────────────────────────────────────────────
    # 公开接口
    # ──────────────────────────────────────────────────────────

    def apply(self, delta: OrderBookDelta) -> Optional[OrderBookSnapshot]:
        """
        应用一个订单簿增量包，返回最新快照（gap 状态下返回 None）。

        处理流程：
        1. is_snapshot=True → 重置订单簿，恢复序列
        2. 检测 sequence_id 连续性
        3. gap 检测失败 → 进入 RECOVERING，返回 None
        4. 合并增量 → 预计算派生字段 → 更新 _last_snapshot
        5. 返回新快照

        Args:
            delta: 订单簿增量包（来自 ws_client 或 replay）

        Returns:
            合并后的 OrderBookSnapshot，或 None（gap 状态）
        """
        with self._lock:
            self._update_count += 1
            should_log = (
                self.config.log_every_n_updates == 0
                or self._update_count % self.config.log_every_n_updates == 0
            )

            # ── 1. 全量快照重置
            if delta.is_snapshot:
                log.info(
                    "[DepthCache] 收到全量快照，重置订单簿: symbol={} seq={}",
                    delta.symbol,
                    delta.sequence_id,
                )
                self._bids.reset(delta.bid_updates)
                self._asks.reset(delta.ask_updates)
                self._sequence_id = delta.sequence_id
                old_status = self._gap_status
                self._gap_status = GapStatus.RECOVERED if old_status != GapStatus.OK else GapStatus.OK
                snapshot = self._build_snapshot(delta, is_gap_recovered=(old_status != GapStatus.OK))
                self._last_snapshot = snapshot
                log.info(
                    "[DepthCache] 快照回补完成: symbol={} seq={} gap_status={}",
                    delta.symbol,
                    delta.sequence_id,
                    self._gap_status.value,
                )
                return snapshot

            # ── 2. 首次初始化（无历史序列号）
            if self._sequence_id is None:
                self._bids.apply_updates(delta.bid_updates)
                self._asks.apply_updates(delta.ask_updates)
                self._sequence_id = delta.sequence_id
                snapshot = self._build_snapshot(delta)
                self._last_snapshot = snapshot
                log.debug(
                    "[DepthCache] 首次初始化: symbol={} seq={}",
                    delta.symbol,
                    delta.sequence_id,
                )
                return snapshot

            # ── 3. 序列号连续性检查
            expected_seq = self._sequence_id + 1
            if delta.sequence_id != expected_seq:
                self._gap_count += 1
                old_status = self._gap_status
                self._gap_status = GapStatus.GAP_DETECTED
                log.warning(
                    "[DepthCache] 序列缺口检测: symbol={} expected={} got={} "
                    "gap_count={} prev_status={}",
                    delta.symbol,
                    expected_seq,
                    delta.sequence_id,
                    self._gap_count,
                    old_status.value,
                )
                return None  # 脏快照不发布

            # ── 4. 正常增量合并
            self._bids.apply_updates(delta.bid_updates)
            self._asks.apply_updates(delta.ask_updates)
            self._sequence_id = delta.sequence_id

            if self._gap_status == GapStatus.RECOVERING:
                # 序列号跟上了但没有显式快照，仍然保持 RECOVERING
                pass
            elif self._gap_status in (GapStatus.OK, GapStatus.RECOVERED):
                self._gap_status = GapStatus.OK

            snapshot = self._build_snapshot(delta)
            self._last_snapshot = snapshot

            if should_log:
                log.debug(
                    "[DepthCache] 增量合并: symbol={} seq={} bid={:.2f} ask={:.2f} "
                    "spread_bps={:.2f} imbalance={:.4f} update_count={}",
                    delta.symbol,
                    delta.sequence_id,
                    snapshot.best_bid,
                    snapshot.best_ask,
                    snapshot.spread_bps,
                    snapshot.imbalance,
                    self._update_count,
                )

            return snapshot

    def get_snapshot(self) -> Optional[OrderBookSnapshot]:
        """
        获取当前最新快照（gap 状态下返回 None）。

        策略层应检查返回值的 gap_status 字段再决定是否消费。
        """
        with self._lock:
            if not self._gap_status.is_healthy():
                log.debug(
                    "[DepthCache] 快照不可用（gap 状态）: symbol={} status={}",
                    self.symbol,
                    self._gap_status.value,
                )
                return None
            return self._last_snapshot

    def reset(self) -> None:
        """清空缓存（用于重新连接或手动重置）。"""
        with self._lock:
            self._bids = _BookSide(ascending=False, max_depth=self.config.max_depth)
            self._asks = _BookSide(ascending=True, max_depth=self.config.max_depth)
            self._sequence_id = None
            self._gap_status = GapStatus.OK
            self._last_snapshot = None
            self._update_count = 0
            log.info("[DepthCache] 已重置: symbol={}", self.symbol)

    @property
    def gap_status(self) -> GapStatus:
        with self._lock:
            return self._gap_status

    @property
    def sequence_id(self) -> Optional[int]:
        with self._lock:
            return self._sequence_id

    def diagnostics(self) -> dict[str, Any]:
        """返回当前缓存诊断信息。"""
        with self._lock:
            best_bid = self._bids.best_price()
            best_ask = self._asks.best_price()
            return {
                "symbol": self.symbol,
                "exchange": self.exchange,
                "gap_status": self._gap_status.value,
                "sequence_id": self._sequence_id,
                "update_count": self._update_count,
                "gap_count": self._gap_count,
                "bid_levels": len(self._bids._levels),
                "ask_levels": len(self._asks._levels),
                "best_bid": best_bid,
                "best_ask": best_ask,
                "has_snapshot": self._last_snapshot is not None,
            }

    # ──────────────────────────────────────────────────────────
    # 内部工具
    # ──────────────────────────────────────────────────────────

    def _build_snapshot(
        self,
        delta: OrderBookDelta,
        is_gap_recovered: bool = False,
    ) -> OrderBookSnapshot:
        """基于当前订单簿状态构建 OrderBookSnapshot（预计算派生字段）。"""
        bids = self._bids.to_depth_levels()
        asks = self._asks.to_depth_levels()

        best_bid = bids[0].price if bids else 0.0
        best_ask = asks[0].price if asks else 0.0

        if best_bid > 0 and best_ask > 0:
            mid_price = (best_bid + best_ask) / 2.0
            spread_bps = (best_ask - best_bid) / mid_price * 10000.0
        else:
            mid_price = 0.0
            spread_bps = 0.0

        top_n = self.config.imbalance_top_n
        bid_qty = self._bids.total_qty(top_n=top_n)
        ask_qty = self._asks.total_qty(top_n=top_n)
        if (bid_qty + ask_qty) > 0:
            imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty)
        else:
            imbalance = 0.0

        return OrderBookSnapshot(
            symbol=self.symbol,
            exchange=self.exchange,
            sequence_id=delta.sequence_id,
            best_bid=best_bid,
            best_ask=best_ask,
            bids=bids,
            asks=asks,
            spread_bps=spread_bps,
            mid_price=mid_price,
            imbalance=imbalance,
            received_at=delta.received_at,
            exchange_ts=delta.exchange_ts,
            gap_status=self._gap_status,
            is_gap_recovered=is_gap_recovered,
            depth_levels=len(bids) + len(asks),
            debug_payload={
                "update_count": self._update_count,
                "gap_count": self._gap_count,
            },
        )


# ══════════════════════════════════════════════════════════════
# 四、DepthCacheRegistry — 多交易对管理
# ══════════════════════════════════════════════════════════════

class DepthCacheRegistry:
    """
    管理多个交易对的 DepthCache 实例，提供统一访问入口。

    线程安全：注册与查询操作持 threading.Lock
    """

    def __init__(self, config: DepthCacheConfig = DepthCacheConfig()) -> None:
        self._config = config
        self._caches: dict[str, DepthCache] = {}
        self._lock = threading.Lock()
        log.info("[DepthCache] Registry 初始化: max_depth={}", config.max_depth)

    def get_or_create(self, symbol: str, exchange: str) -> DepthCache:
        """获取或创建指定 symbol 的 DepthCache。"""
        key = f"{exchange}:{symbol}"
        with self._lock:
            if key not in self._caches:
                self._caches[key] = DepthCache(symbol=symbol, exchange=exchange, config=self._config)
                log.info("[DepthCache] 创建新缓存: key={}", key)
            return self._caches[key]

    def get(self, symbol: str, exchange: str) -> Optional[DepthCache]:
        """获取已注册的 DepthCache，不存在时返回 None。"""
        key = f"{exchange}:{symbol}"
        with self._lock:
            return self._caches.get(key)

    def apply_delta(self, delta: OrderBookDelta) -> Optional[OrderBookSnapshot]:
        """
        转发增量包到对应 DepthCache 并返回快照。
        自动创建不存在的 DepthCache。
        """
        cache = self.get_or_create(delta.symbol, delta.exchange)
        return cache.apply(delta)

    def list_symbols(self) -> list[str]:
        with self._lock:
            return list(self._caches.keys())

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {key: cache.diagnostics() for key, cache in self._caches.items()}
