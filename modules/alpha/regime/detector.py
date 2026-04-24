"""
modules/alpha/regime/detector.py — MarketRegimeDetector v1

设计说明：
- 对外唯一接口：update(ohlcv_df, bar_seq) -> RegimeState
- 内部由三个组件协作：
    RegimeFeatureSource → 提取特征
    HybridRegimeScorer  → 评分产出概率分布
    RegimeCache         → 存储历史、检测切换
- 支持最小 bar 数量检查（冷启动期输出 unknown，不影响调用方）
- 支持从 DataKitchen 的 regime_features 视图直接传入，避免重复计算
- detector 本身无状态存储（除 cache），可并发调用（单线程事件循环下安全）

日志标签：[Regime]
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.logger import get_logger
from modules.alpha.contracts.regime_types import RegimeState
from modules.alpha.regime.cache import RegimeCache
from modules.alpha.regime.feature_source import RegimeFeatureSource
from modules.alpha.regime.scorer import HybridRegimeScorer, ScorerConfig

log = get_logger(__name__)

_UNKNOWN_REGIME = RegimeState(
    bull_prob=0.25,
    bear_prob=0.25,
    sideways_prob=0.25,
    high_vol_prob=0.25,
    confidence=0.0,
    dominant_regime="unknown",
)


@dataclass
class DetectorConfig:
    """MarketRegimeDetector 顶层配置。"""

    # 更新频率（每隔多少根 bar 重新评分；=1 表示每根 bar 都评分）
    update_every_n_bars: int = 1

    # RegimeFeatureSource 参数
    min_bars_required: int = 30

    # RegimeCache 参数
    cache_maxlen: int = 200
    shift_log_min_conf: float = 0.5

    # 评分器配置（使用默认值即可，支持 Optuna 后续注入）
    scorer_config: ScorerConfig | None = None


class MarketRegimeDetector:
    """
    市场环境感知器 v1（HybridRegimeDetectorV1）。

    使用示例（每根 K 线的主循环中）：
        detector = MarketRegimeDetector()

        # 方式 A：直接传 OHLCV DataFrame（含当前 bar）
        regime = detector.update(ohlcv_df=lookback_df, bar_seq=loop_seq)

        # 方式 B：已有 DataKitchen regime_features 视图（跳过重复计算）
        regime = detector.update_from_frame(regime_feature_df, bar_seq=loop_seq)

        # 快速读取缓存（不触发重新评分，可在同一 bar 多次调用）
        current_regime = detector.current_regime

    Args:
        config: DetectorConfig 实例
    """

    def __init__(self, config: DetectorConfig | None = None) -> None:
        self.config = config or DetectorConfig()

        self._feature_source = RegimeFeatureSource(
            min_bars=self.config.min_bars_required,
        )
        self._scorer = HybridRegimeScorer(
            config=self.config.scorer_config,
        )
        self._cache = RegimeCache(
            maxlen=self.config.cache_maxlen,
            shift_log_min_conf=self.config.shift_log_min_conf,
        )

        self._bar_counter = 0

        log.info(
            "[Regime] MarketRegimeDetector v1 初始化: update_every={} min_bars={} cache={}",
            self.config.update_every_n_bars,
            self.config.min_bars_required,
            self.config.cache_maxlen,
        )

    # ────────────────────────────────────────────────────────────
    # 主更新接口
    # ────────────────────────────────────────────────────────────

    def update(
        self,
        ohlcv_df: pd.DataFrame,
        bar_seq: int = 0,
    ) -> RegimeState:
        """
        从原始 OHLCV DataFrame 更新 Regime 状态。

        Args:
            ohlcv_df:  原始 OHLCV DataFrame，包含 [open, high, low, close, volume]
            bar_seq:   当前 loop_seq（用于日志 / 缓存记录）

        Returns:
            最新 RegimeState（若未到更新间隔则返回缓存值）
        """
        self._bar_counter += 1

        # 更新频率控制：未到间隔时返回缓存
        if self._bar_counter % self.config.update_every_n_bars != 0:
            return self._cache.latest or _UNKNOWN_REGIME

        # 提取特征
        features = self._feature_source.extract_from_ohlcv(ohlcv_df)

        if not features.valid:
            log.debug(
                "[Regime] 特征无效(bar_seq={}), 保持 unknown 状态", bar_seq,
            )
            return _UNKNOWN_REGIME

        # 评分
        regime = self._scorer.score(features)

        # 缓存 + 切换检测
        self._cache.store(regime, bar_seq=bar_seq)

        log.debug(
            "[Regime] update完成: bar_seq={} dominant={} conf={:.3f} "
            "stable={} cache_size={}",
            bar_seq, regime.dominant_regime, regime.confidence,
            self._cache.is_stable(5), len(self._cache),
        )

        return regime

    def update_from_frame(
        self,
        regime_feature_df: pd.DataFrame,
        bar_seq: int = 0,
    ) -> RegimeState:
        """
        从 DataKitchen regime_features 视图更新（避免重复计算技术指标）。

        Args:
            regime_feature_df: DataKitchen.transform()["regime_features"]
            bar_seq:           当前 loop_seq

        Returns:
            最新 RegimeState
        """
        self._bar_counter += 1

        if self._bar_counter % self.config.update_every_n_bars != 0:
            return self._cache.latest or _UNKNOWN_REGIME

        features = self._feature_source.extract_from_frame(regime_feature_df)

        if not features.valid:
            return _UNKNOWN_REGIME

        regime = self._scorer.score(features)
        self._cache.store(regime, bar_seq=bar_seq)

        log.debug(
            "[Regime] update_from_frame完成: bar_seq={} dominant={} conf={:.3f}",
            bar_seq, regime.dominant_regime, regime.confidence,
        )

        return regime

    # ────────────────────────────────────────────────────────────
    # 快速读取接口
    # ────────────────────────────────────────────────────────────

    @property
    def current_regime(self) -> RegimeState:
        """最新缓存的 RegimeState（不触发重评分，可高频调用）。"""
        return self._cache.latest or _UNKNOWN_REGIME

    @property
    def is_stable(self) -> bool:
        """最近 5 根 bar 的 dominant_regime 是否一致。"""
        return self._cache.is_stable(window=5)

    # ────────────────────────────────────────────────────────────
    # 健康诊断
    # ────────────────────────────────────────────────────────────

    def health_snapshot(self) -> dict:
        """返回 detector 当前状态的诊断快照（用于 API 或日志）。"""
        return {
            "bar_counter": self._bar_counter,
            "current_regime": self.current_regime.dominant_regime,
            "current_confidence": self.current_regime.confidence,
            "is_stable": self.is_stable,
            "cache": self._cache.diagnostics(),
        }
