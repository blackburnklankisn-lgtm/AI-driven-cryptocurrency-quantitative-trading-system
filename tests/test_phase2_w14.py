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


# ─────────────────────────────────────────────────────────────
# Phase 3 扩展测试：microstructure / rl source + Phase3Config
# ─────────────────────────────────────────────────────────────

class TestOmniSignalFusionPhase3:
    """验证 OmniSignalFusion 对 microstructure / rl source 的支持。"""

    def test_microstructure_base_weight_default(self):
        cfg = OmniSignalFusionConfig()
        assert cfg.microstructure_base_weight == pytest.approx(0.6)

    def test_rl_base_weight_default(self):
        cfg = OmniSignalFusionConfig()
        assert cfg.rl_base_weight == pytest.approx(0.4)

    def test_base_weight_microstructure(self):
        cfg = OmniSignalFusionConfig(microstructure_base_weight=0.7)
        assert cfg.base_weight("microstructure") == pytest.approx(0.7)

    def test_base_weight_rl(self):
        cfg = OmniSignalFusionConfig(rl_base_weight=0.5)
        assert cfg.base_weight("rl") == pytest.approx(0.5)

    def test_microstructure_signal_participates_in_fusion(self):
        """microstructure source 参与融合，且影响 aggregate_score。"""
        cfg = OmniSignalFusionConfig(
            buy_threshold=0.10,
            technical_base_weight=1.0,
            microstructure_base_weight=0.6,
        )
        fusion = OmniSignalFusion(cfg)
        tech = make_source_signal("technical", "BUY", score=0.15)   # 勉强过 threshold
        micro = make_source_signal("microstructure", "BUY", score=0.5)
        decision = fusion.fuse([tech, micro])
        assert decision.final_action == "BUY"
        source_names = {s.source_name for s in decision.source_signals}
        assert "microstructure" in source_names

    def test_rl_signal_participates_in_fusion(self):
        """rl source 参与融合，且影响 aggregate_score。"""
        cfg = OmniSignalFusionConfig(
            buy_threshold=0.10,
            technical_base_weight=1.0,
            rl_base_weight=0.4,
        )
        fusion = OmniSignalFusion(cfg)
        tech = make_source_signal("technical", "BUY", score=0.15)
        rl = make_source_signal("rl", "BUY", score=0.6)
        decision = fusion.fuse([tech, rl])
        assert decision.final_action == "BUY"
        source_names = {s.source_name for s in decision.source_signals}
        assert "rl" in source_names

    def test_stale_microstructure_zeroed_out(self):
        """stale microstructure 不影响结果，融合仍由 technical 主导。"""
        fusion = OmniSignalFusion()
        tech = make_source_signal("technical", "BUY", score=0.6)
        micro_stale = make_source_signal("microstructure", "SELL", score=-0.9,
                                          freshness_ok=False)
        decision = fusion.fuse([tech, micro_stale])
        assert decision.final_action == "BUY"

    def test_stale_rl_zeroed_out(self):
        """stale rl source 不影响结果。"""
        fusion = OmniSignalFusion()
        tech = make_source_signal("technical", "SELL", score=-0.6)
        rl_stale = make_source_signal("rl", "BUY", score=0.9, freshness_ok=False)
        decision = fusion.fuse([tech, rl_stale])
        assert decision.final_action == "SELL"

    def test_high_risk_reduces_microstructure_weight(self):
        """高风险状态下 microstructure source 被压制（与 onchain/sentiment 同规则）。"""
        cfg = OmniSignalFusionConfig(
            buy_threshold=0.15,
            sell_threshold=-0.15,
            technical_base_weight=1.0,
            microstructure_base_weight=1.0,
            risk_penalty_factor=0.0,  # 完全压制非 technical source
        )
        fusion = OmniSignalFusion(cfg)
        tech = make_source_signal("technical", "BUY", score=0.5)
        micro = make_source_signal("microstructure", "SELL", score=-0.9)
        risk = {"current_drawdown": 0.10, "kill_switch_active": False}
        decision = fusion.fuse([tech, micro], risk_snapshot=risk)
        assert decision.final_action == "BUY"

    def test_five_source_fusion(self):
        """所有五个 source 同时参与融合无崩溃。"""
        cfg = OmniSignalFusionConfig(
            buy_threshold=0.12,
            technical_base_weight=1.0,
            onchain_base_weight=0.5,
            sentiment_base_weight=0.5,
            microstructure_base_weight=0.6,
            rl_base_weight=0.4,
        )
        fusion = OmniSignalFusion(cfg)
        signals = [
            make_source_signal("technical", "BUY", score=0.4),
            make_source_signal("onchain", "BUY", score=0.3),
            make_source_signal("sentiment", "BUY", score=0.2),
            make_source_signal("microstructure", "BUY", score=0.5),
            make_source_signal("rl", "BUY", score=0.4),
        ]
        decision = fusion.fuse(signals)
        assert decision.final_action == "BUY"
        assert len(decision.source_signals) == 5

    def test_config_negative_microstructure_weight_raises(self):
        with pytest.raises(ValueError):
            OmniSignalFusionConfig(microstructure_base_weight=-0.1)

    def test_config_negative_rl_weight_raises(self):
        with pytest.raises(ValueError):
            OmniSignalFusionConfig(rl_base_weight=-0.1)

    def test_diagnostics_after_phase3_fusion(self):
        fusion = OmniSignalFusion()
        fusion.fuse([
            make_source_signal("technical", score=0.4),
            make_source_signal("microstructure", score=0.3),
            make_source_signal("rl", score=0.2),
        ])
        diag = fusion.diagnostics()
        assert diag["n_fuse_calls"] == 1


