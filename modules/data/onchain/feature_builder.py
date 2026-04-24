"""
modules/data/onchain/feature_builder.py — 链上特征视图构建器

设计说明：
- 消费 OnChainCollector.collect() 的结果（OnChainRecord），
  结合 K 线时间索引，产出规范化的 SourceFrame
- 职责分离：
    * Collector 负责"采集"，FeatureBuilder 负责"构建特征视图"
    * FeatureBuilder 不做 API 调用，只做数值转换和 DataFrame 组装
- 特征处理规则（第一版：轻量规则化，无 ML）：
    * 直接使用规范化字段值（不做复杂工程）
    * 对 exchange_inflow_ratio / whale_tx_count_ratio 做 0-1 clip
    * nvt_proxy 做 log 变换（防止极端值）
    * 缺失字段（None 值）保留为 NaN
- 输出 SourceFrame，freshness 来自 OnChainCache.evaluate_freshness()

接口：
    OnChainFeatureBuilder(cache, evaluator_config)
    .build(symbol, record, kline_index) -> SourceFrame
    .build_from_cache(symbol, kline_index) -> SourceFrame

日志标签：[OnChain]
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timezone
from typing import Optional

import numpy as np
import pandas as pd

from core.logger import get_logger
from modules.data.fusion.freshness import FreshnessConfig
from modules.data.fusion.source_contract import FreshnessStatus, SourceFrame
from modules.data.onchain.cache import OnChainCache
from modules.data.onchain.providers import ONCHAIN_FIELDS, OnChainRecord

log = get_logger(__name__)

# 输出特征列名（与 ONCHAIN_FIELDS 对应，但可以在这里做名称映射）
FEATURE_COLUMNS = [
    "oc_active_addr_chg",       # active_addresses_change
    "oc_exchange_inflow",       # exchange_inflow_ratio（clip 0-1）
    "oc_whale_tx_ratio",        # whale_tx_count_ratio（clip 0-1）
    "oc_stablecoin_supply",     # stablecoin_supply_ratio
    "oc_miner_reserve_chg",     # miner_reserve_change
    "oc_nvt_log",               # log1p(nvt_proxy)
]

_FIELD_TO_FEATURE = {
    "active_addresses_change": "oc_active_addr_chg",
    "exchange_inflow_ratio":   "oc_exchange_inflow",
    "whale_tx_count_ratio":    "oc_whale_tx_ratio",
    "stablecoin_supply_ratio": "oc_stablecoin_supply",
    "miner_reserve_change":    "oc_miner_reserve_chg",
    "nvt_proxy":               "oc_nvt_log",  # log 变换
}


@dataclass
class FeatureBuilderConfig:
    """OnChainFeatureBuilder 配置。"""

    freshness_config: Optional[FreshnessConfig] = None
    clip_ratio_fields: bool = True       # 是否 clip 比率字段到 [0, 1]
    log_transform_nvt: bool = True       # 是否对 nvt_proxy 做 log1p 变换
    ttl_sec: int = 3600                  # SourceFrame 的 freshness_ttl_sec


class OnChainFeatureBuilder:
    """
    链上特征视图构建器。

    将 OnChainRecord 中的原始字段转换为适合注入 DataKitchen 的特征 DataFrame，
    并包装为 SourceFrame（含 freshness 元数据）。

    Args:
        cache:  OnChainCache 实例（用于读取缓存和评估 freshness）
        config: FeatureBuilderConfig
    """

    def __init__(
        self,
        cache: Optional[OnChainCache] = None,
        config: Optional[FeatureBuilderConfig] = None,
    ) -> None:
        self.cache = cache
        self.config = config or FeatureBuilderConfig()
        log.info(
            "[OnChain] OnChainFeatureBuilder 初始化: "
            "clip={} log_nvt={} ttl={}s",
            self.config.clip_ratio_fields,
            self.config.log_transform_nvt,
            self.config.ttl_sec,
        )

    # ──────────────────────────────────────────────────────────────
    # 核心构建接口
    # ──────────────────────────────────────────────────────────────

    def build(
        self,
        symbol: str,
        record: OnChainRecord,
        kline_index: Optional[pd.DatetimeIndex] = None,
    ) -> SourceFrame:
        """
        将 OnChainRecord 构建为 SourceFrame。

        如果提供 kline_index，会将该时刻的特征值广播到整个时间序列
        （单点时间戳 → 所有行使用同一值，后续由 SourceAligner 处理前向填充）。
        如果不提供 kline_index，则返回只有一行的 SourceFrame（时间戳 = fetched_at）。

        Args:
            symbol:       目标资产
            record:       OnChainRecord 采集结果
            kline_index:  K 线时间索引（可选）

        Returns:
            SourceFrame
        """
        # 应用特征变换
        feature_values = self._transform(record)

        if kline_index is not None and len(kline_index) > 0:
            # 将单点值扩展到 K 线索引（每行都是同一值）
            # 注意：这里只创建一行（fetched_at 时刻），由 SourceAligner 做前向填充
            idx_utc = kline_index.tz_convert("UTC") if kline_index.tz else kline_index.tz_localize("UTC")
            row_ts = record.fetched_at
            if row_ts.tzinfo is None:
                row_ts = row_ts.replace(tzinfo=timezone.utc)
            # 找到最近的 K 线时刻（向后对齐）
            pos = idx_utc.searchsorted(row_ts, side="right") - 1
            if pos < 0:
                pos = 0
            snap_ts = idx_utc[pos]
            frame = pd.DataFrame(
                [feature_values],
                index=pd.DatetimeIndex([snap_ts], tz="UTC"),
            )
        else:
            # 单行 SourceFrame（fetched_at 为索引）
            ts = record.fetched_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            frame = pd.DataFrame(
                [feature_values],
                index=pd.DatetimeIndex([ts], tz="UTC"),
            )

        # 评估 freshness
        if self.cache is not None:
            freshness = self.cache.evaluate_freshness(
                symbol, self.config.freshness_config
            )
        else:
            # 无缓存时根据 record.fetched_at 直接评估
            from datetime import datetime
            import time
            now = datetime.now(tz=timezone.utc)
            lag = (now - record.fetched_at.replace(tzinfo=timezone.utc) if record.fetched_at.tzinfo is None else now - record.fetched_at).total_seconds()
            ttl = self.config.ttl_sec
            from modules.data.fusion.source_contract import SourceFreshness
            freshness = SourceFreshness(
                source_name=f"onchain_{symbol.lower()}",
                status=FreshnessStatus.FRESH if lag <= ttl else FreshnessStatus.STALE,
                lag_sec=lag,
                ttl_sec=ttl,
                collected_at=record.fetched_at,
            )

        log.debug(
            "[OnChain] FeatureBuilder.build: symbol={} rows={} "
            "missing_fields={} freshness={}",
            symbol,
            len(frame),
            record.missing_fields(),
            freshness.status.value,
        )

        return SourceFrame(
            source_name=f"onchain_{symbol.lower()}",
            frame=frame,
            freshness=freshness,
            freshness_ttl_sec=self.config.ttl_sec,
            metadata={
                "symbol": symbol,
                "provider": record.source_name,
                "fetched_at": record.fetched_at.isoformat(),
                "missing_fields": record.missing_fields(),
                "feature_columns": FEATURE_COLUMNS,
            },
        )

    def build_from_cache(
        self,
        symbol: str,
        kline_index: Optional[pd.DatetimeIndex] = None,
    ) -> SourceFrame:
        """
        直接从缓存读取数据构建 SourceFrame（不触发新采集）。

        用于"降级读缓存"场景：provider 不可用时仍能产出链上特征视图。

        Returns:
            SourceFrame（缓存为空时返回 empty SourceFrame，freshness=MISSING）
        """
        if self.cache is None:
            return SourceFrame.make_empty(
                f"onchain_{symbol.lower()}",
                reason="无 OnChainCache 实例",
                ttl_sec=self.config.ttl_sec,
            )

        cached = self.cache.read(symbol)
        if not cached:
            return SourceFrame.make_empty(
                f"onchain_{symbol.lower()}",
                reason=f"缓存无 {symbol} 数据",
                ttl_sec=self.config.ttl_sec,
            )

        # 构造最小 OnChainRecord（仅用于 _transform）
        from datetime import datetime
        collected_ats = [
            entry["collected_at"]
            for entry in cached.values()
            if entry.get("collected_at") is not None
        ]
        fetched_at = max(collected_ats) if collected_ats else datetime.now(tz=timezone.utc)

        fields = {
            field_name: entry.get("value")
            for field_name, entry in cached.items()
        }
        # 补充缺失的标准字段
        for f in ONCHAIN_FIELDS:
            if f not in fields:
                fields[f] = None

        record = OnChainRecord(
            fetched_at=fetched_at,
            fields=fields,
            source_name=f"cache/onchain_{symbol.lower()}",
        )
        return self.build(symbol, record, kline_index)

    # ──────────────────────────────────────────────────────────────
    # 特征变换（内部）
    # ──────────────────────────────────────────────────────────────

    def _transform(self, record: OnChainRecord) -> dict[str, Optional[float]]:
        """
        将原始字段值转换为规范化特征值。

        Rules:
        - None → NaN（缺失字段）
        - exchange_inflow_ratio / whale_tx_count_ratio → clip [0, 1]
        - nvt_proxy → log1p(max(0, x))（防止负值和极端值）
        """
        f = record.fields
        result: dict[str, Optional[float]] = {}

        # oc_active_addr_chg: 直接使用（范围 [-0.5, 0.5] 典型）
        val = f.get("active_addresses_change")
        result["oc_active_addr_chg"] = float(val) if val is not None else float("nan")

        # oc_exchange_inflow: clip [0, 1]
        val = f.get("exchange_inflow_ratio")
        if val is not None and self.config.clip_ratio_fields:
            result["oc_exchange_inflow"] = float(max(0.0, min(1.0, val)))
        else:
            result["oc_exchange_inflow"] = float(val) if val is not None else float("nan")

        # oc_whale_tx_ratio: clip [0, 1]
        val = f.get("whale_tx_count_ratio")
        if val is not None and self.config.clip_ratio_fields:
            result["oc_whale_tx_ratio"] = float(max(0.0, min(1.0, val)))
        else:
            result["oc_whale_tx_ratio"] = float(val) if val is not None else float("nan")

        # oc_stablecoin_supply: 直接使用
        val = f.get("stablecoin_supply_ratio")
        result["oc_stablecoin_supply"] = float(val) if val is not None else float("nan")

        # oc_miner_reserve_chg: 直接使用
        val = f.get("miner_reserve_change")
        result["oc_miner_reserve_chg"] = float(val) if val is not None else float("nan")

        # oc_nvt_log: log1p(max(0, nvt_proxy))
        val = f.get("nvt_proxy")
        if val is not None and self.config.log_transform_nvt:
            result["oc_nvt_log"] = math.log1p(max(0.0, float(val)))
        else:
            result["oc_nvt_log"] = float(val) if val is not None else float("nan")

        return result
