"""
tests/test_data_kitchen.py — DataKitchen 特征中台单元测试

覆盖项：
- VarianceFilter: 低方差列剔除、高NaN列剔除
- Decorrelator: 高相关列剔除、去相关后列数减少
- FeaturePipeline: 多 stage 组合、fit/transform 一致性
- FeatureContract: 签名生成、validate 通过/失败、save/load 往返
- DataKitchen.fit(): 三视图产出、契约生成、列数正确
- DataKitchen.transform(): 推理期契约验证、列名一致
- DataKitchen: 配置开关（关闭 decorrelation、关闭 variance filter）
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from modules.alpha.ml.data_kitchen import DataKitchen, DataKitchenConfig
from modules.alpha.ml.feature_contract import FeatureContract
from modules.alpha.ml.feature_pipeline import FeaturePipeline
from modules.alpha.ml.feature_selectors import (
    Decorrelator,
    DecorrelatorConfig,
    VarianceFilter,
    VarianceFilterConfig,
)


# ─────────────────────────────────────────────────────────────
# 测试数据生成
# ─────────────────────────────────────────────────────────────

def make_ohlcv(n: int = 400, seed: int = 42) -> pd.DataFrame:
    """生成合成 OHLCV 数据（与 test_ml_alpha.py 保持一致的生成方式）。"""
    rng = np.random.RandomState(seed)
    ts = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    prices = 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n)))
    noise = rng.uniform(0.005, 0.015, n)
    return pd.DataFrame({
        "timestamp": ts,
        "symbol":    "BTC/USDT",
        "open":      prices * (1 - noise / 4),
        "high":      prices * (1 + noise / 2),
        "low":       prices * (1 - noise / 2),
        "close":     prices,
        "volume":    rng.uniform(100, 500, n),
    })


def make_numeric_df(n: int = 200, ncols: int = 10) -> pd.DataFrame:
    """生成纯数值测试 DataFrame，用于 selector 单元测试。"""
    rng = np.random.RandomState(0)
    cols = {f"f{i}": rng.randn(n) for i in range(ncols)}
    return pd.DataFrame(cols)


# ─────────────────────────────────────────────────────────────
# VarianceFilter 测试
# ─────────────────────────────────────────────────────────────

class TestVarianceFilter:
    def test_drops_zero_variance_column(self):
        """常数列（方差=0）应该被剔除。"""
        df = make_numeric_df()
        df["const_col"] = 1.0  # 常数列

        vf = VarianceFilter()
        vf.fit(df)
        X_out = vf.transform(df)

        assert "const_col" not in X_out.columns
        assert len(X_out.columns) == len(df.columns) - 1

    def test_drops_high_nan_column(self):
        """高 NaN 比例列（>50%）应该被剔除。"""
        df = make_numeric_df(n=100)
        # 70% NaN
        df["nan_heavy"] = np.where(np.arange(100) < 70, np.nan, 1.0)

        vf = VarianceFilter(VarianceFilterConfig(nan_threshold=0.5))
        vf.fit(df)
        X_out = vf.transform(df)

        assert "nan_heavy" not in X_out.columns

    def test_keeps_normal_columns(self):
        """正常列不应被剔除。"""
        df = make_numeric_df(n=200, ncols=5)
        vf = VarianceFilter()
        vf.fit(df)
        X_out = vf.transform(df)

        # 全部正常列都应该保留
        assert len(X_out.columns) == 5

    def test_transform_without_fit_raises(self):
        vf = VarianceFilter()
        with pytest.raises(RuntimeError, match="必须先调用 fit"):
            vf.transform(make_numeric_df())


# ─────────────────────────────────────────────────────────────
# Decorrelator 测试
# ─────────────────────────────────────────────────────────────

class TestDecorrelator:
    def test_drops_highly_correlated_column(self):
        """高度相关列（相关系数>0.95）应该被剔除其中一个。"""
        n = 200
        rng = np.random.RandomState(1)
        base = rng.randn(n)
        df = pd.DataFrame({
            "a": base,
            "b": base * 2.0 + rng.randn(n) * 0.001,  # 与 a 高度相关
            "c": rng.randn(n),                          # 独立列
        })

        decorr = Decorrelator(DecorrelatorConfig(correlation_threshold=0.95))
        decorr.fit(df)
        X_out = decorr.transform(df)

        # 应该剔除 b（与 a 高度相关），保留 a 和 c
        assert "a" in X_out.columns
        assert "c" in X_out.columns
        assert "b" not in X_out.columns

    def test_keeps_independent_columns(self):
        """独立列不应被剔除。"""
        rng = np.random.RandomState(2)
        df = pd.DataFrame({
            "x": rng.randn(200),
            "y": rng.randn(200),
            "z": rng.randn(200),
        })
        decorr = Decorrelator()
        decorr.fit(df)
        X_out = decorr.transform(df)

        # 三个独立列都应该保留
        assert len(X_out.columns) == 3

    def test_diagnostics_reports_dropped(self):
        """diagnostics() 应该报告剔除了多少列。"""
        n = 100
        rng = np.random.RandomState(3)
        base = rng.randn(n)
        df = pd.DataFrame({
            "a": base,
            "b": base * 1.5 + rng.randn(n) * 0.001,
        })
        decorr = Decorrelator(DecorrelatorConfig(correlation_threshold=0.9))
        decorr.fit(df)
        diag = decorr.diagnostics()

        assert diag["dropped_count"] >= 1
        assert diag["fitted"] is True


# ─────────────────────────────────────────────────────────────
# FeaturePipeline 测试
# ─────────────────────────────────────────────────────────────

class TestFeaturePipeline:
    def test_single_stage_fit_transform(self):
        """单 stage pipeline fit_transform 应与直接 stage 一致。"""
        df = make_numeric_df(n=200)
        df["const"] = 0.0

        pipeline = FeaturePipeline(name="test")
        pipeline.add_stage("variance_filter", VarianceFilter())

        X_out = pipeline.fit_transform(df)
        assert "const" not in X_out.columns

    def test_two_stage_pipeline(self):
        """两个 stage 链式处理，结果应该比输入列更少。"""
        n = 200
        rng = np.random.RandomState(5)
        base = rng.randn(n)
        df = pd.DataFrame({
            "a": base,
            "b": base * 2.0 + rng.randn(n) * 0.001,  # 高相关
            "c": rng.randn(n),
            "const": 0.0,  # 零方差
        })

        pipeline = (
            FeaturePipeline(name="two_stage")
            .add_stage("var_filter", VarianceFilter())
            .add_stage("decorrelator", Decorrelator(DecorrelatorConfig(correlation_threshold=0.9)))
        )
        X_out = pipeline.fit_transform(df)

        # const 被 var_filter 剔除，b 被 decorrelator 剔除
        assert "const" not in X_out.columns
        assert "b" not in X_out.columns
        assert "a" in X_out.columns
        assert "c" in X_out.columns

    def test_transform_without_fit_raises(self):
        pipeline = FeaturePipeline()
        pipeline.add_stage("vf", VarianceFilter())
        with pytest.raises(RuntimeError, match="必须先调用 fit"):
            pipeline.transform(make_numeric_df())

    def test_diagnostics_returns_stages(self):
        pipeline = FeaturePipeline(name="diag_test")
        pipeline.add_stage("vf", VarianceFilter())
        pipeline.fit_transform(make_numeric_df())

        diag = pipeline.diagnostics()
        assert diag["stage_count"] == 1
        assert len(diag["stages"]) == 1
        assert diag["fitted"] is True


# ─────────────────────────────────────────────────────────────
# FeatureContract 测试
# ─────────────────────────────────────────────────────────────

class TestFeatureContract:
    def _make_contract(self, n_features: int = 10) -> FeatureContract:
        features = [f"feat_{i}" for i in range(n_features)]
        return FeatureContract(
            version="dk_v1_202601",
            alpha_features=features,
            regime_features=features[:4],
            diagnostic_features=features[:3],
        )

    def test_signature_computed_on_init(self):
        contract = self._make_contract(10)
        assert contract.signature != ""
        assert len(contract.signature) == 16  # SHA256 前 16 位

    def test_validate_passes_when_all_present(self):
        contract = self._make_contract(5)
        df_cols = [f"feat_{i}" for i in range(10)]  # 超集
        ok, missing = contract.validate(df_cols)
        assert ok is True
        assert missing == []

    def test_validate_fails_when_missing_features(self):
        contract = self._make_contract(5)
        df_cols = [f"feat_{i}" for i in range(3)]  # 只有 3 列，缺 2 列
        ok, missing = contract.validate(df_cols)
        assert ok is False
        assert len(missing) == 2

    def test_save_and_load_roundtrip(self, tmp_path: Path):
        contract = self._make_contract(8)
        path = tmp_path / "contract.json"
        contract.save(path)

        loaded = FeatureContract.load(path)
        assert loaded.version == contract.version
        assert loaded.signature == contract.signature
        assert loaded.alpha_features == contract.alpha_features

    def test_different_feature_order_produces_different_signature(self):
        f1 = FeatureContract(version="v1", alpha_features=["a", "b", "c"])
        f2 = FeatureContract(version="v1", alpha_features=["c", "b", "a"])
        assert f1.signature != f2.signature


# ─────────────────────────────────────────────────────────────
# DataKitchen 集成测试
# ─────────────────────────────────────────────────────────────

class TestDataKitchen:
    def test_fit_produces_three_views(self):
        """fit() 应该产出 alpha/regime/diagnostic 三个视图。"""
        dk = DataKitchen(DataKitchenConfig(
            enable_variance_filter=True,
            enable_decorrelation=True,
            enable_pca=False,
        ))
        views, contract = dk.fit(make_ohlcv(400))

        assert "alpha_features" in views
        assert "regime_features" in views
        assert "diagnostic_features" in views

    def test_fit_alpha_features_not_empty(self):
        """alpha_features 不应该是空 DataFrame。"""
        dk = DataKitchen()
        views, _ = dk.fit(make_ohlcv(400))

        X = views["alpha_features"]
        assert len(X) > 100  # 至少保留 100 行（去掉预热期）
        assert len(X.columns) > 10  # 至少 10 个特征

    def test_fit_produces_valid_contract(self):
        """fit() 产出的 FeatureContract 应该有正确的签名和列名。"""
        dk = DataKitchen()
        views, contract = dk.fit(make_ohlcv(400))

        assert contract.version.startswith("dk_v1")
        assert len(contract.signature) == 16
        assert len(contract.alpha_features) == len(views["alpha_features"].columns)
        assert len(contract.regime_features) == len(views["regime_features"].columns)

    def test_transform_consistent_with_fit(self):
        """transform() 产出的特征列应与 fit() 完全一致。"""
        df = make_ohlcv(400)
        train_df = df.iloc[:300]
        infer_df = df.iloc[200:]  # 部分重叠，但结构一致

        dk = DataKitchen()
        views_train, _ = dk.fit(train_df)

        views_infer = dk.transform(infer_df, validate_contract=True)

        # 列名应该相同
        assert list(views_train["alpha_features"].columns) == list(
            views_infer["alpha_features"].columns
        )
        assert list(views_train["regime_features"].columns) == list(
            views_infer["regime_features"].columns
        )
        assert list(views_train["diagnostic_features"].columns) == list(
            views_infer["diagnostic_features"].columns
        )

    def test_contract_validate_detects_missing_feature(self):
        """手动验证缺少特征时应返回 False。"""
        dk = DataKitchen()
        views, contract = dk.fit(make_ohlcv(400))

        # 假设缺少一个特征列
        partial_cols = list(views["alpha_features"].columns)[:-2]
        ok, missing = contract.validate(partial_cols)

        assert ok is False
        assert len(missing) == 2

    def test_regime_features_preserve_detector_core_columns(self):
        """regime_features 应保留 detector 依赖的核心列，不受 ML selector 裁剪影响。"""
        dk = DataKitchen()
        views, _ = dk.fit(make_ohlcv(400))

        regime_cols = set(views["regime_features"].columns)
        required = {
            "ret_roll_mean_20",
            "ret_roll_std_20",
            "price_vs_sma_20",
            "price_vs_sma_50",
            "adx_14",
            "rsi_14",
            "atr_pct_14",
            "bb_width",
            "volume_ratio",
        }

        assert required.issubset(regime_cols)

    def test_contract_save_load_roundtrip(self, tmp_path: Path):
        """FeatureContract 保存后重新加载，签名和列名一致。"""
        dk = DataKitchen()
        views, contract = dk.fit(make_ohlcv(400))

        path = tmp_path / "contract.json"
        contract.save(path)
        loaded = FeatureContract.load(path)

        assert loaded.signature == contract.signature
        assert loaded.alpha_features == contract.alpha_features

    def test_config_disable_decorrelation(self):
        """禁用去相关时，特征列数应 >= 启用时。"""
        df = make_ohlcv(400)

        dk_default = DataKitchen(DataKitchenConfig(enable_decorrelation=True))
        views_default, _ = dk_default.fit(df)

        dk_nodecorr = DataKitchen(DataKitchenConfig(enable_decorrelation=False))
        views_nodecorr, _ = dk_nodecorr.fit(df)

        # 禁用去相关，保留列应 ≥ 启用去相关时
        assert len(views_nodecorr["alpha_features"].columns) >= \
               len(views_default["alpha_features"].columns)

    def test_diagnostics_output(self):
        """diagnostics() 应该返回含 fitted 的字典。"""
        dk = DataKitchen()
        dk.fit(make_ohlcv(300))
        diag = dk.diagnostics()

        assert diag["fitted"] is True
        assert "pipeline" in diag
        assert diag["contract_version"] is not None
