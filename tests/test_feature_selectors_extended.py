"""
tests/test_feature_selectors_extended.py — 补充覆盖 feature_selectors.py 缺失分支

Missed lines: 95, 142-146, 154, 182, 188, 221-226, 229-257, 260-280, 283-285
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from modules.alpha.ml.feature_selectors import (
    Decorrelator,
    DecorrelatorConfig,
    PCAConfig,
    PCAReducer,
    VarianceFilter,
    VarianceFilterConfig,
)


def _make_df(n: int = 50) -> pd.DataFrame:
    rng = np.random.RandomState(42)
    return pd.DataFrame({
        "a": rng.randn(n),
        "b": rng.randn(n),
        "c": rng.randn(n),
        "d": rng.randn(n),
    })


# ══════════════════════════════════════════════════════════════
# VarianceFilter
# ══════════════════════════════════════════════════════════════

class TestVarianceFilter:

    def test_fit_transform_keeps_all_high_variance(self):
        vf = VarianceFilter()
        df = _make_df()
        vf.fit(df)
        result = vf.transform(df)
        # All columns have high variance, should keep all
        assert set(result.columns) == set(df.columns)

    def test_drops_constant_column(self):
        df = _make_df()
        df["constant"] = 1.0  # zero variance
        vf = VarianceFilter()
        vf.fit(df)
        result = vf.transform(df)
        assert "constant" not in result.columns

    def test_drops_high_nan_column(self):
        df = _make_df()
        col = [np.nan] * 45 + [1.0] * 5
        df["mostly_nan"] = col  # 90% NaN
        vf = VarianceFilter(VarianceFilterConfig(nan_threshold=0.5))
        vf.fit(df)
        result = vf.transform(df)
        assert "mostly_nan" not in result.columns

    def test_transform_before_fit_raises(self):
        vf = VarianceFilter()
        with pytest.raises(RuntimeError, match="fit"):
            vf.transform(_make_df())

    def test_diagnostics(self):
        vf = VarianceFilter()
        df = _make_df()
        vf.fit(df)
        d = vf.diagnostics()
        assert "dropped_count" in d
        assert "fitted" in d
        assert d["fitted"] is True

    def test_dropped_cols_property(self):
        df = _make_df()
        df["zero_var"] = 0.0
        vf = VarianceFilter()
        vf.fit(df)
        assert "zero_var" in vf.dropped_cols


# ══════════════════════════════════════════════════════════════
# Decorrelator
# ══════════════════════════════════════════════════════════════

class TestDecorrelator:

    def test_fit_transform_normal(self):
        dc = Decorrelator()
        df = _make_df()
        dc.fit(df)
        result = dc.transform(df)
        assert isinstance(result, pd.DataFrame)
        assert len(result.columns) > 0

    def test_drops_highly_correlated_column(self):
        rng = np.random.RandomState(42)
        a = rng.randn(100)
        df = pd.DataFrame({
            "a": a,
            "b": a + rng.randn(100) * 0.001,  # nearly identical to a
            "c": rng.randn(100),
        })
        dc = Decorrelator(DecorrelatorConfig(correlation_threshold=0.99))
        dc.fit(df)
        result = dc.transform(df)
        # 'b' should be dropped (high correlation with 'a')
        assert "a" in result.columns
        assert "b" not in result.columns

    def test_empty_numeric_df_does_not_crash(self):
        df = pd.DataFrame({"text": ["a", "b", "c"]})
        dc = Decorrelator()
        dc.fit(df)
        result = dc.transform(df)
        assert isinstance(result, pd.DataFrame)

    def test_transform_before_fit_raises(self):
        dc = Decorrelator()
        with pytest.raises(RuntimeError, match="fit"):
            dc.transform(_make_df())

    def test_diagnostics(self):
        dc = Decorrelator()
        dc.fit(_make_df())
        d = dc.diagnostics()
        assert "kept_count" in d
        assert "dropped_count" in d
        assert d["fitted"] is True

    def test_non_numeric_cols_preserved(self):
        df = _make_df()
        df["symbol"] = "BTC"  # non-numeric
        dc = Decorrelator()
        dc.fit(df)
        result = dc.transform(df)
        assert "symbol" in result.columns


# ══════════════════════════════════════════════════════════════
# PCAReducer
# ══════════════════════════════════════════════════════════════

class TestPCAReducer:

    def test_fit_transform_normal(self):
        pca = PCAReducer()
        df = _make_df(100)
        pca.fit(df)
        result = pca.transform(df)
        assert isinstance(result, pd.DataFrame)
        assert all(c.startswith("pc_") for c in result.columns)

    def test_empty_df_does_not_crash(self):
        pca = PCAReducer()
        df = pd.DataFrame({"text": ["a", "b", "c"]})
        pca.fit(df)
        # transform with empty input — pca is None
        result = pca.transform(df)
        assert isinstance(result, pd.DataFrame)

    def test_transform_before_fit_raises(self):
        pca = PCAReducer()
        with pytest.raises(RuntimeError, match="fit"):
            pca.transform(_make_df())

    def test_fixed_n_components(self):
        pca = PCAReducer(PCAConfig(n_components=2, scale_before_pca=True))
        df = _make_df(100)
        pca.fit(df)
        result = pca.transform(df)
        assert len(result.columns) >= 2  # at least 2 pca columns

    def test_diagnostics_after_fit(self):
        pca = PCAReducer()
        pca.fit(_make_df(100))
        d = pca.diagnostics()
        assert "fitted" in d
        assert "output_components" in d
        assert d["fitted"] is True

    def test_diagnostics_before_fit_no_pca(self):
        pca = PCAReducer()
        # Manually set fitted=False, _pca=None
        d = pca.diagnostics()
        assert "components" in d

    def test_non_numeric_cols_preserved_after_pca(self):
        df = _make_df(100)
        df["symbol"] = "BTC"
        pca = PCAReducer()
        pca.fit(df)
        result = pca.transform(df)
        assert "symbol" in result.columns

    def test_no_scale_before_pca(self):
        pca = PCAReducer(PCAConfig(scale_before_pca=False))
        df = _make_df(100)
        pca.fit(df)
        result = pca.transform(df)
        assert isinstance(result, pd.DataFrame)
