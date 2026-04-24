"""
modules/data/realtime/feature_builder.py — 微观结构特征构建器

设计说明：
- 消费 OrderBookSnapshot + TradeFlowStats，产出微观结构特征向量
- 输出格式与 W12/W13 保持一致（SourceFrame 或 DataFrame）
- 只构建特征，不做 BUY/SELL 判断，不依赖执行层
- 特征命名前缀：mb_（microbook）
- 所有特征经过 clip / normalize 处理，输出值域有界
- NaN 处理：输入为 None 时对应特征输出 NaN（不影响其他特征）

输出特征（第一版 8 个）：
    mb_spread_bps            订单簿价差（基点），clip [0, 200]
    mb_order_imbalance       订单簿买卖不平衡度 ∈ [-1, 1]
    mb_micro_price           微价格（按挂单量加权的 mid），与 mid_price 之差的 bps
    mb_book_pressure_ratio   买盘 / 卖盘总挂单量比（top-N），clip [0.1, 10] → log10 → [-1, 1]
    mb_trade_flow_imbalance  成交流不平衡度 ∈ [-1, 1]（来自 TradeFlowStats）
    mb_vwap_vs_mid           VWAP 与 mid_price 的偏差（bps），clip [-50, 50]
    mb_liq_ratio             强平单占比 = liq_count / trade_count ∈ [0, 1]
    mb_spread_tightness      1 / (1 + spread_bps)，越紧 → 越接近 1

日志标签：[MicroAlpha]
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from core.logger import get_logger
from modules.data.realtime.orderbook_types import OrderBookSnapshot
from modules.data.realtime.trade_cache import TradeFlowStats

log = get_logger(__name__)

# 特征列名常量
MICRO_FEATURE_COLS = [
    "mb_spread_bps",
    "mb_order_imbalance",
    "mb_micro_price",
    "mb_book_pressure_ratio",
    "mb_trade_flow_imbalance",
    "mb_vwap_vs_mid",
    "mb_liq_ratio",
    "mb_spread_tightness",
]


# ══════════════════════════════════════════════════════════════
# 一、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class MicroFeatureBuilderConfig:
    """
    微观结构特征构建器配置。

    Attributes:
        spread_clip_bps:       价差 clip 上限（基点）
        book_pressure_top_n:   计算 book_pressure_ratio 的 top-N 档位
        vwap_clip_bps:         VWAP vs mid 偏差 clip 上限（基点）
        spread_log_base:       book_pressure 对数底（默认 10）
        log_build_time:        是否打印特征构建耗时
    """

    spread_clip_bps: float = 200.0
    book_pressure_top_n: int = 5
    vwap_clip_bps: float = 50.0
    spread_log_base: float = 10.0
    log_build_time: bool = False


# ══════════════════════════════════════════════════════════════
# 二、特征构建结果
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class MicroFeatureFrame:
    """
    微观结构特征向量（单时刻快照）。

    所有特征值均为 float（NaN 表示输入缺失，无法计算）。
    """

    symbol: str
    timestamp: datetime
    mb_spread_bps: float
    mb_order_imbalance: float
    mb_micro_price: float
    mb_book_pressure_ratio: float
    mb_trade_flow_imbalance: float
    mb_vwap_vs_mid: float
    mb_liq_ratio: float
    mb_spread_tightness: float
    is_book_healthy: bool             # 来源快照是否健康
    debug_payload: dict[str, Any] = field(default_factory=dict)

    def to_series(self) -> pd.Series:
        """转换为 pandas Series（只含数值特征列）。"""
        return pd.Series(
            {
                "mb_spread_bps": self.mb_spread_bps,
                "mb_order_imbalance": self.mb_order_imbalance,
                "mb_micro_price": self.mb_micro_price,
                "mb_book_pressure_ratio": self.mb_book_pressure_ratio,
                "mb_trade_flow_imbalance": self.mb_trade_flow_imbalance,
                "mb_vwap_vs_mid": self.mb_vwap_vs_mid,
                "mb_liq_ratio": self.mb_liq_ratio,
                "mb_spread_tightness": self.mb_spread_tightness,
            }
        )

    def to_dataframe(self) -> pd.DataFrame:
        """转换为单行 DataFrame（可直接 concat 到历史帧）。"""
        return pd.DataFrame([self.to_series()], index=[self.timestamp])

    def has_nan(self) -> bool:
        """任意特征字段是否含有 NaN。"""
        s = self.to_series()
        return bool(s.isna().any())

    def feature_names(self) -> list[str]:
        return list(MICRO_FEATURE_COLS)


# ══════════════════════════════════════════════════════════════
# 三、MicroFeatureBuilder 主体
# ══════════════════════════════════════════════════════════════

class MicroFeatureBuilder:
    """
    微观结构特征构建器。

    无状态，每次 build() 输入快照和成交统计，输出特征帧。
    设计原则：
    - 不缓存任何中间状态
    - 所有特征有界（通过 clip / normalize）
    - 输入 None / 不健康快照时相关特征为 NaN

    Args:
        config: MicroFeatureBuilderConfig
    """

    def __init__(
        self,
        config: Optional[MicroFeatureBuilderConfig] = None,
    ) -> None:
        self.config = config or MicroFeatureBuilderConfig()
        log.info(
            "[MicroAlpha] MicroFeatureBuilder 初始化: spread_clip={}bps top_n={}",
            self.config.spread_clip_bps,
            self.config.book_pressure_top_n,
        )

    def build(
        self,
        snapshot: Optional[OrderBookSnapshot],
        trade_stats: Optional[TradeFlowStats] = None,
    ) -> MicroFeatureFrame:
        """
        构建微观结构特征帧。

        Args:
            snapshot:     订单簿快照（None 时所有订单簿特征为 NaN）
            trade_stats:  成交流统计（None 时成交流特征为 NaN）

        Returns:
            MicroFeatureFrame 特征帧
        """
        t_start = time.perf_counter()
        nan = float("nan")

        # ── 基础信息
        symbol = snapshot.symbol if snapshot is not None else "UNKNOWN"
        ts = snapshot.received_at if snapshot is not None else datetime.now(tz=timezone.utc)
        is_healthy = snapshot is not None and snapshot.is_healthy()

        # ── 订单簿特征
        if is_healthy:
            mb_spread_bps = self._clip(snapshot.spread_bps, 0.0, self.config.spread_clip_bps)
            mb_order_imbalance = self._clip(snapshot.imbalance, -1.0, 1.0)
            mb_micro_price = self._compute_micro_price(snapshot)
            mb_book_pressure = self._compute_book_pressure(snapshot)
            mb_spread_tightness = 1.0 / (1.0 + mb_spread_bps)
        else:
            if snapshot is not None and not snapshot.is_healthy():
                log.debug(
                    "[MicroAlpha] 快照不健康，订单簿特征置 NaN: symbol={} gap_status={}",
                    snapshot.symbol,
                    snapshot.gap_status.value,
                )
            mb_spread_bps = nan
            mb_order_imbalance = nan
            mb_micro_price = nan
            mb_book_pressure = nan
            mb_spread_tightness = nan

        # ── 成交流特征
        if trade_stats is not None and not trade_stats.is_empty():
            mb_trade_flow_imbalance = self._clip(trade_stats.trade_flow_imbalance, -1.0, 1.0)

            # VWAP vs mid（bps）
            if trade_stats.vwap is not None and is_healthy and snapshot.mid_price > 0:
                vwap_vs_mid_raw = (trade_stats.vwap - snapshot.mid_price) / snapshot.mid_price * 10000.0
                mb_vwap_vs_mid = self._clip(vwap_vs_mid_raw, -self.config.vwap_clip_bps, self.config.vwap_clip_bps)
            else:
                mb_vwap_vs_mid = nan

            # 强平比率
            if trade_stats.trade_count > 0:
                mb_liq_ratio = self._clip(trade_stats.liquidation_count / trade_stats.trade_count, 0.0, 1.0)
            else:
                mb_liq_ratio = 0.0
        else:
            mb_trade_flow_imbalance = nan
            mb_vwap_vs_mid = nan
            mb_liq_ratio = nan

        t_elapsed_ms = (time.perf_counter() - t_start) * 1000
        if self.config.log_build_time or t_elapsed_ms > 5.0:
            log.debug(
                "[MicroAlpha] 特征构建完成: symbol={} spread_bps={:.2f} "
                "imbalance={:.4f} micro_price={:.4f} trade_flow={:.4f} elapsed_ms={:.2f}",
                symbol,
                mb_spread_bps,
                mb_order_imbalance,
                mb_micro_price,
                mb_trade_flow_imbalance,
                t_elapsed_ms,
            )

        return MicroFeatureFrame(
            symbol=symbol,
            timestamp=ts,
            mb_spread_bps=mb_spread_bps,
            mb_order_imbalance=mb_order_imbalance,
            mb_micro_price=mb_micro_price,
            mb_book_pressure_ratio=mb_book_pressure,
            mb_trade_flow_imbalance=mb_trade_flow_imbalance,
            mb_vwap_vs_mid=mb_vwap_vs_mid,
            mb_liq_ratio=mb_liq_ratio,
            mb_spread_tightness=mb_spread_tightness,
            is_book_healthy=is_healthy,
            debug_payload={
                "elapsed_ms": round(t_elapsed_ms, 3),
                "has_trade_stats": trade_stats is not None,
                "trade_count": trade_stats.trade_count if trade_stats else 0,
            },
        )

    # ──────────────────────────────────────────────────────────
    # 内部计算工具
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _clip(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))

    def _compute_micro_price(self, snapshot: OrderBookSnapshot) -> float:
        """
        计算微价格（weighted mid price）。

        micro_price = (best_bid × ask_qty + best_ask × bid_qty) / (bid_qty + ask_qty)

        输出：micro_price 与 mid_price 的偏差（基点），clip [-50, 50]
        """
        top_n = self.config.book_pressure_top_n
        bids = snapshot.bids[:top_n]
        asks = snapshot.asks[:top_n]

        bid_qty = sum(d.size for d in bids)
        ask_qty = sum(d.size for d in asks)
        total_qty = bid_qty + ask_qty

        if total_qty <= 0 or snapshot.mid_price <= 0:
            return float("nan")

        micro = (snapshot.best_bid * ask_qty + snapshot.best_ask * bid_qty) / total_qty
        deviation_bps = (micro - snapshot.mid_price) / snapshot.mid_price * 10000.0
        return self._clip(deviation_bps, -50.0, 50.0)

    def _compute_book_pressure(self, snapshot: OrderBookSnapshot) -> float:
        """
        计算订单簿买卖压力比。

        raw_ratio = bid_qty / ask_qty (top-N)
        → log10(ratio) → clip [-1, 1]

        0.0 表示完全平衡，正值偏多，负值偏空
        """
        top_n = self.config.book_pressure_top_n
        bids = snapshot.bids[:top_n]
        asks = snapshot.asks[:top_n]

        bid_qty = sum(d.size for d in bids)
        ask_qty = sum(d.size for d in asks)

        if ask_qty <= 0 or bid_qty <= 0:
            return float("nan")

        raw_ratio = bid_qty / ask_qty
        # 防止 log(0)
        raw_ratio = max(0.01, min(100.0, raw_ratio))
        log_ratio = math.log10(raw_ratio)  # [-2, 2]
        # 缩放到 [-1, 1]
        return self._clip(log_ratio / 2.0, -1.0, 1.0)
