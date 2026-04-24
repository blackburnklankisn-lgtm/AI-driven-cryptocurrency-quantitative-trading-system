"""
tests/test_continuous_learner_extended.py — 补充覆盖 continuous_learner.py 缺失分支

Missed lines: 202, 221-225, 244-250, 256, 271, 276-309, 329, 348-349, 372-374,
              392, 439-456, 466-467, 474-484
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from modules.alpha.ml.continuous_learner import (
    ContinuousLearner,
    ContinuousLearnerConfig,
    ModelVersion,
)


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

def _make_signal_model():
    m = MagicMock()
    m.save = MagicMock()
    return m


def _make_model_version(
    acc: float = 0.60,
    f1: float = 0.58,
    is_active: bool = True,
    path: Optional[str] = None,
) -> ModelVersion:
    return ModelVersion(
        version_id="v_test",
        model=_make_signal_model(),
        trained_at=datetime.now(tz=timezone.utc),
        train_bars=500,
        oos_accuracy=acc,
        oos_f1=f1,
        is_active=is_active,
        model_path=path,
    )


def _make_learner(
    cfg: ContinuousLearnerConfig = None,
) -> tuple[ContinuousLearner, MagicMock, MagicMock, MagicMock]:
    trainer = MagicMock()
    feature_builder = MagicMock()
    labeler = MagicMock()
    config = cfg or ContinuousLearnerConfig(
        min_bars_for_retrain=50,
        retrain_every_n_bars=100,
        max_buffer_size=1000,
        drift_check_window=30,
        model_dir="./tmp_models_test",
    )
    learner = ContinuousLearner(trainer, feature_builder, labeler, config)
    return learner, trainer, feature_builder, labeler


def _make_wf_result(acc: float = 0.62, f1: float = 0.60):
    fold = MagicMock()
    fold.auc = 0.70
    result = MagicMock()
    result.fold_results = [fold]
    result.final_model = _make_signal_model()
    result.avg_metrics.return_value = {"accuracy": acc, "f1": f1}
    result.optimal_buy_threshold = 0.62
    result.optimal_sell_threshold = 0.38
    return result


# ══════════════════════════════════════════════════════════════
# Tests: basic interface
# ══════════════════════════════════════════════════════════════

class TestBasicInterface:

    def test_get_active_model_returns_none_initially(self):
        learner, *_ = _make_learner()
        assert learner.get_active_model() is None

    def test_get_active_model_returns_model_after_set(self):
        learner, *_ = _make_learner()
        v = _make_model_version()
        learner._active_version = v
        model = learner.get_active_model()
        assert model is v.model

    def test_get_optimal_thresholds_default(self):
        learner, *_ = _make_learner()
        buy, sell = learner.get_optimal_thresholds()
        assert 0.0 < buy < 1.0
        assert 0.0 < sell < 1.0

    def test_get_model_version_info_empty(self):
        learner, *_ = _make_learner()
        info = learner.get_model_version_info()
        assert isinstance(info, list)
        assert len(info) == 0

    def test_get_model_version_info_with_versions(self):
        learner, *_ = _make_learner()
        learner._versions.append(_make_model_version())
        info = learner.get_model_version_info()
        assert len(info) == 1
        assert "version_id" in info[0]
        assert "is_active" in info[0]

    def test_record_prediction_outcome_correct(self):
        learner, *_ = _make_learner()
        learner.record_prediction_outcome(1, 1)
        assert sum(learner._recent_correct) == 1

    def test_record_prediction_outcome_wrong(self):
        learner, *_ = _make_learner()
        learner.record_prediction_outcome(1, 0)
        assert sum(learner._recent_correct) == 0


# ══════════════════════════════════════════════════════════════
# Tests: on_new_bar — buffer accumulation, no trigger yet
# ══════════════════════════════════════════════════════════════

class TestOnNewBar:

    def test_returns_none_when_buffer_too_small(self):
        learner, *_ = _make_learner()
        row = {"timestamp": "2024-01-01", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
        result = learner.on_new_bar(row)
        assert result is None

    def test_bar_count_increments(self):
        learner, *_ = _make_learner()
        row = {"timestamp": "2024-01-01", "close": 1, "open": 1, "high": 1, "low": 1, "volume": 1}
        learner.on_new_bar(row)
        assert learner._bar_count == 1


# ══════════════════════════════════════════════════════════════
# Tests: _check_retrain_triggers
# ══════════════════════════════════════════════════════════════

class TestCheckRetrainTriggers:

    def test_scheduled_trigger_fires_when_bars_elapsed(self):
        learner, *_ = _make_learner(
            ContinuousLearnerConfig(
                retrain_every_n_bars=10,
                min_bars_for_retrain=50,
            )
        )
        learner._bars_since_retrain = 10
        trigger = learner._check_retrain_triggers()
        assert trigger == "scheduled"

    def test_performance_degradation_trigger(self):
        learner, *_ = _make_learner(
            ContinuousLearnerConfig(
                retrain_every_n_bars=1000,
                min_accuracy_threshold=0.70,
            )
        )
        # Fill recent_correct with all wrong (accuracy = 0)
        for _ in range(35):
            learner._recent_correct.append(0)
        trigger = learner._check_retrain_triggers()
        assert trigger == "performance_degradation"

    def test_no_trigger_when_performance_ok(self):
        learner, *_ = _make_learner(
            ContinuousLearnerConfig(
                retrain_every_n_bars=1000,
                min_accuracy_threshold=0.50,
            )
        )
        for _ in range(35):
            learner._recent_correct.append(1)  # high accuracy
        trigger = learner._check_retrain_triggers()
        assert trigger is None

    def test_no_trigger_with_insufficient_observations(self):
        learner, *_ = _make_learner(
            ContinuousLearnerConfig(
                retrain_every_n_bars=1000,
                min_accuracy_threshold=0.90,
            )
        )
        # Only 20 observations (< 30 threshold)
        for _ in range(20):
            learner._recent_correct.append(0)
        trigger = learner._check_retrain_triggers()
        assert trigger is None


# ══════════════════════════════════════════════════════════════
# Tests: _should_switch_model
# ══════════════════════════════════════════════════════════════

class TestShouldSwitchModel:

    def test_switch_when_no_active_version(self):
        learner, *_ = _make_learner()
        learner._active_version = None
        new_v = _make_model_version(acc=0.60)
        assert learner._should_switch_model(new_v) is True

    def test_switch_when_accuracy_improved_by_1pct(self):
        learner, *_ = _make_learner()
        learner._active_version = _make_model_version(acc=0.60, f1=0.58)
        new_v = _make_model_version(acc=0.62, f1=0.58)  # +2%
        assert learner._should_switch_model(new_v) is True

    def test_switch_when_f1_improved_by_2pct(self):
        learner, *_ = _make_learner()
        learner._active_version = _make_model_version(acc=0.60, f1=0.58)
        new_v = _make_model_version(acc=0.60, f1=0.61)  # f1 +3%
        assert learner._should_switch_model(new_v) is True

    def test_no_switch_when_improvement_insufficient(self):
        learner, *_ = _make_learner()
        learner._active_version = _make_model_version(acc=0.60, f1=0.58)
        new_v = _make_model_version(acc=0.605, f1=0.585)  # tiny improvement
        assert learner._should_switch_model(new_v) is False


# ══════════════════════════════════════════════════════════════
# Tests: _cleanup_old_versions
# ══════════════════════════════════════════════════════════════

class TestCleanupOldVersions:

    def test_no_cleanup_when_within_limit(self):
        cfg = ContinuousLearnerConfig(max_saved_versions=3)
        learner, *_ = _make_learner(cfg)
        learner._versions = [_make_model_version() for _ in range(3)]
        learner._cleanup_old_versions()
        assert len(learner._versions) == 3

    def test_cleanup_removes_oldest_versions(self):
        cfg = ContinuousLearnerConfig(max_saved_versions=2)
        learner, *_ = _make_learner(cfg)
        learner._versions = [_make_model_version(is_active=False) for _ in range(4)]
        learner._cleanup_old_versions()
        assert len(learner._versions) == 2

    def test_cleanup_deletes_file_if_path_exists(self, tmp_path):
        cfg = ContinuousLearnerConfig(max_saved_versions=1)
        learner, *_ = _make_learner(cfg)
        f = tmp_path / "model_old.pkl"
        f.write_text("dummy")
        old_v = _make_model_version(is_active=False, path=str(f))
        active_v = _make_model_version(is_active=True)
        learner._versions = [old_v, active_v]
        learner._cleanup_old_versions()
        assert not f.exists()


# ══════════════════════════════════════════════════════════════
# Tests: force_retrain
# ══════════════════════════════════════════════════════════════

class TestForceRetrain:

    def test_force_retrain_calls_retrain(self):
        learner, trainer, *_ = _make_learner()
        wf_result = _make_wf_result()
        trainer.train.return_value = wf_result
        # Fill buffer
        row = {"timestamp": "2024-01-01", "close": 1, "open": 1, "high": 1, "low": 1, "volume": 1}
        for _ in range(60):
            learner._ohlcv_buffer.append(row)
        result = learner.force_retrain()
        # Should return a ModelVersion or None (depending on retrain result)
        # The trainer is called
        assert trainer.train.called

    def test_force_retrain_returns_none_when_retraining_flag_set(self):
        learner, trainer, *_ = _make_learner()
        learner._is_retraining = True
        result = learner.force_retrain()
        # _retrain returns None because _is_retraining=True
        assert result is None


# ══════════════════════════════════════════════════════════════
# Tests: _update_reference_features
# ══════════════════════════════════════════════════════════════

class TestUpdateReferenceFeatures:

    def test_update_reference_features_success(self):
        learner, trainer, feature_builder, labeler = _make_learner()
        n = 100
        feat_df = pd.DataFrame({
            "f1": np.random.randn(n),
            "f2": np.random.randn(n),
        })
        feature_builder.build.return_value = feat_df
        feature_builder.get_feature_names.return_value = ["f1", "f2"]
        df = pd.DataFrame({"close": np.ones(n)})
        learner._update_reference_features(df)
        assert learner._reference_features is not None

    def test_update_reference_features_exception_handled(self):
        learner, trainer, feature_builder, labeler = _make_learner()
        feature_builder.build.side_effect = RuntimeError("build fail")
        df = pd.DataFrame({"close": [1, 2, 3]})
        learner._update_reference_features(df)  # Should not raise
        assert learner._reference_features is None