class TestMetaLearnerV2Phase3:
    """验证 MetaLearnerV2 的 Phase 3 新接口 fuse_with_phase3_sources。"""

    def _make_v2(self) -> MetaLearnerV2:
        cfg = OmniSignalFusionConfig(
            buy_threshold=0.12,
            technical_base_weight=1.0,
            microstructure_base_weight=0.6,
            rl_base_weight=0.4,
        )
        return MetaLearnerV2(MetaLearnerV2Config(fusion_config=cfg))

    def test_fuse_with_microstructure_only(self):
        v2 = self._make_v2()
        votes = make_votes(n=2, action="BUY", prob=0.75)
        micro = SourceSignal.from_score("microstructure", score=0.5, freshness_ok=True)
        decision = v2.fuse_with_phase3_sources(votes, microstructure_signal=micro)
        assert decision.final_action == "BUY"
        assert "microstructure" in {s.source_name for s in decision.source_signals}

    def test_fuse_with_rl_only(self):
        v2 = self._make_v2()
        votes = make_votes(n=2, action="BUY", prob=0.75)
        rl = SourceSignal.from_score("rl", score=0.4, freshness_ok=True)
        decision = v2.fuse_with_phase3_sources(votes, rl_signal=rl)
        assert decision.final_action == "BUY"
        assert "rl" in {s.source_name for s in decision.source_signals}

    def test_fuse_with_all_phase3_sources(self):
        """microstructure + rl + other_signals 同时传入。"""
        cfg = OmniSignalFusionConfig(
            buy_threshold=0.12,
            technical_base_weight=1.0,
            onchain_base_weight=0.5,
            microstructure_base_weight=0.6,
            rl_base_weight=0.4,
        )
        v2 = MetaLearnerV2(MetaLearnerV2Config(fusion_config=cfg))
        votes = make_votes(n=2, action="BUY", prob=0.75)
        micro = SourceSignal.from_score("microstructure", score=0.5)
        rl = SourceSignal.from_score("rl", score=0.3)
        oc = SourceSignal.from_score("onchain", score=0.3)
        decision = v2.fuse_with_phase3_sources(
            votes,
            microstructure_signal=micro,
            rl_signal=rl,
            other_signals=[oc],
        )
        assert decision.final_action == "BUY"
        source_names = {s.source_name for s in decision.source_signals}
        assert "microstructure" in source_names
        assert "rl" in source_names
        assert "onchain" in source_names

    def test_fuse_with_no_phase3_sources_backward_compat(self):
        """不传任何 Phase 3 source 时行为与 fuse() 一致。"""
        v2 = self._make_v2()
        votes = make_votes(n=2, action="BUY", prob=0.75)
        decision = v2.fuse_with_phase3_sources(votes)
        # 只有 technical source 参与
        assert decision.final_action == "BUY"
        assert "technical" in {s.source_name for s in decision.source_signals}

    def test_stale_phase3_sources_ignored(self):
        """stale microstructure 和 rl 不干扰融合结果。"""
        v2 = self._make_v2()
        votes = make_votes(n=2, action="BUY", prob=0.75)
        micro_stale = SourceSignal.from_score("microstructure", score=-0.9,
                                               freshness_ok=False)
        rl_stale = SourceSignal.from_score("rl", score=-0.9, freshness_ok=False)
        decision = v2.fuse_with_phase3_sources(
            votes,
            microstructure_signal=micro_stale,
            rl_signal=rl_stale,
        )
        assert decision.final_action == "BUY"

    def test_diagnostics_after_phase3_fuse(self):
        v2 = self._make_v2()
        votes = make_votes(n=2)
        micro = SourceSignal.from_score("microstructure", score=0.3)
        v2.fuse_with_phase3_sources(votes, microstructure_signal=micro)
        diag = v2.diagnostics()
        assert diag["n_calls"] == 1


