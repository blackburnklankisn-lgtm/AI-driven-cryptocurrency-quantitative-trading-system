"""
modules/data/realtime/trade_cache.py — 最新成交流缓冲区

设计说明：
- 维护每个交易对最近 N 笔成交记录（固定大小环形缓冲区）
- 计算成交流派生统计：买卖比、成交量加权均价、成交流不平衡度
- 去重：相同 trade_id 不重复记录（每个 symbol 独立去重集合）
- 线程安全（threading.RLock）
- 不做交易判断，只维护缓冲区和统计量

派生统计字段（TradeFlowStats）：
    buy_volume:              最近 N 笔买单总量（base）
    sell_volume:             最近 N 笔卖单总量（base）
    trade_flow_imbalance:    成交流不平衡度 ∈ [-1, 1]
                             = (buy_vol - sell_vol) / (buy_vol + sell_vol)
    vwap:                    成交量加权均价（最近 N 笔）
    total_notional:          最近 N 笔总成交金额（quote）
    trade_count:             缓冲区内有效成交笔数
    liquidation_count:       强平单笔数
    latest_price:            最新成交价

日志标签：[TradeCache]
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.data.realtime.orderbook_types import TradeSide, TradeTick

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class TradeCacheConfig:
    """
    TradeCache 配置。

    Attributes:
        max_trades:            每个 symbol 最多缓存成交笔数
        dedup_window_size:     去重集合大小（保留最近 N 笔的 trade_id）
        log_every_n_trades:    每 N 笔打印一次 DEBUG 日志（0 = 每次）
    """

    max_trades: int = 500
    dedup_window_size: int = 1000
    log_every_n_trades: int = 50


# ══════════════════════════════════════════════════════════════
# 二、统计结果
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class TradeFlowStats:
    """
    成交流统计结果（由 TradeCache.compute_stats() 产出）。

    字段全为 float，None 表示缓冲区为空无法计算。
    """

    symbol: str
    trade_count: int
    buy_volume: float
    sell_volume: float
    trade_flow_imbalance: float        # ∈ [-1, 1]，正值偏买
    vwap: Optional[float]             # 成交量加权均价，缓冲区空时 None
    total_notional: float             # 总成交金额（quote）
    liquidation_count: int
    latest_price: Optional[float]     # 最新成交价
    computed_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    def is_empty(self) -> bool:
        return self.trade_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "trade_count": self.trade_count,
            "buy_volume": self.buy_volume,
            "sell_volume": self.sell_volume,
            "trade_flow_imbalance": self.trade_flow_imbalance,
            "vwap": self.vwap,
            "total_notional": self.total_notional,
            "liquidation_count": self.liquidation_count,
            "latest_price": self.latest_price,
            "computed_at": self.computed_at.isoformat(),
        }


# ══════════════════════════════════════════════════════════════
# 三、TradeCache 主体
# ══════════════════════════════════════════════════════════════

class TradeCache:
    """
    单个交易对的成交流缓冲区。

    内部维护一个 deque（固定长度），按时间序列保存最近 N 笔成交。
    线程安全：所有操作持 RLock。
    """

    def __init__(
        self,
        symbol: str,
        exchange: str,
        config: TradeCacheConfig = TradeCacheConfig(),
    ) -> None:
        self.symbol = symbol
        self.exchange = exchange
        self.config = config

        self._trades: deque[TradeTick] = deque(maxlen=config.max_trades)
        self._seen_ids: deque[str] = deque(maxlen=config.dedup_window_size)
        self._seen_set: set[str] = set()
        self._lock = threading.RLock()
        self._total_received: int = 0
        self._total_duplicates: int = 0

        log.info(
            "[TradeCache] 初始化: symbol={} exchange={} max_trades={}",
            symbol,
            exchange,
            config.max_trades,
        )

    # ──────────────────────────────────────────────────────────
    # 公开接口
    # ──────────────────────────────────────────────────────────

    def add(self, tick: TradeTick) -> bool:
        """
        添加一笔成交记录。

        Args:
            tick: 成交记录

        Returns:
            True = 成功添加；False = 重复 trade_id（已跳过）
        """
        with self._lock:
            self._total_received += 1

            # 去重检查
            if tick.trade_id in self._seen_set:
                self._total_duplicates += 1
                log.debug(
                    "[TradeCache] 重复成交跳过: symbol={} trade_id={}",
                    tick.symbol,
                    tick.trade_id,
                )
                return False

            # 维护去重窗口
            if len(self._seen_ids) >= self.config.dedup_window_size:
                oldest_id = self._seen_ids[0]
                self._seen_set.discard(oldest_id)
            self._seen_ids.append(tick.trade_id)
            self._seen_set.add(tick.trade_id)

            self._trades.append(tick)

            should_log = (
                self.config.log_every_n_trades == 0
                or self._total_received % self.config.log_every_n_trades == 0
            )
            if should_log:
                log.debug(
                    "[TradeCache] 新成交: symbol={} side={} price={:.2f} "
                    "size={:.6f} total_received={}",
                    tick.symbol,
                    tick.side.value,
                    tick.price,
                    tick.size,
                    self._total_received,
                )

            return True

    def compute_stats(self) -> TradeFlowStats:
        """
        计算当前缓冲区的成交流统计结果。

        Returns:
            TradeFlowStats 统计结果（缓冲区空时返回零值）
        """
        with self._lock:
            trades = list(self._trades)

        if not trades:
            return TradeFlowStats(
                symbol=self.symbol,
                trade_count=0,
                buy_volume=0.0,
                sell_volume=0.0,
                trade_flow_imbalance=0.0,
                vwap=None,
                total_notional=0.0,
                liquidation_count=0,
                latest_price=None,
            )

        buy_vol = 0.0
        sell_vol = 0.0
        total_notional = 0.0
        weighted_price_sum = 0.0
        total_size_for_vwap = 0.0
        liq_count = 0

        for t in trades:
            if t.side == TradeSide.BUY:
                buy_vol += t.size
            elif t.side == TradeSide.SELL:
                sell_vol += t.size
            # UNKNOWN 方向不计入不平衡度，但计入 VWAP

            total_notional += t.notional
            weighted_price_sum += t.price * t.size
            total_size_for_vwap += t.size

            if t.is_liquidation:
                liq_count += 1

        total_vol = buy_vol + sell_vol
        imbalance = (buy_vol - sell_vol) / total_vol if total_vol > 0 else 0.0

        vwap = weighted_price_sum / total_size_for_vwap if total_size_for_vwap > 0 else None
        latest_price = trades[-1].price if trades else None

        return TradeFlowStats(
            symbol=self.symbol,
            trade_count=len(trades),
            buy_volume=buy_vol,
            sell_volume=sell_vol,
            trade_flow_imbalance=imbalance,
            vwap=vwap,
            total_notional=total_notional,
            liquidation_count=liq_count,
            latest_price=latest_price,
        )

    def recent_trades(self, n: Optional[int] = None) -> list[TradeTick]:
        """
        返回最近 n 笔成交记录（时间序列，最旧到最新）。

        Args:
            n: 返回笔数（None = 全部）
        """
        with self._lock:
            trades = list(self._trades)
        if n is not None:
            trades = trades[-n:]
        return trades

    def clear(self) -> None:
        """清空缓冲区（不重置计数器）。"""
        with self._lock:
            self._trades.clear()
            self._seen_ids.clear()
            self._seen_set.clear()
        log.info("[TradeCache] 已清空: symbol={}", self.symbol)

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "symbol": self.symbol,
                "exchange": self.exchange,
                "trade_count": len(self._trades),
                "total_received": self._total_received,
                "total_duplicates": self._total_duplicates,
                "dedup_window_used": len(self._seen_ids),
            }


# ══════════════════════════════════════════════════════════════
# 四、TradeCacheRegistry — 多交易对管理
# ══════════════════════════════════════════════════════════════

class TradeCacheRegistry:
    """
    管理多个交易对的 TradeCache 实例，提供统一入口。

    线程安全：注册和查询操作持 threading.Lock
    """

    def __init__(self, config: TradeCacheConfig = TradeCacheConfig()) -> None:
        self._config = config
        self._caches: dict[str, TradeCache] = {}
        self._lock = threading.Lock()
        log.info("[TradeCache] Registry 初始化: max_trades={}", config.max_trades)

    def get_or_create(self, symbol: str, exchange: str) -> TradeCache:
        """获取或创建指定 symbol 的 TradeCache。"""
        key = f"{exchange}:{symbol}"
        with self._lock:
            if key not in self._caches:
                self._caches[key] = TradeCache(symbol=symbol, exchange=exchange, config=self._config)
                log.info("[TradeCache] 创建新缓存: key={}", key)
            return self._caches[key]

    def add_tick(self, tick: TradeTick) -> bool:
        """转发成交记录到对应 TradeCache（自动创建）。"""
        cache = self.get_or_create(tick.symbol, tick.exchange)
        return cache.add(tick)

    def get_stats(self, symbol: str, exchange: str) -> Optional[TradeFlowStats]:
        """获取指定 symbol 的成交流统计，未注册时返回 None。"""
        key = f"{exchange}:{symbol}"
        with self._lock:
            cache = self._caches.get(key)
        if cache is None:
            return None
        return cache.compute_stats()

    def list_symbols(self) -> list[str]:
        with self._lock:
            return list(self._caches.keys())

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {key: cache.diagnostics() for key, cache in self._caches.items()}
