"""
tests/test_threshold_calibrator.py — W7 阈值校准 + 模型注册表 + 诊断 单元测试

覆盖项：
- ThresholdCalibrator: Youden's J 计算、多折聚合（mean/median/conservative）
- CalibrationResult: save/load 往返、阈值边界
- ModelRegistry: register/promote/rollback/load_active/diagnostics
- MLDiagnostics: report_walk_forward/report_calibration/save_report
"""

from __future__ import annotations

import pickle
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from modules.alpha.ml.diagnostics import MLDiagnostics
from modules.alpha.ml.model_registry import ModelRegistry
from modules.alpha.ml.threshold_calibrator import CalibrationResult, ThresholdCalibrator


# ─────────────────────────────────────────────────────────────
# 测试数据生成
# ─────────────────────────────────────────────────────────────

def make_proba_actual(n: int = 200, seed: int = 0) -> tuple[pd.Series, pd.Series]:
    """生成模拟的 OOS 买入概率和真实二分类标签。"""
    rng = np.random.RandomState(seed)
    # 真实标签
    actual = pd.Series(rng.randint(0, 2, n).astype(float))
    # 概率：正样本偏高，负样本偏低，有一定噪声
    proba = pd.Series(
        np.where(actual == 1,
                 rng.beta(5, 3, n),   # 正样本：偏高概率
                 rng.beta(3, 5, n))   # 负样本：偏低概率
    )
    return proba, actual


def make_fold_data(n_folds: int = 3, n_each: int = 150) -> tuple[list, list]:
    """生成多折 OOS 数据。"""
    probas, actuals = [], []
    for i in range(n_folds):
        p, a = make_proba_actual(n_each, seed=i * 7)
        probas.append(p)
        actuals.append(a)
    return probas, actuals


# ─────────────────────────────────────────────────────────────
# ThresholdCalibrator 测试
# ─────────────────────────────────────────────────────────────

class TestThresholdCalibrator:
    def test_single_fold_calibration(self):
        """单折校准应该返回有效阈值。"""
        proba, actual = make_proba_actual(200)
        calibrator = ThresholdCalibrator()
        result = calibrator.calibrate_from_fold_results([proba], [actual])

        assert 0.4 <= result.recommended_buy_threshold <= 0.85
        assert 0.15 <= result.recommended_sell_threshold <= 0.60

    def test_multi_fold_calibration(self):
        """多折校准应该汇总所有折的最优阈值。"""
        probas, actuals = make_fold_data(n_folds=3)
        calibrator = ThresholdCalibrator()
        result = calibrator.calibrate_from_fold_results(probas, actuals)

        assert len(result.fold_thresholds) == 3
        assert 0.0 <= result.avg_auc <= 1.0
        assert 0.0 <= result.avg_j_statistic <= 1.0

    def test_aggregation_strategy_mean(self):
        """mean 策略下，recommended_buy 应该等于各折均值。"""
        probas, actuals = make_fold_data(n_folds=3)
        calibrator = ThresholdCalibrator(aggregation_strategy="mean")
        result = calibrator.calibrate_from_fold_results(probas, actuals)

        expected = result.buy_threshold_mean
        assert abs(result.recommended_buy_threshold - expected) < 1e-6

    def test_aggregation_strategy_median(self):
        """median 策略下，recommended_buy 应该等于各折中位数。"""
        probas, actuals = make_fold_data(n_folds=4)
        calibrator = ThresholdCalibrator(aggregation_strategy="median")
        result = calibrator.calibrate_from_fold_results(probas, actuals)

        assert abs(result.recommended_buy_threshold - result.buy_threshold_median) < 1e-6

    def test_conservative_higher_than_mean(self):
        """conservative 策略应该比 mean 更严格（阈值更高）。"""
        probas, actuals = make_fold_data(n_folds=5)
        cal_mean = ThresholdCalibrator("mean").calibrate_from_fold_results(probas, actuals)
        cal_cons = ThresholdCalibrator("conservative").calibrate_from_fold_results(probas, actuals)

        # conservative 阈值 >= mean 阈值
        assert cal_cons.recommended_buy_threshold >= cal_mean.recommended_buy_threshold - 1e-6

    def test_invalid_strategy_raises(self):
        """无效的 aggregation_strategy 应该抛出 ValueError。"""
        with pytest.raises(ValueError, match="aggregation_strategy"):
            ThresholdCalibrator(aggregation_strategy="invalid")

    def test_empty_input_raises(self):
        """空输入应该抛出 ValueError。"""
        calibrator = ThresholdCalibrator()
        with pytest.raises(ValueError, match="至少需要一折"):
            calibrator.calibrate_from_fold_results([], [])

    def test_mismatched_lengths_raises(self):
        """概率和标签长度不一致应该抛出 ValueError。"""
        p, a = make_proba_actual(100)
        calibrator = ThresholdCalibrator()
        with pytest.raises(ValueError, match="长度必须相同"):
            calibrator.calibrate_from_fold_results([p], [a, a])

    def test_all_same_class_falls_back(self):
        """全部为同一类标签时，应该优雅降级（不崩溃）。"""
        proba = pd.Series([0.6] * 100)
        actual = pd.Series([1] * 100)  # 全部正样本
        calibrator = ThresholdCalibrator()
        result = calibrator.calibrate_from_fold_results([proba], [actual])
        # 降级为默认阈值 0.5
        assert result.recommended_buy_threshold == pytest.approx(0.5, abs=0.01)