class TestPhase3Config:
    """验证 Phase3Config 从 YAML 加载正确。"""

    def test_load_config_has_phase3(self):
        from core.config import load_config
        cfg = load_config()
        assert hasattr(cfg, "phase3")

    def test_phase3_defaults(self):
        from core.config import Phase3Config
        p3 = Phase3Config()
        assert p3.enabled is True
        assert p3.realtime_feed_enabled is False
        assert p3.market_making_enabled is False
        assert p3.rl_agent_enabled is False
        assert p3.self_evolution_enabled is False

    def test_phase3_subconfig_realtime_feed(self):
        from core.config import Phase3RealtimeFeedConfig
        rtf = Phase3RealtimeFeedConfig()
        assert rtf.reconnect_backoff_sec > 0
        assert rtf.orderbook_depth_levels > 0
        assert rtf.snapshot_recovery_enabled is True

    def test_phase3_subconfig_market_making(self):
        from core.config import Phase3MarketMakingConfig
        mm = Phase3MarketMakingConfig()
        assert 0 < mm.risk_aversion_gamma < 1
        assert 0 < mm.max_inventory_pct < 1
        assert mm.maker_only is True

    def test_phase3_subconfig_rl(self):
        from core.config import Phase3RLConfig
        rl = Phase3RLConfig()
        assert rl.training_enabled is False
        assert rl.policy_mode == "shadow"
        assert 0 < rl.action_confidence_floor < 1

    def test_phase3_subconfig_evolution(self):
        from core.config import Phase3EvolutionConfig
        ev = Phase3EvolutionConfig()
        assert ev.shadow_days >= 1
        assert ev.paper_days >= 1
        assert ev.promote_min_sharpe > 0
        assert ev.auto_rollback_enabled is True

    def test_phase3_subconfig_logging(self):
        from core.config import Phase3LoggingConfig
        lg = Phase3LoggingConfig()
        assert lg.trace_sample_rate == pytest.approx(1.0)

    def test_yaml_phase3_values_loaded(self):
        """验证 YAML 中的 phase3 块值被正确读取。"""
        from core.config import load_config
        cfg = load_config()
        # YAML 设置的值
        assert cfg.phase3.market_making.risk_aversion_gamma == pytest.approx(0.12)
        assert cfg.phase3.rl.policy_mode == "shadow"
        assert cfg.phase3.evolution.promote_min_sharpe == pytest.approx(0.8)
        assert cfg.phase3.realtime_feed.orderbook_depth_levels == 20

