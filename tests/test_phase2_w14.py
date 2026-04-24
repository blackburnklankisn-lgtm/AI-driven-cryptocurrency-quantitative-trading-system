"""
tests/test_phase2_w14.py — Phase 2 W14 多维 Alpha 融合层单元测试

覆盖项：
- SourceSignal: from_meta_signal、from_score、effective_score、to_dict
- FusionDecision: is_actionable、to_dict
- OmniSignalFusion: 正常融合、freshness 过滤、风险压制、技术面 fallback、空列表、降级
- MetaLearnerV2: 技术面独立运行、外部 signal 融合、risk_snapshot 影响、
                 fuse_from_meta_signal、diagnostics
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from modules.alpha.contracts.alpha_source_types import FusionDecision, SourceSignal
from modules.alpha.contracts.ensemble_types import MetaSignal, ModelVote
from modules.alpha.ml.meta_learner_v2 import MetaLearnerV2, MetaLearnerV2Config
from modules.alpha.ml.omni_signal_fusion import OmniSignalFusion, OmniSignalFusionConfig


# ─────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────

def make_meta_signal(
    action: str = "BUY",
    confidence: float = 0.7,
    dominant_model: str = "rf",
) -> MetaSignal:
    return MetaSignal(
        final_action=action,
        final_confidence=confidence,
        dominant_model=dominant_model,
        model_votes=[],
        debug_payload={},
    )


def make_source_signal(
    source: str = "technical",
    action: str = "BUY",
    confidence: float = 0.6,
    score: float = 0.6,
    freshness_ok: bool = True,
    weight: float = 1.0,
) -> SourceSignal:
    return SourceSignal(
        source_name=source,  # type: ignore[arg-type]
        action=action,  # type: ignore[arg-type]
        confidence=confidence,
        score=score,
        freshness_ok=freshness_ok,
        weight=weight,
    )


def make_votes(n: int = 2, action: str = "BUY", prob: float = 0.75) -> list[ModelVote]:
    return [
        ModelVote(model_name=f"m{i}", buy_probability=prob, action=action, weight=1.0)
        for i in range(n)
    ]


# ─────────────────────────────────────────────────────────────
# SourceSignal 测试
# ─────────────────────────────────────────────────────────────

class TestSourceSignal:
    def test_from_meta_signal_buy(self):
        meta = make_meta_signal("BUY", 0.8)
        sig = SourceSignal.from_meta_signal(meta)
        assert sig.source_name == "technical"
        assert sig.action == "BUY"
        assert sig.score > 0
        assert sig.confidence == pytest.approx(0.8)

    def test_from_meta_signal_sell(self):
        meta = make_meta_signal("SELL", 0.65)
        sig = SourceSignal.from_meta_signal(meta)
        assert sig.action == "SELL"
        assert sig.score < 0
        assert sig.confidence == pytest.approx(0.65)

    def test_from_meta_signal_hold(self):
        meta = make_meta_signal("HOLD", 0.3)
        sig = SourceSignal.from_meta_signal(meta)
        assert sig.action == "HOLD"
        assert sig.score == pytest.approx(0.0)

    def test_from_score_buy(self):
        sig = SourceSignal.from_score("onchain", score=0.5)
        assert sig.action == "BUY"
        assert sig.confidence == pytest.approx(0.5)

    def test_from_score_sell(self):
        sig = SourceSignal.from_score("sentiment", score=-0.4)
        assert sig.action == "SELL"

    def test_from_score_hold(self):
        sig = SourceSignal.from_score("onchain", score=0.1)
        assert sig.action == "HOLD"

    def test_from_score_clips_range(self):
        sig = SourceSignal.from_score("onchain", score=5.0)
        assert sig.score == pytest.approx(1.0)

    def test_effective_score_when_fresh(self):
        sig = make_source_signal(freshness_ok=True, score=0.6)
        assert sig.effective_score() == pytest.approx(0.6)

    def test_effective_score_when_stale(self):
        sig = make_source_signal(freshness_ok=False, score=0.6)
        assert sig.effective_score() == pytest.approx(0.0)

    def test_to_dict_keys(self):
        sig = make_source_signal()
        d = sig.to_dict()
        for key in ("source_name", "action", "confidence", "score", "freshness_ok", "weight"):
            assert key in d


# ─────────────────────────────────────────────────────────────
# FusionDecision 测试
# ─────────────────────────────────────────────────────────────

class TestFusionDecision:
    def test_is_actionable_buy(self):
        d = FusionDecision("BUY", 0.7, "technical")
        assert d.is_actionable() is True

    def test_is_actionable_sell(self):
        d = FusionDecision("SELL", 0.6, "technical")
        assert d.is_actionable() is True

    def test_is_actionable_hold(self):
        d = FusionDecision("HOLD", 0.1, "technical")
        assert d.is_actionable() is False

    def test_to_dict_structure(self):
        d = FusionDecision("BUY", 0.7, "technical")
        result = d.to_dict()
        for key in ("final_action", "final_confidence", "dominant_source",
                    "source_signals", "debug_payload"):
            assert key in result


# ─────────────────────────────────────────────────────────────
# OmniSignalFusion 测试
# ─────────────────────────────────────────────────────────────

class TestOmniSignalFusion:
    def _make_fusion(self, **kwargs) -> OmniSignalFusion:
        cfg = OmniSignalFusionConfig(**kwargs)
        return OmniSignalFusion(cfg)

    def test_config_invalid_sell_threshold_raises(self):
        with pytest.raises(ValueError):
            OmniSignalFusionConfig(sell_threshold=0.1)  # 必须为负

    def test_config_invalid_buy_threshold_raises(self):
        with pytest.raises(ValueError):
            OmniSignalFusionConfig(buy_threshold=-0.1)  # 必须为正

    def test_single_technical_buy(self):
        fusion = OmniSignalFusion()
        sig = make_source_signal("technical", "BUY", score=0.7)
        decision = fusion.fuse([sig])
        assert decision.final_action == "BUY"

    def test_single_technical_sell(self):
        fusion = OmniSignalFusion()
        sig = make_source_signal("technical", "SELL", score=-0.7)
        decision = fusion.fuse([sig])
        assert decision.final_action == "SELL"

    def test_single_technical_hold(self):
        fusion = OmniSignalFusion()
        sig = make_source_signal("technical", "HOLD", score=0.05)
        decision = fusion.fuse([sig])
        assert decision.final_action == "HOLD"

    def test_empty_signals_returns_hold(self):
        fusion = OmniSignalFusion()
        decision = fusion.fuse([])
        assert decision.final_action == "HOLD"
        assert decision.dominant_source == "none"

    def test_stale_source_zeroed_out(self):
        """外部 source stale 时权重为 0，不影响融合结果。"""
        fusion = self._make_fusion(buy_threshold=0.15)
        tech = make_source_signal("technical", "BUY", score=0.6)
        onchain = make_source_signal("onchain", "SELL", score=-0.9, freshness_ok=False)
        decision = fusion.fuse([tech, onchain])
        # onchain stale → 只有 tech 生效 → BUY
        assert decision.final_action == "BUY"

    def test_freshness_ok_sources_all_participate(self):
        """两个 fresh source 都参与融合，结果是它们的加权均值。"""
        cfg = OmniSignalFusionConfig(
            buy_threshold=0.15,
            technical_base_weight=1.0,
            onchain_base_weight=1.0,
        )
        fusion = OmniSignalFusion(cfg)
        tech = make_source_signal("technical", "BUY", score=0.4)
        onchain = make_source_signal("onchain", "BUY", score=0.3)
        decision = fusion.fuse([tech, onchain])
        assert decision.final_action == "BUY"
        # aggregate ≈ (0.4×1 + 0.3×1) / 2 = 0.35 > 0.15 → BUY

    def test_high_risk_reduces_external_weight(self):
        """高风险时外部 source 权重被压制，技术面主导。"""
        cfg = OmniSignalFusionConfig(
            buy_threshold=0.15,
            sell_threshold=-0.15,
            technical_base_weight=1.0,
            onchain_base_weight=1.0,
            risk_penalty_threshold=0.05,
            risk_penalty_factor=0.0,  # 外部 source 完全压制
        )
        fusion = OmniSignalFusion(cfg)
        tech = make_source_signal("technical", "BUY", score=0.6)
        onchain = make_source_signal("onchain", "SELL", score=-0.9)
        risk = {"current_drawdown": 0.10, "kill_switch_active": False}
        decision = fusion.fuse([tech, onchain], risk_snapshot=risk)
        # onchain 被压制 → tech 主导 → BUY
        assert decision.final_action == "BUY"

    def test_no_risk_snapshot_no_penalty(self):
        """没有 risk_snapshot 时不触发风险压制。"""
        cfg = OmniSignalFusionConfig(
            risk_penalty_factor=0.0,
            risk_penalty_threshold=0.05,
        )
        fusion = OmniSignalFusion(cfg)
        tech = make_source_signal("technical", "BUY", score=0.4)
        onchain = make_source_signal("onchain", "BUY", score=0.4)
        decision = fusion.fuse([tech, onchain], risk_snapshot=None)
        # 无 risk_snapshot → 正常融合，不压制
        assert decision.final_action == "BUY"

    def test_kill_switch_active_triggers_risk(self):
        """kill_switch_active=True 应触发外部 source 降权。"""
        cfg = OmniSignalFusionConfig(
            buy_threshold=0.15,
            sell_threshold=-0.15,
            technical_base_weight=1.0,
            onchain_base_weight=1.0,
            risk_penalty_factor=0.0,  # 完全压制
        )
        fusion = OmniSignalFusion(cfg)
        tech = make_source_signal("technical", "BUY", score=0.5)
        onchain = make_source_signal("onchain", "SELL", score=-0.9)
        risk = {"current_drawdown": 0.01, "kill_switch_active": True}
        decision = fusion.fuse([tech, onchain], risk_snapshot=risk)
        assert decision.final_action == "BUY"  # onchain 被压制

    def test_technical_only_fallback_when_all_external_stale(self):
        """所有外部 source 均 stale 时，降级为 technical-only。"""
        fusion = OmniSignalFusion(OmniSignalFusionConfig(technical_only_fallback=True))
        tech = make_source_signal("technical", "BUY", score=0.7)
        onchain = make_source_signal("onchain", "HOLD", score=0.0, freshness_ok=False)
        decision = fusion.fuse([tech, onchain])
        assert decision.final_action == "BUY"
        assert "technical_only_fallback" in decision.debug_payload.get("reason", "")

    def test_dedup_same_source(self):
        """同一 source 重复提交时只保留最后一个。"""
        fusion = OmniSignalFusion()
        sig1 = make_source_signal("technical", "BUY", score=0.8)
        sig2 = make_source_signal("technical", "SELL", score=-0.9)
        decision = fusion.fuse([sig1, sig2])
        # 只保留 sig2（SELL），结果应为 SELL
        assert decision.final_action == "SELL"

    def test_source_signals_in_decision(self):
        """FusionDecision 中应包含所有参与融合的 source_signals。"""
        fusion = OmniSignalFusion()
        tech = make_source_signal("technical", score=0.5)
        onchain = make_source_signal("onchain", score=0.3)
        decision = fusion.fuse([tech, onchain])
        names = {s.source_name for s in decision.source_signals}
        assert "technical" in names
        assert "onchain" in names

    def test_diagnostics_structure(self):
        fusion = OmniSignalFusion()
        fusion.fuse([make_source_signal()])
        diag = fusion.diagnostics()
        assert "n_fuse_calls" in diag
        assert diag["n_fuse_calls"] == 1

    def test_contradictory_signals_produces_hold(self):
        """技术面 BUY + 链上 SELL 等权重 → aggregate ≈ 0 → HOLD。"""
        cfg = OmniSignalFusionConfig(
            buy_threshold=0.15,
            sell_threshold=-0.15,
            technical_base_weight=1.0,
            onchain_base_weight=1.0,
        )
        fusion = OmniSignalFusion(cfg)
        tech = make_source_signal("technical", "BUY", score=0.5)
        onchain = make_source_signal("onchain", "SELL", score=-0.5)
        decision = fusion.fuse([tech, onchain])
        # aggregate = (0.5 - 0.5) / 2 = 0 → HOLD
        assert decision.final_action == "HOLD"

    def test_confidence_reflects_aggregate_magnitude(self):
        fusion = OmniSignalFusion()
        sig = make_source_signal("technical", score=0.8)
        decision = fusion.fuse([sig])
        assert decision.final_confidence > 0

    def test_dominant_source_is_highest_weight(self):
        """dominant_source 应是有效权重最大的 source。"""
        cfg = OmniSignalFusionConfig(
            technical_base_weight=2.0,  # 技术面权重最高
            onchain_base_weight=0.5,
        )
        fusion = OmniSignalFusion(cfg)
        tech = make_source_signal("technical", score=0.3)
        onchain = make_source_signal("onchain", score=0.3)
        decision = fusion.fuse([tech, onchain])
        assert decision.dominant_source == "technical"


# ─────────────────────────────────────────────────────────────
# MetaLearnerV2 测试
# ─────────────────────────────────────────────────────────────

class TestMetaLearnerV2:
    def _make_v2(self, **fusion_kwargs) -> MetaLearnerV2:
        fusion_cfg = OmniSignalFusionConfig(**fusion_kwargs) if fusion_kwargs else None
        from modules.alpha.ml.meta_learner import MetaLearnerConfig
        cfg = MetaLearnerV2Config()
        if fusion_cfg:
            cfg = MetaLearnerV2Config(
                meta_config=MetaLearnerConfig(),
                fusion_config=fusion_cfg,
            )
        return MetaLearnerV2(cfg)

    def test_technical_only_buy(self):
        """仅技术面 votes → MetaLearnerV2 输出与 Phase 1 MetaLearner 一致。"""
        v2 = self._make_v2(buy_threshold=0.15)
        votes = make_votes(n=3, action="BUY", prob=0.80)
        decision = v2.fuse(votes)
        assert decision.final_action == "BUY"

    def test_technical_only_sell(self):
        v2 = self._make_v2(sell_threshold=-0.15)
        votes = make_votes(n=2, action="SELL", prob=0.20)
        decision = v2.fuse(votes)
        assert decision.final_action == "SELL"

    def test_technical_hold_no_votes(self):
        """空 votes → HOLD。"""
        v2 = MetaLearnerV2()
        decision = v2.fuse([])
        assert decision.final_action == "HOLD"

    def test_external_onchain_reinforces_buy(self):
        """技术面 BUY + 链上 BUY → 更强的 BUY 信号。"""
        cfg = OmniSignalFusionConfig(
            buy_threshold=0.15,
            technical_base_weight=1.0,
            onchain_base_weight=0.5,
        )
        v2 = MetaLearnerV2(MetaLearnerV2Config(fusion_config=cfg))
        votes = make_votes(n=2, action="BUY", prob=0.75)
        onchain_sig = SourceSignal.from_score("onchain", score=0.5, freshness_ok=True)
        decision = v2.fuse(votes, external_signals=[onchain_sig])
        assert decision.final_action == "BUY"
        assert "onchain" in {s.source_name for s in decision.source_signals}

    def test_stale_external_signal_ignored(self):
        """stale 外部 source 不影响技术面主导的融合结果。"""
        v2 = self._make_v2(buy_threshold=0.15)
        votes = make_votes(n=2, action="BUY", prob=0.75)
        stale_sig = SourceSignal.from_score(
            "sentiment", score=-0.9, freshness_ok=False
        )
        decision = v2.fuse(votes, external_signals=[stale_sig])
        assert decision.final_action == "BUY"

    def test_high_risk_reduces_external_influence(self):
        """高风险状态下外部 source 被压制，技术面主导。"""
        cfg = OmniSignalFusionConfig(
            buy_threshold=0.15,
            sell_threshold=-0.15,
            technical_base_weight=1.0,
            onchain_base_weight=1.0,
            risk_penalty_threshold=0.05,
            risk_penalty_factor=0.0,
        )
        v2 = MetaLearnerV2(MetaLearnerV2Config(fusion_config=cfg))
        votes = make_votes(n=2, action="BUY", prob=0.78)
        bad_onchain = SourceSignal.from_score("onchain", score=-0.9, freshness_ok=True)
        risk = {"current_drawdown": 0.10, "kill_switch_active": False}
        decision = v2.fuse(votes, external_signals=[bad_onchain], risk_snapshot=risk)
        assert decision.final_action == "BUY"

    def test_fuse_from_meta_signal(self):
        """fuse_from_meta_signal 应跳过投票步骤，直接使用已有 MetaSignal。"""
        v2 = self._make_v2(buy_threshold=0.15)
        meta = make_meta_signal("BUY", 0.75)
        decision = v2.fuse_from_meta_signal(meta)
        assert decision.final_action == "BUY"

    def test_fuse_from_meta_signal_with_external(self):
        v2 = self._make_v2(buy_threshold=0.15)
        meta = make_meta_signal("BUY", 0.7)
        oc = SourceSignal.from_score("onchain", score=0.4)
        decision = v2.fuse_from_meta_signal(meta, external_signals=[oc])
        assert decision.final_action == "BUY"
        assert len(decision.source_signals) == 2

    def test_decision_is_actionable(self):
        v2 = self._make_v2(buy_threshold=0.15)
        votes = make_votes(n=2, prob=0.80)
        decision = v2.fuse(votes)
        assert decision.is_actionable() is True

    def test_diagnostics_structure(self):
        v2 = MetaLearnerV2()
        v2.fuse(make_votes(n=2))
        diag = v2.diagnostics()
        assert "n_calls" in diag
        assert "meta_learner" in diag
        assert "fusion" in diag
        assert diag["n_calls"] == 1

    def test_multiple_external_sources(self):
        """同时提供 onchain + sentiment 两个外部 source。"""
        cfg = OmniSignalFusionConfig(
            buy_threshold=0.15,
            technical_base_weight=1.0,
            onchain_base_weight=0.5,
            sentiment_base_weight=0.3,
        )
        v2 = MetaLearnerV2(MetaLearnerV2Config(fusion_config=cfg))
        votes = make_votes(n=2, action="BUY", prob=0.75)
        oc = SourceSignal.from_score("onchain", score=0.4)
        st = SourceSignal.from_score("sentiment", score=0.3)
        decision = v2.fuse(votes, external_signals=[oc, st])
        assert decision.final_action == "BUY"
        source_names = {s.source_name for s in decision.source_signals}
        assert source_names == {"technical", "onchain", "sentiment"}
