"""
modules/alpha/ml/feature_selectors.py — 特征降维与筛选器

设计说明：
- 提供三种可组合的特征处理器：PCA 降维、去相关处理、低方差筛选
- 均遵循 sklearn-like fit/transform 接口
- 设计为无状态函数包装 + 有状态对象（fit 后可 transform）
- 支持诊断输出（dropped_cols、explained_variance 等）

日志标签：[FeatureSelector] [PCA] [Decorr] [VarFilter]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler

from core.logger import get_logger

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 1. 低方差过滤器
# ══════════════════════════════════════════════════════════════

@dataclass
class VarianceFilterConfig:
    """低方差过滤器配置。"""
    min_variance: float = 1e-6  # 方差低于此阈值的列被剔除
    drop_na_cols: bool = True   # 超过 50% NaN 的列也被剔除
    nan_threshold: float = 0.5  # NaN 比例阈值


class VarianceFilter:
    """
    剔除低方差或高 NaN 比例的特征列。

    fit 后记录被剔除的列，transform 时应用相同过滤。
    """

    def __init__(self, config: VarianceFilterConfig | None = None) -> None:
        self.config = config or VarianceFilterConfig()
        self._dropped_cols: list[str] = []
        self._fitted = False

    def fit(self, X: pd.DataFrame) -> "VarianceFilter":
        cfg = self.config
        dropped: list[str] = []

        # 高 NaN 比例列
        if cfg.drop_na_cols:
            nan_ratio = X.isna().mean()
            high_nan = nan_ratio[nan_ratio > cfg.nan_threshold].index.tolist()
            dropped.extend(high_nan)
            if high_nan:
                log.debug(
                    "[VarFilter] 高NaN列剔除: count={} cols={}",
                    len(high_nan), high_nan[:5],
                )

        # 低方差列（仅对数值列计算）
        numeric_cols = [c for c in X.columns if c not in dropped]
        X_num = X[numeric_cols].select_dtypes(include=[np.number])
        var = X_num.var()
        low_var = var[var < cfg.min_variance].index.tolist()
        dropped.extend([c for c in low_var if c not in dropped])
        if low_var:
            log.debug(
                "[VarFilter] 低方差列剔除: count={} cols={}",
                len(low_var), low_var[:5],
            )

        self._dropped_cols = dropped
        self._fitted = True
        log.info(
            "[VarFilter] fit完成: 输入={} 剔除={} 保留={}",
            len(X.columns), len(dropped), len(X.columns) - len(dropped),
        )
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        if not self._fitted:
            raise RuntimeError("VarianceFilter.transform() 必须先调用 fit()")
        keep = [c for c in X.columns if c not in self._dropped_cols]
        return X[keep]

    @property
    def dropped_cols(self) -> list[str]:
        return list(self._dropped_cols)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "dropped_count": len(self._dropped_cols),
            "dropped_cols": self._dropped_cols,
            "fitted": self._fitted,
        }


# ══════════════════════════════════════════════════════════════
# 2. 去相关器（高度相关特征剔除）
# ══════════════════════════════════════════════════════════════

@dataclass
class DecorrelatorConfig:
    """去相关配置。"""
    correlation_threshold: float = 0.95  # 皮尔逊相关系数阈值（>= 此值则剔除其中一个）
    method: str = "pearson"              # 'pearson' | 'spearman'


class Decorrelator:
    """
    去相关处理器：对高度相关的特征对，保留先出现的那个，剔除后者。

    使用贪心策略（与 freqtrade DataKitchen 的 datasieve 对齐）：
    逐列扫描，如果某列与已选列的相关系数超过阈值，则剔除该列。
    """

    def __init__(self, config: DecorrelatorConfig | None = None) -> None:
        self.config = config or DecorrelatorConfig()
        self._dropped_cols: list[str] = []
        self._kept_cols: list[str] = []
        self._fitted = False

    def fit(self, X: pd.DataFrame) -> "Decorrelator":
        cfg = self.config
        X_num = X.select_dtypes(include=[np.number]).dropna()

        if X_num.empty:
            log.warning("[Decorr] 输入矩阵全为非数值或全NaN，跳过去相关")
            self._kept_cols = list(X.columns)
            self._fitted = True
            return self

        try:
            corr_matrix = X_num.corr(method=cfg.method).abs()
        except Exception as exc:  # noqa: BLE001
            log.warning("[Decorr] 相关矩阵计算失败: {} 跳过去相关", exc)
            self._kept_cols = list(X.columns)
            self._fitted = True
            return self

        kept: list[str] = []
        dropped: list[str] = []
        cols = list(X_num.columns)

        for col in cols:
            if col in dropped:
                continue
            # 检查与已保留列的相关性
            redundant = False
            for kept_col in kept:
                if col in corr_matrix.index and kept_col in corr_matrix.columns:
                    if corr_matrix.loc[col, kept_col] >= cfg.correlation_threshold:
                        dropped.append(col)
                        redundant = True
                        break
            if not redundant:
                kept.append(col)

        # 非数值列直接保留
        non_numeric = [c for c in X.columns if c not in X_num.columns]
        self._kept_cols = kept + non_numeric
        self._dropped_cols = dropped
        self._fitted = True

        log.info(
            "[Decorr] fit完成: 输入={} 去相关剔除={} 保留={}",
            len(X.columns), len(dropped), len(self._kept_cols),
        )
        if dropped:
            log.debug("[Decorr] 剔除的高相关列: {}", dropped[:8])
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        if not self._fitted:
            raise RuntimeError("Decorrelator.transform() 必须先调用 fit()")
        keep = [c for c in X.columns if c in set(self._kept_cols)]
        return X[keep]

    @property
    def dropped_cols(self) -> list[str]:
        return list(self._dropped_cols)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "kept_count": len(self._kept_cols),
            "dropped_count": len(self._dropped_cols),
            "dropped_cols": self._dropped_cols,
            "fitted": self._fitted,
        }


# ══════════════════════════════════════════════════════════════
# 3. PCA 压缩器（可选降维，保留方差比例）
# ══════════════════════════════════════════════════════════════

@dataclass
class PCAConfig:
    """PCA 配置。"""
    n_components: float | int = 0.95  # float: 保留方差比例；int: 固定主成分数
    scale_before_pca: bool = True      # PCA 前是否 StandardScaler 标准化


class PCAReducer:
    """
    PCA 降维器。

    注意：PCA 输出的列名为 pc_0, pc_1, ... 而不是原始特征名。
    这会让特征可解释性降低，但在特征数量极大时有助于防止维度灾难。

    建议只在特征数 > 100 时使用。
    """

    def __init__(self, config: PCAConfig | None = None) -> None:
        self.config = config or PCAConfig()
        self._scaler: StandardScaler | None = None
        self._pca: PCA | None = None
        self._input_cols: list[str] = []
        self._output_cols: list[str] = []
        self._fitted = False

    def fit(self, X: pd.DataFrame) -> "PCAReducer":
        cfg = self.config
        X_num = X.select_dtypes(include=[np.number]).dropna()

        if X_num.empty:
            log.warning("[PCA] 输入为空或无数值列，跳过PCA")
            self._fitted = True
            return self

        self._input_cols = list(X_num.columns)

        X_arr = X_num.values
        if cfg.scale_before_pca:
            self._scaler = StandardScaler()
            X_arr = self._scaler.fit_transform(X_arr)

        pca = PCA(n_components=cfg.n_components)
        pca.fit(X_arr)
        self._pca = pca

        n_comp = pca.n_components_
        self._output_cols = [f"pc_{i}" for i in range(n_comp)]
        explained_var = float(pca.explained_variance_ratio_.sum())

        log.info(
            "[PCA] fit完成: 输入特征={} → 主成分={} 保留方差={:.3f}",
            len(self._input_cols), n_comp, explained_var,
        )
        self._fitted = True
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        if not self._fitted:
            raise RuntimeError("PCAReducer.transform() 必须先调用 fit()")

        if self._pca is None:
            return X  # 跳过（空数据路径）

        X_num = X[self._input_cols].dropna()
        X_arr = X_num.values

        if self._scaler is not None:
            X_arr = self._scaler.transform(X_arr)

        X_pca = self._pca.transform(X_arr)
        result = pd.DataFrame(X_pca, columns=self._output_cols, index=X_num.index)

        # 保留非数值列（如 timestamp、symbol）
        non_numeric = [c for c in X.columns if c not in self._input_cols]
        for col in non_numeric:
            result[col] = X.loc[X_num.index, col]

        return result

    def diagnostics(self) -> dict[str, Any]:
        if self._pca is None:
            return {"fitted": self._fitted, "components": 0}
        return {
            "fitted": self._fitted,
            "input_features": len(self._input_cols),
            "output_components": len(self._output_cols),
            "explained_variance_ratio": self._pca.explained_variance_ratio_.tolist(),
            "total_explained_variance": float(self._pca.explained_variance_ratio_.sum()),
        }
