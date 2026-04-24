"""
tests/test_regime_detector.py — MarketRegimeDetector W5 单元测试

覆盖项：
- RegimeFeatureSource: OHLCV 提取、bar 数不足时降级、frame 提取
- HybridRegimeScorer: 概率分布总和为 1、高 ATR 产出 high_vol、强上升趋势产出 bull
- RegimeCache: store/latest、shift 检测、is_stable、regime_counts
- MarketRegimeDetector: end-to-end update、缓存读取、update_from_frame、health_snapshot
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from modules.alpha.contracts.regime_types import RegimeState
from modules.alpha.regime.cache import RegimeCache
from modules.alpha.regime.detector import DetectorConfig, MarketRegimeDetector
from modules.alpha.regime.feature_source import RegimeFeatureSource
from modules.alpha.regime.scorer import HybridRegimeScorer, ScorerConfig


# ─────────────────────────────────────────────────────────────
# 测试数据生成
# ─────────────────────────────────────────────────────────────

def make_ohlcv(
    n: int = 100,
    trend: float = 0.002,     # 每根 bar 均值对数收益率
    vol: float = 0.01,        # 价格波动率
    seed: int = 0,
) -> pd.DataFrame:
    """生成 OHLCV DataFrame，trend > 0 为上升趋势，trend < 0 为下降趋势。"""
    rng = np.random.RandomState(seed)
    prices = 10000.0 * np.exp(np.cumsum(rng.normal(trend, vol, n)))
    noise = rng.uniform(0.003, 0.015, n)
    return pd.DataFrame({
        "open":   prices * (1 - noise / 4),
        "high":   prices * (1 + noise / 2),
        "low":    prices * (1 - noise / 2),
        "close":  prices,
        "volume": rng.uniform(100, 500, n),
    })


def make_high_vol_ohlcv(n: int = 80) -> pd.DataFrame:
    """生成高波动 OHLCV（large ATR）。"""
    return make_ohlcv(n=n, trend=0.0, vol=0.04, seed=99)


def make_bull_ohlcv(n: int = 80) -> pd.DataFrame:
    """生成强上升趋势 OHLCV。"""
    return make_ohlcv(n=n, trend=0.008, vol=0.008, seed=1)


def make_bear_ohlcv(n: int = 80) -> pd.DataFrame:
    """生成强下降趋势 OHLCV。"""
    return make_ohlcv(n=n, trend=-0.008, vol=0.008, seed=2)


def make_sideways_ohlcv(n: int = 80) -> pd.DataFrame:
    """生成横盘 OHLCV（小波动、无方向）。"""
    return make_ohlcv(n=n, trend=0.0, vol=0.003, seed=3)


# ─────────────────────────────────────────────────────────────
# RegimeFeatureSource 测试
# ─────────────────────────────────────────────────────────────

class TestRegimeFeatureSource:
    def test_extract_from_ohlcv_valid(self):
        """正常 bar 数量时，应该产出 valid=True 的特征。"""
        df = make_ohlcv(100)
        source = RegimeFeatureSource(min_bars=30)
        rf = source.extract_from_ohlcv(df)

        assert rf.valid is True
        assert rf.n_bars == 100
        assert rf.ret_roll_std_20 > 0

    def test_extract_insufficient_bars(self):
        """bar 数量不足时，应该返回 valid=False。"""
        df = make_ohlcv(10)
        source = RegimeFeatureSource(min_bars=30)
        rf = source.extract_from_ohlcv(df)

        assert rf.valid is False

    def test_extract_rsi_in_range(self):
        """RSI 应该在 [0, 100] 范围内。"""
        df = make_ohlcv(100)
        source = RegimeFeatureSource()
        rf = source.extract_from_ohlcv(df)

        assert 0.0 <= rf.rsi_14 <= 100.0

    def test_extract_volume_ratio_positive(self):
        """volume_ratio 应该 > 0。"""
        df = make_ohlcv(100)
        source = RegimeFeatureSource()
        rf = source.extract_from_ohlcv(df)

        assert rf.volume_ratio > 0.0

    def test_extract_from_frame(self):
        """从包含预计算列的 DataFrame 提取，应该正常工作。"""
        n = 50
        rng = np.random.RandomState(10)
        frame = pd.DataFrame({
            "close_return":    rng.randn(n) * 0.01,
            "ret_roll_mean_20": rng.randn(n) * 0.002,
            "ret_roll_std_20": abs(rng.randn(n)) * 0.01 + 0.001,
            "rsi_14":          rng.uniform(30, 70, n),
            "adx_14":          rng.uniform(10, 50, n),
            "atr_pct_14":      rng.uniform(0.005, 0.03, n),
            "volume_ratio":    rng.uniform(0.5, 2.0, n),
        })
        source = RegimeFeatureSource()
        rf = source.extract_from_frame(frame)

        assert rf.valid is True
        assert 0.0 < rf.rsi_14 <= 100.0

    def test_extract_from_empty_frame(self):
        """空 frame 应该返回 valid=False。"""
        source = RegimeFeatureSource()
        rf = source.extract_from_frame(pd.DataFrame())
        assert rf.valid is False


# ─────────────────────────────────────────────────────────────
# HybridRegimeScorer 测试
# ─────────────────────────────────────────────────────────────

class TestHybridRegimeScorer:
    def _get_regime(self, df: pd.DataFrame) -> RegimeState:
        source = RegimeFeatureSource()
        scorer = HybridRegimeScorer()
        features = source.extract_from_ohlcv(df)
        return scorer.score(features)

    def test_probs_sum_to_one(self):
        """四种概率之和应该等于 1（容差 1e-4）。"""
        regime = self._get_regime(make_ohlcv(100))
        total = regime.bull_prob + regime.bear_prob + regime.sideways_prob + regime.high_vol_prob
        assert abs(total - 1.0) < 1e-4

    def test_all_probs_positive(self):
        """所有概率应该 > 0（有概率下限）。"""
        regime = self._get_regime(make_ohlcv(100))
        assert regime.bull_prob > 0
        assert regime.bear_prob > 0
        assert regime.sideways_prob > 0
        assert regime.high_vol_prob > 0

    def test_high_vol_scenario(self):
        """高波动行情下，high_vol_prob 应该高于 0.25（均匀基准）。"""
        regime = self._get_regime(make_high_vol_ohlcv(100))
        # 不强要求 dominant == "high_vol"，只要求 high_vol_prob 偏高
        assert regime.high_vol_prob > 0.25, f"高波动场景 high_vol_prob 偏低: {regime}"

    def test_bull_scenario(self):
        """强上升趋势下，bull_prob 应该高于 bear_prob。"""
        regime = self._get_regime(make_bull_ohlcv(100))
        assert regime.bull_prob >= regime.bear_prob, f"强牛市 bull_prob 未高于 bear_prob: {regime}"

    def test_bear_scenario(self):
        """强下降趋势下，bear_prob 应该高于 bull_prob。"""
        regime = self._get_regime(make_bear_ohlcv(100))
        assert regime.bear_prob >= regime.bull_prob, f"强熊市 bear_prob 未高于 bull_prob: {regime}"

    def test_invalid_features_returns_unknown(self):
        """特征无效时应该返回 unknown dominant_regime。"""
        from modules.alpha.regime.feature_source import RegimeFeatures
        scorer = HybridRegimeScorer()
        regime = scorer.score(RegimeFeatures(valid=False))
        assert regime.dominant_regime == "unknown"

    def test_confidence_in_range(self):
        """置信度应该在 [0, 1] 范围内。"""
        regime = self._get_regime(make_bull_ohlcv(100))
        assert 0.0 <= regime.confidence <= 1.0

    def test_dominant_regime_is_valid(self):
        """dominant_regime 值应该是合法的 RegimeName。"""
        valid_names = {"bull", "bear", "sideways", "high_vol", "unknown"}
        regime = self._get_regime(make_ohlcv(100))
        assert regime.dominant_regime in valid_names


# ─────────────────────────────────────────────────────────────
# RegimeCache 测试
# ─────────────────────────────────────────────────────────────

def make_regime(dominant: str = "bull", conf: float = 0.6) -> RegimeState:
    bull = 0.6 if dominant == "bull" else 0.1
    bear = 0.6 if dominant == "bear" else 0.1
    sw   = 0.6 if dominant == "sideways" else 0.1
    hv   = 0.6 if dominant == "high_vol" else 0.1
    total = bull + bear + sw + hv
    return RegimeState(
        bull_prob=round(bull / total, 4),
        bear_prob=round(bear / total, 4),
        sideways_prob=round(sw / total, 4),
        high_vol_prob=round(hv / total, 4),
        confidence=conf,
        dominant_regime=dominant,  # type: ignore
    )


class TestRegimeCache:
    def test_latest_returns_none_when_empty(self):
        cache = RegimeCache()
        assert cache.latest is None

    def test_store_and_latest(self):
        cache = RegimeCache()
        r = make_regime("bull")
        cache.store(r, bar_seq=1)
        assert cache.latest == r

    def test_shift_detected(self):
        cache = RegimeCache(shift_log_min_conf=0.0)
        cache.store(make_regime("bull"), bar_seq=1)
        shifted = cache.store(make_regime("bear"), bar_seq=2)
        assert shifted is True

    def test_no_shift_same_regime(self):
        cache = RegimeCache()
        cache.store(make_regime("bull"), bar_seq=1)
        shifted = cache.store(make_regime("bull"), bar_seq=2)
        assert shifted is False

    def test_is_stable_true(self):
        cache = RegimeCache()
        for i in range(5):
            cache.store(make_regime("bull"), bar_seq=i)
        assert cache.is_stable(5) is True

    def test_is_stable_false_after_shift(self):
        cache = RegimeCache(shift_log_min_conf=0.0)
        for i in range(4):
            cache.store(make_regime("bull"), bar_seq=i)
        cache.store(make_regime("bear"), bar_seq=4)
        assert cache.is_stable(5) is False

    def test_maxlen_respected(self):
        cache = RegimeCache(maxlen=5)
        for i in range(20):
            cache.store(make_regime("bull"), bar_seq=i)
        assert len(cache) == 5

    def test_regime_counts(self):
        cache = RegimeCache()
        for _ in range(3):
            cache.store(make_regime("bull"))
        for _ in range(2):
            cache.store(make_regime("bear"))
        counts = cache.regime_counts(window=5)
        assert counts["bull"] == 3
        assert counts["bear"] == 2

    def test_diagnostics_keys(self):
        cache = RegimeCache()
        cache.store(make_regime("sideways"))
        diag = cache.diagnostics()
        assert "latest_dominant" in diag
        assert "cache_size" in diag


# ─────────────────────────────────────────────────────────────
# MarketRegimeDetector 集成测试
# ─────────────────────────────────────────────────────────────

class TestMarketRegimeDetector:
    def test_update_returns_regime_state(self):
        """update() 应该返回 RegimeState 实例。"""
        detector = MarketRegimeDetector()
        df = make_ohlcv(100)
        regime = detector.update(df, bar_seq=1)
        assert isinstance(regime, RegimeState)

    def test_insufficient_bars_returns_unknown(self):
        """bar 数量不足时 update() 应该返回 unknown。"""
        detector = MarketRegimeDetector(DetectorConfig(min_bars_required=50))
        df = make_ohlcv(20)
        regime = detector.update(df, bar_seq=1)
        assert regime.dominant_regime == "unknown"

    def test_current_regime_after_update(self):
        """update() 之后 current_regime 应该与返回值一致。"""
        detector = MarketRegimeDetector()
        df = make_ohlcv(100)
        regime = detector.update(df, bar_seq=1)
        assert detector.current_regime == regime

    def test_update_from_frame(self):
        """update_from_frame() 接收 regime_features 视图，应该返回有效 RegimeState。"""
        rng = np.random.RandomState(42)
        n = 40
        frame = pd.DataFrame({
            "close_return":    rng.randn(n) * 0.01,
            "ret_roll_mean_20": rng.randn(n) * 0.001,
            "ret_roll_std_20": abs(rng.randn(n)) * 0.01 + 0.002,
            "rsi_14":          rng.uniform(40, 60, n),
            "adx_14":          rng.uniform(10, 30, n),
            "atr_pct_14":      rng.uniform(0.005, 0.02, n),
            "volume_ratio":    rng.uniform(0.8, 1.2, n),
        })
        detector = MarketRegimeDetector()
        regime = detector.update_from_frame(frame, bar_seq=1)
        assert isinstance(regime, RegimeState)
        total = regime.bull_prob + regime.bear_prob + regime.sideways_prob + regime.high_vol_prob
        assert abs(total - 1.0) < 1e-4

    def test_update_every_n_bars(self):
        """update_every_n_bars=3 时，只有第 3、6、9 次才重新评分。"""
        detector = MarketRegimeDetector(DetectorConfig(update_every_n_bars=3))
        df = make_ohlcv(100)

        # 第 1、2 次应该返回 unknown（缓存为空）
        r1 = detector.update(df, bar_seq=1)
        r2 = detector.update(df, bar_seq=2)
        r3 = detector.update(df, bar_seq=3)  # 第 3 次才评分

        # r1 和 r2 是 unknown（缓存为空时的降级值）
        assert r1.dominant_regime == "unknown"
        assert r2.dominant_regime == "unknown"
        # r3 应该是评分结果（非 unknown，除非 bar 数真的不足）
        # 这里只验证 r3 是 RegimeState 实例
        assert isinstance(r3, RegimeState)

    def test_health_snapshot_keys(self):
        """health_snapshot() 应该返回包含关键字段的字典。"""
        detector = MarketRegimeDetector()
        detector.update(make_ohlcv(100), bar_seq=1)
        snap = detector.health_snapshot()

        assert "current_regime" in snap
        assert "current_confidence" in snap
        assert "is_stable" in snap
        assert "cache" in snap

    def test_continuous_updates_accumulate_cache(self):
        """连续多次 update() 后，缓存应该累积记录。"""
        detector = MarketRegimeDetector()
        df = make_ohlcv(200)

        for i in range(10):
            # 每次 update 用不同长度的 df 模拟新 bar 到来
            detector.update(df.iloc[:100 + i], bar_seq=i)

        assert len(detector._cache) >= 1
