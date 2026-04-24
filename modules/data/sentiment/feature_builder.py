"""
modules/data/sentiment/feature_builder.py — 情绪特征视图构建器

设计说明：
- 消费 SentimentCollector.collect() 的结果（SentimentRecord），
  结合 K 线时间索引，产出规范化的 SourceFrame
- 职责分离：
    * Collector 负责"采集"，FeatureBuilder 负责"构建特征视图"
    * FeatureBuilder 不做 API 调用，只做数值转换和 DataFrame 组装
- 特征处理规则（第一版：轻量规则化，无 ML）：
    * fear_greed_index:       归一化到 [0, 1]（÷100）
    * funding_rate_zscore:    clip 到 [-3, 3]（限制极端 z-score）
    * long_short_ratio_change: 直接使用（已为变化率）
    * open_interest_change:   直接使用（已为变化率）
    * liquidation_imbalance:  clip 到 [-1, 1]
    * sentiment_score_ema:    clip 到 [0, 1]
    * 缺失字段（None 值）保留为 NaN
- 输出 SourceFrame，freshness 来自 SentimentCache.evaluate_freshness()
- 数据缺失时返回带 MISSING 状态的空 SourceFrame，而不是 neutral 伪值

接口：
    SentimentFeatureBuilder(cache, config)
    .build(symbol, record, kline_index=None) -> SourceFrame
    .build_from_cache(symbol, kline_index=None) -> SourceFrame

日志标签：[Sentiment]
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from core.logger import get_logger
from modules.data.fusion.freshness import FreshnessConfig
from modules.data.fusion.source_contract import FreshnessStatus, SourceFrame, SourceFreshness
from modules.data.sentiment.cache import SentimentCache
from modules.data.sentiment.providers import SENTIMENT_FIELDS, SentimentRecord

log = get_logger(__name__)

# 输出特征列名（与 SENTIMENT_FIELDS 对应，带 st_ 前缀）
FEATURE_COLUMNS = [
    "st_fear_greed",        # fear_greed_index ÷ 100 → [0, 1]
    "st_funding_rate",      # funding_rate_zscore，clip [-3, 3]
    "st_long_short_chg",    # long_short_ratio_change（直接使用）
    "st_oi_change",         # open_interest_change（直接使用）
    "st_liq_imbalance",     # liquidation_imbalance，clip [-1, 1]
    "st_sentiment_ema",     # sentiment_score_ema，clip [0, 1]
]

_FIELD_TO_FEATURE = {
    "fear_greed_index":       "st_fear_greed",
    "funding_rate_zscore":    "st_funding_rate",
    "long_short_ratio_change":"st_long_short_chg",
    "open_interest_change":   "st_oi_change",
    "liquidation_imbalance":  "st_liq_imbalance",
    "sentiment_score_ema":    "st_sentiment_ema",
}


@dataclass
class SentimentFeatureBuilderConfig:
    """SentimentFeatureBuilder 配置。"""

    freshness_config: Optional[FreshnessConfig] = None
    normalize_fear_greed: bool = True    # 是否将 fear_greed_index 归一化到 [0, 1]
    clip_funding_rate: bool = True       # 是否 clip funding_rate_zscore 到 [-3, 3]
    clip_liq_imbalance: bool = True      # 是否 clip liquidation_imbalance 到 [-1, 1]
    clip_sentiment_ema: bool = True      # 是否 clip sentiment_score_ema 到 [0, 1]
    ttl_sec: int = 3600                  # SourceFrame 的 freshness_ttl_sec


class SentimentFeatureBuilder:
    """
    情绪特征视图构建器。

    将 SentimentRecord 中的原始字段转换为适合注入 DataKitchen 的特征 DataFrame，
    并包装为 SourceFrame（含 freshness 元数据）。

    Args:
        cache:  SentimentCache 实例（用于读取缓存和评估 freshness）
        config: SentimentFeatureBuilderConfig
    """

    def __init__(
        self,
        cache: Optional[SentimentCache] = None,
        config: Optional[SentimentFeatureBuilderConfig] = None,
    ) -> None:
        self.cache = cache
        self.config = config or SentimentFeatureBuilderConfig()
        log.info(
            "[Sentiment] SentimentFeatureBuilder 初始化: "
            "normalize_fg={} clip_funding={} ttl={}s",
            self.config.normalize_fear_greed,
            self.config.clip_funding_rate,
            self.config.ttl_sec,
        )

    # ──────────────────────────────────────────────────────────────
    # 核心构建接口
    # ──────────────────────────────────────────────────────────────

    def build(
        self,
        symbol: str,
        record: SentimentRecord,
        kline_index: Optional[pd.DatetimeIndex] = None,
    ) -> SourceFrame:
        """
        将 SentimentRecord 构建为 SourceFrame。

        如果提供 kline_index，会将特征值对齐到最近的 K 线时刻（单行），
        后续由 SourceAligner 负责前向填充。
        如果不提供 kline_index，则返回只有一行的 SourceFrame（时间戳 = fetched_at）。

        Args:
            symbol:       目标资产
            record:       SentimentRecord 采集结果
            kline_index:  K 线时间索引（可选）

        Returns:
            SourceFrame
        """
        feature_values = self._transform(record)
        source_name = f"sentiment_{symbol.lower()}"

        if kline_index is not None and len(kline_index) > 0:
            idx_utc = (
                kline_index.tz_convert("UTC")
                if kline_index.tz
                else kline_index.tz_localize("UTC")
            )
            row_ts = record.fetched_at
            if row_ts.tzinfo is None:
                row_ts = row_ts.replace(tzinfo=timezone.utc)
            pos = idx_utc.searchsorted(row_ts, side="right") - 1
            if pos < 0:
                pos = 0
            snap_ts = idx_utc[pos]
            frame = pd.DataFrame(
                [feature_values],
                index=pd.DatetimeIndex([snap_ts], tz="UTC"),
            )
        else:
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
            now = datetime.now(tz=timezone.utc)
            ft = record.fetched_at
            if ft.tzinfo is None:
                ft = ft.replace(tzinfo=timezone.utc)
            lag = (now - ft).total_seconds()
            ttl = self.config.ttl_sec
            freshness = SourceFreshness(
                source_name=source_name,
                status=FreshnessStatus.FRESH if lag <= ttl else FreshnessStatus.STALE,
                lag_sec=lag,
                ttl_sec=ttl,
                collected_at=record.fetched_at,
            )

        log.debug(
            "[Sentiment] FeatureBuilder.build: symbol={} rows={} "
            "missing_fields={} freshness={}",
            symbol,
            len(frame),
            record.missing_fields(),
            freshness.status,
        )

        return SourceFrame(
            source_name=source_name,
            frame=frame,
            freshness=freshness,
            freshness_ttl_sec=self.config.ttl_sec,
            metadata={
                "provider": record.source_name,
                "fetched_at": record.fetched_at.isoformat()
                if record.fetched_at
                else None,
                "symbol": symbol,
            },
        )

    def build_from_cache(
        self,
        symbol: str,
        kline_index: Optional[pd.DatetimeIndex] = None,
    ) -> SourceFrame:
        """
        从缓存读取数据并构建 SourceFrame。

        如果缓存为空，返回带 MISSING 状态的空 SourceFrame（不生成伪信号）。

        Args:
            symbol:      目标资产
            kline_index: K 线时间索引（可选）

        Returns:
            SourceFrame（有数据）或 MISSING SourceFrame（缓存为空）
        """
        source_name = f"sentiment_{symbol.lower()}"

        if self.cache is None:
            log.warning("[Sentiment] build_from_cache: 未提供 cache 实例")
            return SourceFrame.make_empty(
                source_name, reason="无 cache 实例", ttl_sec=self.config.ttl_sec
            )

        raw = self.cache.read(symbol)
        if not raw:
            log.debug("[Sentiment] build_from_cache: 缓存为空, symbol={}", symbol)
            return SourceFrame.make_empty(
                source_name, reason="缓存为空", ttl_sec=self.config.ttl_sec
            )

        # 从缓存重建 SentimentRecord
        fields: dict = {}
        latest_at: Optional[datetime] = None
        for field_name in SENTIMENT_FIELDS:
            entry = raw.get(field_name)
            if entry is None:
                fields[field_name] = None
                continue
            fields[field_name] = entry.get("value")
            at = entry.get("collected_at")
            if at and (latest_at is None or at > latest_at):
                latest_at = at

        record = SentimentRecord(
            fetched_at=latest_at or datetime.now(tz=timezone.utc),
            fields=fields,
            source_name=f"cache_{symbol.lower()}",
        )
        return self.build(symbol, record, kline_index=kline_index)

    # ──────────────────────────────────────────────────────────────
    # 特征变换
    # ──────────────────────────────────────────────────────────────

    def _transform(self, record: SentimentRecord) -> dict:
        """将 SentimentRecord 字段转换为特征字典。"""
        import math

        def _get(field_name: str):
            return record.fields.get(field_name)

        result: dict = {}

        # fear_greed_index → st_fear_greed（归一化 ÷100）
        val = _get("fear_greed_index")
        if val is None:
            result["st_fear_greed"] = float("nan")
        elif self.config.normalize_fear_greed:
            result["st_fear_greed"] = max(0.0, min(1.0, val / 100.0))
        else:
            result["st_fear_greed"] = val

        # funding_rate_zscore → st_funding_rate（clip [-3, 3]）
        val = _get("funding_rate_zscore")
        if val is None:
            result["st_funding_rate"] = float("nan")
        elif self.config.clip_funding_rate:
            result["st_funding_rate"] = max(-3.0, min(3.0, val))
        else:
            result["st_funding_rate"] = val

        # long_short_ratio_change → st_long_short_chg（直接使用）
        val = _get("long_short_ratio_change")
        result["st_long_short_chg"] = float("nan") if val is None else val

        # open_interest_change → st_oi_change（直接使用）
        val = _get("open_interest_change")
        result["st_oi_change"] = float("nan") if val is None else val

        # liquidation_imbalance → st_liq_imbalance（clip [-1, 1]）
        val = _get("liquidation_imbalance")
        if val is None:
            result["st_liq_imbalance"] = float("nan")
        elif self.config.clip_liq_imbalance:
            result["st_liq_imbalance"] = max(-1.0, min(1.0, val))
        else:
            result["st_liq_imbalance"] = val

        # sentiment_score_ema → st_sentiment_ema（clip [0, 1]）
        val = _get("sentiment_score_ema")
        if val is None:
            result["st_sentiment_ema"] = float("nan")
        elif self.config.clip_sentiment_ema:
            result["st_sentiment_ema"] = max(0.0, min(1.0, val))
        else:
            result["st_sentiment_ema"] = val

        return result
