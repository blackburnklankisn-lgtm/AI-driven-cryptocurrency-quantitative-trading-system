"""
tests/test_meta_learner.py — W8 多模型集成单元测试

覆盖项：
- ModelEnsemble: add_model / predict / NaN 容忍 / 全错降级
- MetaLearner: weighted_avg / majority_vote / confidence / 0 票 / 单票
- 端到端：Ensemble → MetaLearner 链路
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from modules.alpha.contracts.ensemble_types import MetaSignal, ModelVote
from modules.alpha.ml.ensemble import EnsembleConfig, ModelEnsemble
from modules.alpha.ml.meta_learner import MetaLearner, MetaLearnerConfig


# ─────────────────────────────────────────────────────────────
# 测试用 Fake 模型
# ─────────────────────────────────────────────────────────────

class FakeModel:
    """固定输出概率的假模型（predict_proba 接口）。"""

    def __init__(self, buy_prob: float):
        self._prob = buy_prob

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        n = len(X)
        return np.column_stack([
            np.full(n, 1.0 - self._prob),
            np.full(n, self._prob),
        ])


class NaNModel:
    """始终输出 NaN 的模型（测试容错）。"""

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        return np.full((len(X), 2), np.nan)


class ErrorModel:
    """predict_proba 抛异常的模型（测试容错）。"""

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        raise RuntimeError("模拟模型崩溃")


def make_X(n: int = 1) -> pd.DataFrame:
    return pd.DataFrame({"f1": np.ones(n), "f2": np.zeros(n)})


# ─────────────────────────────────────────────────────────────
# ModelEnsemble 测试
# ─────────────────────────────────────────────────────────────

class TestModelEnsemble:
    def test_add_model_chainable(self):
        ens = (
            ModelEnsemble()
            .add_model("lgbm", FakeModel(0.7))
            .add_model("rf", FakeModel(0.5))
        )
        assert set(ens.model_names()) == {"lgbm", "rf"}

    def test_predict_returns_model_votes(self):
        ens = ModelEnsemble().add_model("lgbm", FakeModel(0.75))
        votes = ens.predict(make_X())
        assert len(votes) == 1
        assert votes[0].model_name == "lgbm"
        assert abs(votes[0].buy_probability - 0.75) < 1e-6

    def test_buy_action_above_threshold(self):
        config = EnsembleConfig(buy_threshold=0.6, sell_threshold=0.4)
        ens = ModelEnsemble(config).add_model("m", FakeModel(0.8))
        votes = ens.predict(make_X())
        assert votes[0].action == "BUY"

    def test_sell_action_below_threshold(self):
        config = EnsembleConfig(buy_threshold=0.6, sell_threshold=0.4)
        ens = ModelEnsemble(config).add_model("m", FakeModel(0.2))
        votes = ens.predict(make_X())
        assert votes[0].action == "SELL"

    def test_hold_action_in_middle(self):
        config = EnsembleConfig(buy_threshold=0.6, sell_threshold=0.4)
        ens = ModelEnsemble(config).add_model("m", FakeModel(0.5))
        votes = ens.predict(make_X())
        assert votes[0].action == "HOLD"

    def test_nan_model_is_skipped(self):
        """NaN 输出的模型应该被跳过，不加入 votes。"""
        ens = (
            ModelEnsemble()
            .add_model("good", FakeModel(0.7))
            .add_model("nan_m", NaNModel())
        )
        votes = ens.predict(make_X())
        assert len(votes) == 1
        assert votes[0].model_name == "good"

    def test_error_model_is_skipped(self):
        """抛异常的模型应该被跳过。"""
        ens = (
            ModelEnsemble()
            .add_model("ok", FakeModel(0.6))
            .add_model("err", ErrorModel())
        )
        votes = ens.predict(make_X())
        assert len(votes) == 1

    def test_all_error_returns_empty_list(self):
        """全部模型失败时返回空列表。"""
        ens = ModelEnsemble().add_model("err", ErrorModel())
        votes = ens.predict(make_X())
        assert votes == []

    def test_empty_ensemble_returns_empty(self):
        """无任何注册模型时返回空列表。"""
        ens = ModelEnsemble()
        assert ens.predict(make_X()) == []

    def test_invalid_weight_raises(self):
        with pytest.raises(ValueError, match="weight"):
            ModelEnsemble().add_model("bad", FakeModel(0.5), weight=-1.0)

    def test_invalid_threshold_config_raises(self):
        with pytest.raises(ValueError):
            EnsembleConfig(buy_threshold=0.3, sell_threshold=0.7)

    def test_weight_is_passed_to_vote(self):
        ens = ModelEnsemble().add_model("m", FakeModel(0.65), weight=3.0)
        votes = ens.predict(make_X())
        assert votes[0].weight == 3.0

    def test_diagnostics_structure(self):
        ens = ModelEnsemble().add_model("m", FakeModel(0.5))
        ens.predict(make_X())
        diag = ens.diagnostics()
        assert "n_models" in diag
        assert "models" in diag
        assert diag["n_models"] == 1
        assert diag["models"]["m"]["n_calls"] == 1

    def test_overwrite_existing_model(self):
        """同名模型重复注册时后者覆盖前者。"""
        ens = (
            ModelEnsemble()
            .add_model("m", FakeModel(0.3))
            .add_model("m", FakeModel(0.9))  # 覆盖
        )
        votes = ens.predict(make_X())
        assert votes[0].buy_probability > 0.5  # 新模型

    def test_predict_multi_row(self):
        """多行输入时应该取最后一行（推理场景）。"""
        ens = ModelEnsemble().add_model("m", FakeModel(0.8))
        votes = ens.predict(make_X(n=5))
        assert abs(votes[0].buy_probability - 0.8) < 1e-6


# ─────────────────────────────────────────────────────────────
# MetaLearner 测试
# ─────────────────────────────────────────────────────────────

def make_vote(model_name: str, buy_prob: float, weight: float = 1.0) -> ModelVote:
    if buy_prob >= 0.6:
        action = "BUY"
    elif buy_prob <= 0.4:
        action = "SELL"
    else:
        action = "HOLD"
    return ModelVote(
        model_name=model_name,
        buy_probability=buy_prob,
        action=action,
        weight=weight,
    )


class TestMetaLearnerWeightedAvg:
    def test_buy_when_avg_above_threshold(self):
        learner = MetaLearner(MetaLearnerConfig(fusion_strategy="weighted_avg"))
        votes = [make_vote("a", 0.8), make_vote("b", 0.7)]
        sig = learner.fuse(votes)
        assert sig.final_action == "BUY"

    def test_sell_when_avg_below_threshold(self):
        learner = MetaLearner(MetaLearnerConfig(fusion_strategy="weighted_avg"))
        votes = [make_vote("a", 0.2), make_vote("b", 0.3)]
        sig = learner.fuse(votes)
        assert sig.final_action == "SELL"

    def test_hold_when_avg_in_middle(self):
        learner = MetaLearner(MetaLearnerConfig(fusion_strategy="weighted_avg"))
        votes = [make_vote("a", 0.5), make_vote("b", 0.5)]
        sig = learner.fuse(votes)
        assert sig.final_action == "HOLD"

    def test_weights_affect_result(self):
        """高权重模型应该拉动平均概率。"""
        learner = MetaLearner(MetaLearnerConfig(fusion_strategy="weighted_avg"))
        votes = [
            make_vote("strong", 0.85, weight=5.0),  # 强 BUY
            make_vote("weak",   0.2,  weight=1.0),  # SELL
        ]
        sig = learner.fuse(votes)
        # 加权平均 = (0.85*5 + 0.2*1) / 6 ≈ 0.742 > 0.6 → BUY
        assert sig.final_action == "BUY"

    def test_dominant_model_is_set(self):
        learner = MetaLearner(MetaLearnerConfig(fusion_strategy="weighted_avg"))
        votes = [make_vote("a", 0.9), make_vote("b", 0.55)]
        sig = learner.fuse(votes)
        assert sig.dominant_model != ""

    def test_debug_payload_contains_avg_prob(self):
        learner = MetaLearner(MetaLearnerConfig(fusion_strategy="weighted_avg"))
        votes = [make_vote("a", 0.7), make_vote("b", 0.8)]
        sig = learner.fuse(votes)
        assert "avg_buy_probability" in sig.debug_payload

    def test_confidence_in_0_to_1(self):
        learner = MetaLearner(MetaLearnerConfig(fusion_strategy="weighted_avg"))
        votes = [make_vote("a", 0.85)]
        sig = learner.fuse(votes)
        assert 0.0 <= sig.final_confidence <= 1.0


class TestMetaLearnerMajorityVote:
    def test_buy_wins_majority(self):
        learner = MetaLearner(MetaLearnerConfig(fusion_strategy="majority_vote"))
        votes = [make_vote("a", 0.8), make_vote("b", 0.75), make_vote("c", 0.25)]
        sig = learner.fuse(votes)
        assert sig.final_action == "BUY"

    def test_tie_broken_by_weight(self):
        """BUY 与 SELL 各一票时，权重更高者胜出。"""
        learner = MetaLearner(MetaLearnerConfig(fusion_strategy="majority_vote"))
        votes = [
            make_vote("a", 0.8, weight=3.0),   # BUY
            make_vote("b", 0.2, weight=1.0),   # SELL
        ]
        sig = learner.fuse(votes)
        assert sig.final_action == "BUY"

    def test_confidence_reflects_majority_ratio(self):
        learner = MetaLearner(MetaLearnerConfig(fusion_strategy="majority_vote"))
        # 全部 BUY → confidence 应接近 1.0
        votes = [make_vote(f"m{i}", 0.8) for i in range(4)]
        sig = learner.fuse(votes)
        assert sig.final_confidence > 0.9

    def test_debug_payload_has_tally(self):
        learner = MetaLearner(MetaLearnerConfig(fusion_strategy="majority_vote"))
        sig = learner.fuse([make_vote("a", 0.7), make_vote("b", 0.75)])
        assert "tally" in sig.debug_payload


class TestMetaLearnerConfidence:
    def test_most_confident_model_wins(self):
        learner = MetaLearner(MetaLearnerConfig(fusion_strategy="confidence"))
        votes = [
            make_vote("timid", 0.55),   # 接近 0.5，置信度低
            make_vote("bold",  0.92),   # 远离 0.5，置信度高
        ]
        sig = learner.fuse(votes)
        assert sig.dominant_model == "bold"
        assert sig.final_action == "BUY"

    def test_sell_side_confidence(self):
        learner = MetaLearner(MetaLearnerConfig(fusion_strategy="confidence"))
        votes = [
            make_vote("a", 0.52),
            make_vote("b", 0.05),  # 强 SELL 置信度
        ]
        sig = learner.fuse(votes)
        assert sig.dominant_model == "b"
        assert sig.final_action == "SELL"


class TestMetaLearnerEdgeCases:
    def test_empty_votes_returns_hold(self):
        learner = MetaLearner()
        sig = learner.fuse([])
        assert sig.final_action == "HOLD"
        assert sig.final_confidence == 0.0
        assert sig.dominant_model == "none"

    def test_single_vote_passthrough(self):
        learner = MetaLearner()
        sig = learner.fuse([make_vote("only", 0.85)])
        assert sig.final_action == "BUY"
        assert sig.dominant_model == "only"

    def test_invalid_fusion_strategy_raises(self):
        with pytest.raises(ValueError, match="fusion_strategy"):
            MetaLearnerConfig(fusion_strategy="invalid")  # type: ignore

    def test_invalid_threshold_raises(self):
        with pytest.raises(ValueError):
            MetaLearnerConfig(buy_threshold=0.3, sell_threshold=0.7)

    def test_diagnostics_count(self):
        learner = MetaLearner()
        learner.fuse([make_vote("a", 0.7)])
        learner.fuse([make_vote("b", 0.3)])
        diag = learner.diagnostics()
        assert diag["n_fuse_calls"] == 2

    def test_model_votes_preserved_in_signal(self):
        """MetaSignal.model_votes 应包含所有输入票。"""
        learner = MetaLearner()
        votes = [make_vote("a", 0.8), make_vote("b", 0.65)]
        sig = learner.fuse(votes)
        assert len(sig.model_votes) == 2


# ─────────────────────────────────────────────────────────────
# 端到端：Ensemble → MetaLearner
# ─────────────────────────────────────────────────────────────

class TestEnsembleToMetaLearnerPipeline:
    def test_full_pipeline_buy(self):
        """三个强 BUY 模型 → MetaLearner 输出 BUY。"""
        config = EnsembleConfig(buy_threshold=0.6, sell_threshold=0.4)
        ens = (
            ModelEnsemble(config)
            .add_model("lgbm", FakeModel(0.82), weight=2.0)
            .add_model("rf",   FakeModel(0.78), weight=1.0)
            .add_model("lr",   FakeModel(0.71), weight=0.5)
        )
        votes = ens.predict(make_X())
        assert len(votes) == 3

        learner = MetaLearner()
        sig = learner.fuse(votes)
        assert sig.final_action == "BUY"

    def test_full_pipeline_with_one_failed_model(self):
        """一个模型崩溃时，其余模型仍能正常投票。"""
        config = EnsembleConfig(buy_threshold=0.6, sell_threshold=0.4)
        ens = (
            ModelEnsemble(config)
            .add_model("ok1", FakeModel(0.8))
            .add_model("ok2", FakeModel(0.75))
            .add_model("bad", ErrorModel())
        )
        votes = ens.predict(make_X())
        assert len(votes) == 2  # 崩溃的被跳过

        learner = MetaLearner()
        sig = learner.fuse(votes)
        assert sig.final_action == "BUY"

    def test_full_pipeline_degraded_single_model(self):
        """只有一个模型注册且正常运行时，MetaLearner 可降级执行。"""
        ens = ModelEnsemble().add_model("solo", FakeModel(0.2))
        votes = ens.predict(make_X())

        learner = MetaLearner()
        sig = learner.fuse(votes)
        assert sig.final_action == "SELL"
        assert sig.dominant_model == "solo"

    def test_full_pipeline_all_models_fail(self):
        """全部模型崩溃时，MetaLearner 应降级返回 HOLD。"""
        ens = ModelEnsemble().add_model("err", ErrorModel())
        votes = ens.predict(make_X())
        assert votes == []

        learner = MetaLearner()
        sig = learner.fuse(votes)
        assert sig.final_action == "HOLD"

    def test_majority_vote_pipeline(self):
        """majority_vote 策略端到端验证。"""
        config = EnsembleConfig(buy_threshold=0.6, sell_threshold=0.4)
        ens = (
            ModelEnsemble(config)
            .add_model("a", FakeModel(0.75))  # BUY
            .add_model("b", FakeModel(0.70))  # BUY
            .add_model("c", FakeModel(0.25))  # SELL
        )
        votes = ens.predict(make_X())

        learner = MetaLearner(MetaLearnerConfig(fusion_strategy="majority_vote"))
        sig = learner.fuse(votes)
        assert sig.final_action == "BUY"

    def test_diagnostics_after_pipeline(self):
        ens = ModelEnsemble().add_model("m", FakeModel(0.5))
        for _ in range(3):
            votes = ens.predict(make_X())
        diag = ens.diagnostics()
        assert diag["models"]["m"]["n_calls"] == 3