# ─────────────────────────────────────────────────────────────
# CalibrationResult 序列化测试
# ─────────────────────────────────────────────────────────────

class TestCalibrationResultSerialization:
    def _make_cal_result(self) -> CalibrationResult:
        probas, actuals = make_fold_data(3)
        return ThresholdCalibrator("median").calibrate_from_fold_results(probas, actuals)

    def test_save_and_load_roundtrip(self, tmp_path: Path):
        result = self._make_cal_result()
        path = tmp_path / "threshold.json"
        result.save(path)

        loaded = CalibrationResult.load(path)
        assert loaded.version == result.version
        assert abs(loaded.recommended_buy_threshold - result.recommended_buy_threshold) < 1e-6
        assert len(loaded.fold_thresholds) == len(result.fold_thresholds)

    def test_loaded_threshold_in_bounds(self, tmp_path: Path):
        result = self._make_cal_result()
        path = tmp_path / "threshold.json"
        result.save(path)
        loaded = CalibrationResult.load(path)

        assert 0.4 <= loaded.recommended_buy_threshold <= 0.85
        assert 0.15 <= loaded.recommended_sell_threshold <= 0.60

    def test_fold_thresholds_preserved(self, tmp_path: Path):
        probas, actuals = make_fold_data(3)
        result = ThresholdCalibrator().calibrate_from_fold_results(probas, actuals)
        path = tmp_path / "t.json"
        result.save(path)
        loaded = CalibrationResult.load(path)

        for orig, load in zip(result.fold_thresholds, loaded.fold_thresholds):
            assert orig.fold_id == load.fold_id
            assert abs(orig.optimal_buy_threshold - load.optimal_buy_threshold) < 1e-6


# ─────────────────────────────────────────────────────────────
# ModelRegistry 测试
# ─────────────────────────────────────────────────────────────

class FakeDummyModel:
    """可 pickle 的假模型对象。"""
    def __init__(self, name: str = "dummy"):
        self.name = name

    def predict(self, X):
        return [0] * len(X)


