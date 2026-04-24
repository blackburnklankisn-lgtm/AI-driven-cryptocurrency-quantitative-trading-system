"""
modules/alpha/ml/meta_learner.py — 多模型投票融合 (W8)

设计要点：
- 接收 `List[ModelVote]`（由 ModelEnsemble.predict() 产出）
- 支持三种融合策略：
    "weighted_avg"  — 加权平均买入概率 → 二次阈值判断（默认，轻量首选）
    "majority_vote" — 权重加权多数票
    "confidence"    — 选置信度（概率远离 0.5）最高的模型
- 输出 `MetaSignal`（合约层已定义）
- 0 票 / 全 NaN 时返回 HOLD，打 WARNING
- 单模型时直接透传，打 INFO（降级运行）

接口：
    MetaLearnerConfig(fusion_strategy, buy_threshold, sell_threshold)
    MetaLearner(config)
        .fuse(votes)  → MetaSignal
        .diagnostics() → dict

日志标签：[Meta]  [MetaVote]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal

import numpy as np

from core.logger import get_logger
from modules.alpha.contracts.ensemble_types import MetaSignal, ModelVote

log = get_logger(__name__)

FusionStrategy = Literal["weighted_avg", "majority_vote", "confidence"]

_VALID_STRATEGIES: set[str] = {"weighted_avg", "majority_vote", "confidence"}


# ─────────────────────────────────────────────────────────────
# MetaLearnerConfig
# ─────────────────────────────────────────────────────────────

@dataclass
class MetaLearnerConfig:
    """MetaLearner 配置项。"""

    fusion_strategy: FusionStrategy = "weighted_avg"
    buy_threshold: float = 0.60
    sell_threshold: float = 0.40

    def __post_init__(self) -> None:
        if self.fusion_strategy not in _VALID_STRATEGIES:
            raise ValueError(
                f"fusion_strategy 必须是 {_VALID_STRATEGIES}，实际: {self.fusion_strategy}"
            )
        if not (0.0 < self.sell_threshold < self.buy_threshold < 1.0):
            raise ValueError(
                f"阈值必须满足 0 < sell({self.sell_threshold}) "
                f"< buy({self.buy_threshold}) < 1"
            )


# ─────────────────────────────────────────────────────────────
# MetaLearner
# ─────────────────────────────────────────────────────────────

class MetaLearner:
    """
    多模型投票融合器。

    使用示例：
        learner = MetaLearner()
        votes   = ensemble.predict(feature_df)
        signal  = learner.fuse(votes)
        print(signal.final_action, signal.final_confidence)
    """

    def __init__(self, config: MetaLearnerConfig | None = None) -> None:
        self._config = config or MetaLearnerConfig()
        self._n_fuse_calls = 0
        self._n_degraded = 0   # 降级（0 vote 或单模型）次数
        log.info(
            "[Meta] 初始化: fusion={} buy_thr={} sell_thr={}",
            self._config.fusion_strategy,
            self._config.buy_threshold,
            self._config.sell_threshold,
        )

    # ── 核心融合入口 ──────────────────────────────────────────

    def fuse(self, votes: List[ModelVote]) -> MetaSignal:
        """将 ModelVote 列表融合为单一 MetaSignal。

        Args:
            votes:  ModelEnsemble.predict() 的输出列表（可为空）

        Returns:
            MetaSignal — 最终交易意向信号
        """
        self._n_fuse_calls += 1

        # ── 0 票 / 全空 ────────────────────────────────────────
        if not votes:
            log.warning("[Meta] 收到 0 个有效 ModelVote，降级返回 HOLD")
            self._n_degraded += 1
            return MetaSignal(
                final_action="HOLD",
                final_confidence=0.0,
                dominant_model="none",
                model_votes=[],
                debug_payload={"reason": "empty_votes"},
            )

        # ── 单模型透传 ────────────────────────────────────────
        if len(votes) == 1:
            v = votes[0]
            log.info(
                "[Meta] 单模型运行（降级）: model={} action={} prob={:.4f}",
                v.model_name, v.action, v.buy_probability,
            )
            self._n_degraded += 1
            return MetaSignal(
                final_action=v.action,
                final_confidence=abs(v.buy_probability - 0.5) * 2,
                dominant_model=v.model_name,
                model_votes=list(votes),
                debug_payload={"reason": "single_model", "buy_probability": v.buy_probability},
            )

        # ── 融合 ──────────────────────────────────────────────
        strategy = self._config.fusion_strategy
        if strategy == "weighted_avg":
            signal = self._fuse_weighted_avg(votes)
        elif strategy == "majority_vote":
            signal = self._fuse_majority_vote(votes)
        else:  # "confidence"
            signal = self._fuse_confidence(votes)

        log.info(
            "[Meta] 融合完成: strategy={} n_votes={} action={} confidence={:.4f} dominant={}",
            strategy,
            len(votes),
            signal.final_action,
            signal.final_confidence,
            signal.dominant_model,
        )
        return signal

    # ── 策略一：加权平均 ─────────────────────────────────────

    def _fuse_weighted_avg(self, votes: List[ModelVote]) -> MetaSignal:
        """加权平均买入概率，再做二次阈值判断。"""
        total_weight = sum(v.weight for v in votes)
        if total_weight == 0:
            return self._fallback_hold(votes, "zero_weight_sum")

        avg_prob = sum(v.buy_probability * v.weight for v in votes) / total_weight

        if avg_prob >= self._config.buy_threshold:
            action = "BUY"
        elif avg_prob <= self._config.sell_threshold:
            action = "SELL"
        else:
            action = "HOLD"

        # dominant model = 最高权重 × 概率贡献的那个
        dominant = max(votes, key=lambda v: v.buy_probability * v.weight).model_name
        confidence = abs(avg_prob - 0.5) * 2

        log.debug(
            "[MetaVote] weighted_avg: avg_prob={:.4f} action={} dominant={}",
            avg_prob, action, dominant,
        )
        return MetaSignal(
            final_action=action,
            final_confidence=confidence,
            dominant_model=dominant,
            model_votes=list(votes),
            debug_payload={
                "strategy": "weighted_avg",
                "avg_buy_probability": avg_prob,
                "total_weight": total_weight,
                "vote_detail": [
                    {"model": v.model_name, "prob": v.buy_probability,
                     "action": v.action, "weight": v.weight}
                    for v in votes
                ],
            },
        )

    # ── 策略二：加权多数票 ────────────────────────────────────

    def _fuse_majority_vote(self, votes: List[ModelVote]) -> MetaSignal:
        """每个 action 按权重累积；得票最高者胜出。"""
        tally: dict[str, float] = {"BUY": 0.0, "SELL": 0.0, "HOLD": 0.0}
        for v in votes:
            tally[v.action] += v.weight

        final_action = max(tally, key=lambda k: tally[k])

        # confidence = 胜出 action 的权重占比
        total_w = sum(tally.values())
        confidence = tally[final_action] / total_w if total_w > 0 else 0.0

        # dominant = 在胜出 action 中权重最大的模型
        winners = [v for v in votes if v.action == final_action]
        dominant = max(winners, key=lambda v: v.weight).model_name if winners else votes[0].model_name

        log.debug(
            "[MetaVote] majority_vote: tally={} action={} dominant={}",
            tally, final_action, dominant,
        )
        return MetaSignal(
            final_action=final_action,
            final_confidence=confidence,
            dominant_model=dominant,
            model_votes=list(votes),
            debug_payload={
                "strategy": "majority_vote",
                "tally": tally,
                "vote_detail": [
                    {"model": v.model_name, "action": v.action, "weight": v.weight}
                    for v in votes
                ],
            },
        )

    # ── 策略三：最高置信模型透传 ──────────────────────────────

    def _fuse_confidence(self, votes: List[ModelVote]) -> MetaSignal:
        """选出概率距 0.5 最远的模型，直接透传其结论。"""
        best = max(votes, key=lambda v: abs(v.buy_probability - 0.5))
        confidence = abs(best.buy_probability - 0.5) * 2

        log.debug(
            "[MetaVote] confidence: best_model={} prob={:.4f} action={}",
            best.model_name, best.buy_probability, best.action,
        )
        return MetaSignal(
            final_action=best.action,
            final_confidence=confidence,
            dominant_model=best.model_name,
            model_votes=list(votes),
            debug_payload={
                "strategy": "confidence",
                "best_model": best.model_name,
                "best_prob": best.buy_probability,
                "vote_detail": [
                    {"model": v.model_name, "prob": v.buy_probability, "action": v.action}
                    for v in votes
                ],
            },
        )

    # ── 降级辅助 ──────────────────────────────────────────────

    def _fallback_hold(self, votes: List[ModelVote], reason: str) -> MetaSignal:
        log.warning("[Meta] 降级 HOLD: reason={}", reason)
        self._n_degraded += 1
        return MetaSignal(
            final_action="HOLD",
            final_confidence=0.0,
            dominant_model="none",
            model_votes=list(votes),
            debug_payload={"reason": reason},
        )

    # ── 诊断 ──────────────────────────────────────────────────

    def diagnostics(self) -> dict:
        return {
            "fusion_strategy": self._config.fusion_strategy,
            "buy_threshold": self._config.buy_threshold,
            "sell_threshold": self._config.sell_threshold,
            "n_fuse_calls": self._n_fuse_calls,
            "n_degraded": self._n_degraded,
        }
