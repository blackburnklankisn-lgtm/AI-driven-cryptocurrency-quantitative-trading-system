"""
modules/alpha/regime/scorer.py — HybridRegimeDetectorV1 评分引擎

设计说明：
- 第一版采用"规则 + 统计"混合实现，不强依赖完整 HMM 训练器
- 三个子评分器分别评估：趋势方向、波动率状态、动量状态
- 最终以加权融合方式输出 RegimeState 概率分布
- 接口设计确保后续可以无缝替换为 HMM 实现

评分逻辑概述：
  波动率评分 → high_vol_prob（ATR / BB宽度 / 收益率标准差）
  趋势评分   → bull_prob / bear_prob（价格偏离SMA / ADX / 方向）
  动量评分   → 修正 bull/bear 概率（RSI / ret_roll_mean）
  sideways   → 1 - (bull + bear + high_vol) 的残差

日志标签：[RegimeScorer]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from core.logger import get_logger
from modules.alpha.contracts.regime_types import RegimeName, RegimeState
from modules.alpha.regime.feature_source import RegimeFeatures

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 评分器配置
# ══════════════════════════════════════════════════════════════

@dataclass
class ScorerConfig:
    """评分超参数（可通过 Optuna 调优）。"""

    # 波动率评分权重
    vol_weight: float = 0.35      # ATR/BB宽度评分在最终合并时的权重
    trend_weight: float = 0.45    # 趋势评分权重
    momentum_weight: float = 0.20 # 动量修正权重

    # 波动率判断阈值
    atr_pct_high: float = 0.025   # ATR > 这个值视为高波动
    atr_pct_mid: float = 0.012    # ATR < 这个值视为低波动
    bb_width_high: float = 0.06   # BB宽度 > 这个值视为高波动
    ret_std_high: float = 0.018   # 收益率标准差 > 这个值视为高波动

    # 趋势判断阈值
    adx_trend: float = 25.0       # ADX > 这个值有趋势
    adx_strong: float = 40.0      # ADX > 这个值强趋势
    sma20_bias_threshold: float = 0.015   # 价格偏离 SMA20 超过这个值才有方向信号
    sma50_bias_threshold: float = 0.030   # 价格偏离 SMA50 超过这个值才有方向信号

    # 动量阈值
    rsi_overbought: float = 65.0  # RSI > 这个值偏多头
    rsi_oversold: float = 35.0    # RSI < 这个值偏空头
    ret_mean_up: float = 0.001    # 滚动均收益率 > 这个值偏多
    ret_mean_down: float = -0.001 # 滚动均收益率 < 这个值偏空

    # 置信度阈值（低于此值降级为 unknown）
    confidence_floor: float = 0.45

    # 各类别概率最小值（避免概率硬零）
    prob_floor: float = 0.05


# ══════════════════════════════════════════════════════════════
# 评分器
# ══════════════════════════════════════════════════════════════

class HybridRegimeScorer:
    """
    混合规则/统计 Regime 评分器 v1。

    接口：
        scorer = HybridRegimeScorer(config=ScorerConfig())
        regime_state = scorer.score(features)  # features: RegimeFeatures

    后续升级路径：
        - 替换 score() 内部实现为 HMM inference（接口不变）
        - 或串联为 scorer.score_hmm() + scorer.score_rules() 的加权融合
    """

    def __init__(self, config: ScorerConfig | None = None) -> None:
        self.config = config or ScorerConfig()

    def score(self, features: RegimeFeatures) -> RegimeState:
        """
        给定 RegimeFeatures，输出 RegimeState 概率分布。

        Args:
            features: RegimeFeatureSource 提取的特征结构

        Returns:
            RegimeState（frozen dataclass）
        """
        cfg = self.config

        if not features.valid:
            log.warning("[RegimeScorer] 特征无效(valid=False)，返回 unknown regime")
            return self._unknown_state()

        # Step 1: 波动率评分
        high_vol_score = self._score_volatility(features)

        # Step 2: 趋势方向评分（bull / bear 原始得分）
        bull_raw, bear_raw = self._score_trend(features)

        # Step 3: 动量修正
        bull_adj, bear_adj = self._score_momentum(features, bull_raw, bear_raw)

        # Step 4: 高波动时压缩 bull/bear，提升 high_vol
        # 逻辑：如果是高波动市场，bull/bear 方向信号置信度下降
        vol_suppression = high_vol_score * 0.6
        bull_adj = bull_adj * (1.0 - vol_suppression)
        bear_adj = bear_adj * (1.0 - vol_suppression)

        # Step 5: 归一化到概率空间
        total_directional = bull_adj + bear_adj + high_vol_score
        if total_directional < 1e-8:
            if self._looks_sideways(features):
                return self._sideways_state()
            return self._unknown_state()

        bull_prob = bull_adj / total_directional
        bear_prob = bear_adj / total_directional
        high_vol_prob = high_vol_score / total_directional

        # Step 6: 计算 sideways（剩余概率）
        sideways_prob = max(0.0, 1.0 - bull_prob - bear_prob - high_vol_prob)

        # Step 7: 加概率下限（避免硬零，保留可解释性）
        floor = cfg.prob_floor
        bull_prob = max(bull_prob, floor)
        bear_prob = max(bear_prob, floor)
        sideways_prob = max(sideways_prob, floor)
        high_vol_prob = max(high_vol_prob, floor)

        # 重新归一化
        total = bull_prob + bear_prob + sideways_prob + high_vol_prob
        bull_prob /= total
        bear_prob /= total
        sideways_prob /= total
        high_vol_prob /= total

        # Step 8: 计算置信度（dominant 概率 - 次高概率的差值）
        probs = [bull_prob, bear_prob, sideways_prob, high_vol_prob]
        sorted_probs = sorted(probs, reverse=True)
        confidence = sorted_probs[0] - sorted_probs[1]

        # Step 9: 确定 dominant regime
        dominant = self._dominant(bull_prob, bear_prob, sideways_prob, high_vol_prob)
        if confidence < cfg.confidence_floor:
            if self._looks_sideways(features):
                return self._sideways_state()
            dominant = "unknown"

        regime = RegimeState(
            bull_prob=round(bull_prob, 4),
            bear_prob=round(bear_prob, 4),
            sideways_prob=round(sideways_prob, 4),
            high_vol_prob=round(high_vol_prob, 4),
            confidence=round(confidence, 4),
            dominant_regime=dominant,
        )

        log.debug(
            "[RegimeScorer] 评分完成: bull={:.3f} bear={:.3f} "
            "sideways={:.3f} high_vol={:.3f} conf={:.3f} dominant={}",
            regime.bull_prob, regime.bear_prob,
            regime.sideways_prob, regime.high_vol_prob,
            regime.confidence, regime.dominant_regime,
        )

        return regime

    # ────────────────────────────────────────────────────────────
    # 子评分器
    # ────────────────────────────────────────────────────────────

    def _score_volatility(self, f: RegimeFeatures) -> float:
        """
        波动率得分（0~1），综合 ATR / BB宽度 / 收益率标准差。
        """
        cfg = self.config
        scores = []

        # ATR 评分
        if f.atr_pct > cfg.atr_pct_high:
            scores.append(1.0)
        elif f.atr_pct > cfg.atr_pct_mid:
            scores.append(0.5)
        else:
            scores.append(0.0)

        # BB宽度评分
        if f.bb_width > cfg.bb_width_high:
            scores.append(0.8)
        elif f.bb_width > cfg.bb_width_high * 0.5:
            scores.append(0.3)
        else:
            scores.append(0.0)

        # 收益率标准差评分
        if f.ret_roll_std_20 > cfg.ret_std_high:
            scores.append(1.0)
        elif f.ret_roll_std_20 > cfg.ret_std_high * 0.5:
            scores.append(0.4)
        else:
            scores.append(0.0)

        vol_score = float(sum(scores) / len(scores))
        log.debug(
            "[RegimeScorer/vol] atr_pct={:.4f} bb_w={:.4f} ret_std={:.4f} vol_score={:.3f}",
            f.atr_pct, f.bb_width, f.ret_roll_std_20, vol_score,
        )
        return vol_score

    def _score_trend(self, f: RegimeFeatures) -> tuple[float, float]:
        """
        趋势方向评分。
        返回 (bull_score, bear_score)，各 0~1。
        """
        cfg = self.config

        # ADX 趋势强度系数（没有趋势时，方向信号权重降低）
        if f.adx > cfg.adx_strong:
            adx_factor = 1.0
        elif f.adx > cfg.adx_trend:
            adx_factor = 0.6
        else:
            adx_factor = 0.2

        # 价格偏离 SMA20 方向
        bias20_bull = max(0.0, f.price_vs_sma20 - cfg.sma20_bias_threshold)
        bias20_bear = max(0.0, -f.price_vs_sma20 - cfg.sma20_bias_threshold)

        # 价格偏离 SMA50 方向
        bias50_bull = max(0.0, f.price_vs_sma50 - cfg.sma50_bias_threshold)
        bias50_bear = max(0.0, -f.price_vs_sma50 - cfg.sma50_bias_threshold)

        # 合并（SMA20 权重 0.6，SMA50 权重 0.4）
        combined_bull = 0.6 * min(bias20_bull / 0.05, 1.0) + 0.4 * min(bias50_bull / 0.05, 1.0)
        combined_bear = 0.6 * min(bias20_bear / 0.05, 1.0) + 0.4 * min(bias50_bear / 0.05, 1.0)

        bull_score = combined_bull * adx_factor
        bear_score = combined_bear * adx_factor

        log.debug(
            "[RegimeScorer/trend] adx={:.1f} adx_factor={:.2f} "
            "sma20={:.4f} sma50={:.4f} bull={:.3f} bear={:.3f}",
            f.adx, adx_factor, f.price_vs_sma20, f.price_vs_sma50,
            bull_score, bear_score,
        )
        return bull_score, bear_score

    def _score_momentum(
        self,
        f: RegimeFeatures,
        bull_raw: float,
        bear_raw: float,
    ) -> tuple[float, float]:
        """
        使用 RSI 和滚动均收益率对趋势评分做动量修正。
        返回 (adjusted_bull, adjusted_bear)。
        """
        cfg = self.config

        # RSI 修正因子
        if f.rsi_14 > cfg.rsi_overbought:
            rsi_bull_boost = 0.3
            rsi_bear_boost = -0.1
        elif f.rsi_14 < cfg.rsi_oversold:
            rsi_bull_boost = -0.1
            rsi_bear_boost = 0.3
        else:
            rsi_bull_boost = 0.0
            rsi_bear_boost = 0.0

        # 滚动均收益率修正
        if f.ret_roll_mean_20 > cfg.ret_mean_up:
            ret_bull_boost = 0.2
            ret_bear_boost = -0.05
        elif f.ret_roll_mean_20 < cfg.ret_mean_down:
            ret_bull_boost = -0.05
            ret_bear_boost = 0.2
        else:
            ret_bull_boost = 0.0
            ret_bear_boost = 0.0

        # 合并修正（动量 weight 作为强度系数）
        mw = cfg.momentum_weight
        bull_adj = bull_raw + mw * (rsi_bull_boost + ret_bull_boost)
        bear_adj = bear_raw + mw * (rsi_bear_boost + ret_bear_boost)

        # 下限 0
        bull_adj = max(0.0, bull_adj)
        bear_adj = max(0.0, bear_adj)

        log.debug(
            "[RegimeScorer/momentum] rsi={:.1f} ret_mean={:.4f} "
            "bull: {:.3f}→{:.3f} bear: {:.3f}→{:.3f}",
            f.rsi_14, f.ret_roll_mean_20, bull_raw, bull_adj, bear_raw, bear_adj,
        )
        return bull_adj, bear_adj

    # ────────────────────────────────────────────────────────────
    # 辅助
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def _dominant(
        bull: float, bear: float, sideways: float, high_vol: float
    ) -> RegimeName:
        mapping = {
            "bull": bull,
            "bear": bear,
            "sideways": sideways,
            "high_vol": high_vol,
        }
        return max(mapping, key=lambda k: mapping[k])  # type: ignore[return-value]

    @staticmethod
    def _unknown_state() -> RegimeState:
        return RegimeState(
            bull_prob=0.25,
            bear_prob=0.25,
            sideways_prob=0.25,
            high_vol_prob=0.25,
            confidence=0.0,
            dominant_regime="unknown",
        )

    def _looks_sideways(self, f: RegimeFeatures) -> bool:
        cfg = self.config
        return (
            abs(f.price_vs_sma20) <= cfg.sma20_bias_threshold
            and abs(f.price_vs_sma50) <= cfg.sma50_bias_threshold
            and f.adx <= cfg.adx_strong
            and f.atr_pct <= cfg.atr_pct_mid
            and f.bb_width <= cfg.bb_width_high * 0.5
            and f.ret_roll_std_20 <= cfg.ret_std_high * 0.5
            and cfg.ret_mean_down <= f.ret_roll_mean_20 <= cfg.ret_mean_up
            and cfg.rsi_oversold <= f.rsi_14 <= cfg.rsi_overbought
        )

    @staticmethod
    def _sideways_state() -> RegimeState:
        return RegimeState(
            bull_prob=0.1,
            bear_prob=0.1,
            sideways_prob=0.7,
            high_vol_prob=0.1,
            confidence=0.6,
            dominant_regime="sideways",
        )