class TestModelRegistry:
    def test_register_creates_pkl_and_json(self, tmp_path: Path):
        registry = ModelRegistry(models_dir=tmp_path)
        model = FakeDummyModel("m1")
        vid = registry.register(model, model_type="rf", oos_auc=0.72)

        assert (tmp_path / f"{vid}.pkl").exists()
        assert (tmp_path / "registry.json").exists()

    def test_promote_sets_active(self, tmp_path: Path):
        registry = ModelRegistry(models_dir=tmp_path)
        vid = registry.register(FakeDummyModel(), oos_auc=0.70)
        registry.promote(vid)

        assert registry.active_version is not None
        assert registry.active_version.version_id == vid
        assert registry.active_version.is_active is True

    def test_load_active(self, tmp_path: Path):
        registry = ModelRegistry(models_dir=tmp_path)
        original = FakeDummyModel("test_model")
        vid = registry.register(original, oos_auc=0.68)
        registry.promote(vid)

        loaded = registry.load_active()
        assert loaded.name == "test_model"

    def test_rollback(self, tmp_path: Path):
        registry = ModelRegistry(models_dir=tmp_path)
        vid1 = registry.register(FakeDummyModel("v1"), oos_auc=0.65)
        registry.promote(vid1)
        vid2 = registry.register(FakeDummyModel("v2"), oos_auc=0.70)
        registry.promote(vid2)

        rolled_back = registry.rollback()
        assert rolled_back == vid1
        assert registry.active_version.version_id == vid1

    def test_rollback_with_single_version_returns_none(self, tmp_path: Path):
        registry = ModelRegistry(models_dir=tmp_path)
        vid = registry.register(FakeDummyModel(), oos_auc=0.6)
        registry.promote(vid)
        result = registry.rollback()
        assert result is None

    def test_promote_unknown_raises(self, tmp_path: Path):
        registry = ModelRegistry(models_dir=tmp_path)
        with pytest.raises(KeyError):
            registry.promote("nonexistent_version")

    def test_no_active_load_raises(self, tmp_path: Path):
        registry = ModelRegistry(models_dir=tmp_path)
        registry.register(FakeDummyModel())  # 注册但不 promote
        with pytest.raises(RuntimeError, match="没有激活"):
            registry.load_active()

    def test_registry_persists_across_instances(self, tmp_path: Path):
        """重新实例化 ModelRegistry 后，注册数据应该从磁盘恢复。"""
        r1 = ModelRegistry(models_dir=tmp_path)
        vid = r1.register(FakeDummyModel("persisted"), oos_auc=0.75)
        r1.promote(vid)

        r2 = ModelRegistry(models_dir=tmp_path)
        assert len(r2.all_versions) == 1
        assert r2.active_version.version_id == vid

    def test_auto_promote_on_register(self, tmp_path: Path):
        registry = ModelRegistry(models_dir=tmp_path)
        vid = registry.register(FakeDummyModel(), oos_auc=0.71, auto_promote=True)
        assert registry.active_version.version_id == vid

    def test_diagnostics_keys(self, tmp_path: Path):
        registry = ModelRegistry(models_dir=tmp_path)
        diag = registry.diagnostics()
        assert "total_versions" in diag
        assert "active_version" in diag
        assert "versions" in diag


# ─────────────────────────────────────────────────────────────
# MLDiagnostics 测试
# ─────────────────────────────────────────────────────────────

class TestMLDiagnostics:
    def _make_mock_wf_result(self):
        """构造一个最小化的 WalkForwardResult 模拟对象。"""
        from modules.alpha.ml.trainer import FoldResult, WalkForwardResult
        fold = FoldResult(
            fold_id=0,
            train_size=300,
            test_size=100,
            train_start="2023-01-01",
            train_end="2023-04-01",
            test_start="2023-04-02",
            test_end="2023-05-01",
            accuracy=0.65,
            f1=0.60,
            precision=0.62,
            recall=0.58,
            auc=0.72,
            oos_predictions=pd.Series([1, 0, 1, 0]),
            oos_probabilities=pd.Series([0.7, 0.3, 0.8, 0.2]),
            oos_actual=pd.Series([1, 0, 1, 0]),
            optimal_threshold=0.58,
        )
        return WalkForwardResult(
            fold_results=[fold],
            feature_names=["f1", "f2", "f3"],
            optimal_buy_threshold=0.58,
            optimal_sell_threshold=0.42,
        )

    def test_report_walk_forward_returns_dict(self):
        diag = MLDiagnostics()
        wf = self._make_mock_wf_result()
        report = diag.report_walk_forward(wf, tag="test")

        assert report["type"] == "walk_forward"
        assert report["n_folds"] == 1
        assert diag.report_count == 1

    def test_report_calibration_returns_dict(self):
        diag = MLDiagnostics()
        probas, actuals = make_fold_data(2)
        cal = ThresholdCalibrator().calibrate_from_fold_results(probas, actuals)
        report = diag.report_calibration(cal, tag="test")

        assert report["type"] == "calibration"
        assert "recommended_buy" in report

    def test_report_oos_probabilities(self):
        diag = MLDiagnostics()
        proba, _ = make_proba_actual(200)
        report = diag.report_oos_probabilities(proba, threshold=0.6)

        assert report["n_total"] == 200
        assert 0.0 <= report["signal_rate"] <= 1.0

    def test_save_report(self, tmp_path: Path):
        diag = MLDiagnostics()
        proba, actual = make_proba_actual()
        cal = ThresholdCalibrator().calibrate_from_fold_results([proba], [actual])
        diag.report_calibration(cal)

        path = tmp_path / "diag.json"
        diag.save_report(path)
        assert path.exists()

        import json
        with open(path) as f:
            data = json.load(f)
        assert data["report_count"] == 1

    def test_clear_resets_report_count(self):
        diag = MLDiagnostics()
        proba, _ = make_proba_actual()
        diag.report_oos_probabilities(proba)
        assert diag.report_count == 1
        diag.clear()
        assert diag.report_count == 0
