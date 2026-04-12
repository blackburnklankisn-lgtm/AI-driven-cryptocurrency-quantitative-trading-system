"""
modules/alpha/ml/model.py — ML 信号模型封装

设计说明：
- 封装 sklearn Pipeline，统一训练/预测接口
- 支持多种基模型：LightGBM（首选）、RandomForest（后备）、LogisticRegression（基线）
- 所有模型均经过特征归一化预处理（Pipeline 保证一致性）
- 模型持久化：pickle 保存整个 Pipeline，确保归一化参数与模型参数一起保存

防过拟合措施：
- 强制设置 class_weight（处理不平衡类别）
- 对 LightGBM 启用 early_stopping（需要验证集）
- 特征重要性记录（便于诊断问题特征）

接口：
    SignalModel(model_type, params)
    .fit(X_train, y_train, X_val, y_val)   → self
    .predict(X)                             → np.ndarray（预测类别）
    .predict_proba(X)                       → np.ndarray（各类概率）
    .get_feature_importance()              → pd.Series
    .save(path)                            → Path
    .load(path)                            → SignalModel（类方法）
"""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from core.logger import get_logger

log = get_logger(__name__)

# LightGBM 可选（首选），不安装时降级到 RandomForest
try:
    import lightgbm as lgb
    _LGBM_AVAILABLE = True
    log.info("LightGBM 可用，将使用 LGBM 作为主模型")
except ImportError:
    _LGBM_AVAILABLE = False
    log.warning("LightGBM 未安装（pip install lightgbm），将使用 RandomForest 作为替代")


# ─── 默认超参数配置 ────────────────────────────────────────────

_LGBM_DEFAULT_PARAMS: Dict[str, Any] = {
    "objective": "multiclass",
    "num_class": 2,           # binary 任务时设为 2，三分类设为 3
    "n_estimators": 300,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "max_depth": -1,
    "min_child_samples": 30,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "lambda_l1": 0.1,
    "lambda_l2": 0.1,
    "verbose": -1,
    "n_jobs": -1,
    "random_state": 42,
}

_RF_DEFAULT_PARAMS: Dict[str, Any] = {
    "n_estimators": 200,
    "max_depth": 8,
    "min_samples_leaf": 30,
    "max_features": "sqrt",
    "class_weight": "balanced",
    "n_jobs": -1,
    "random_state": 42,
}

_LR_DEFAULT_PARAMS: Dict[str, Any] = {
    "C": 0.1,
    "max_iter": 500,
    "class_weight": "balanced",
    "random_state": 42,
}


class SignalModel:
    """
    ML 信号模型封装。

    以 sklearn Pipeline 为核心，包含：
    StandardScaler → 分类模型

    支持三种基模型（按效果优先级）：
    1. "lgbm"  - LightGBM（梯度提升树，高效高精度首选）
    2. "rf"    - RandomForest（鲁棒基线，无需超参调优）
    3. "lr"    - LogisticRegression（线性基线，快速验证特征有效性）

    Args:
        model_type:  "lgbm" | "rf" | "lr"
        params:      模型超参数（将合并进默认配置）
        label_type:  "binary"（0/1）| "multiclass"（-1/0/1）
    """

    def __init__(
        self,
        model_type: str = "lgbm",
        params: Optional[Dict[str, Any]] = None,
        label_type: str = "binary",
    ) -> None:
        if model_type not in {"lgbm", "rf", "lr"}:
            raise ValueError(f"model_type 必须是 lgbm/rf/lr，实际: {model_type}")

        self.model_type = model_type
        self.label_type = label_type
        self._pipeline: Optional[Pipeline] = None
        self._feature_names: Optional[list] = None
        self._classes: Optional[np.ndarray] = None

        # 合并默认超参数
        if model_type == "lgbm":
            merged = _LGBM_DEFAULT_PARAMS.copy()
            if label_type == "binary":
                merged["num_class"] = 2
                merged["objective"] = "binary"
                merged["metric"] = "binary_logloss"
            else:
                merged["num_class"] = 3
                merged["metric"] = "multi_logloss"
        elif model_type == "rf":
            merged = _RF_DEFAULT_PARAMS.copy()
        else:
            merged = _LR_DEFAULT_PARAMS.copy()

        if params:
            merged.update(params)
        self._params = merged

    # ────────────────────────────────────────────────────────────
    # 训练
    # ────────────────────────────────────────────────────────────

    def fit(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,
        X_val: Optional[pd.DataFrame] = None,
        y_val: Optional[pd.Series] = None,
    ) -> "SignalModel":
        """
        训练模型。

        Args:
            X_train:  训练特征矩阵（已 dropna）
            y_train:  训练标签序列（已 dropna，与 X_train 索引对齐）
            X_val:    验证集特征（LightGBM early stopping 使用）
            y_val:    验证集标签

        Returns:
            self（支持链式调用）
        """
        self._feature_names = list(X_train.columns)
        self._classes = np.sort(y_train.unique())

        log.info(
            "开始训练 {}: {} 样本 / {} 特征 / 标签分布={}",
            self.model_type,
            len(X_train),
            len(self._feature_names),
            y_train.value_counts().to_dict(),
        )

        base_model = self._build_base_model()

        # 构建 Pipeline（StandardScaler 对树模型无影响，但对 LR 必须）
        self._pipeline = Pipeline([
            ("scaler", StandardScaler()),
            ("model", base_model),
        ])

        # LightGBM 使用 fit_params 传递 early stopping 回调
        fit_params = {}
        if self.model_type == "lgbm" and _LGBM_AVAILABLE and X_val is not None:
            fit_params["model__eval_set"] = [(X_val.values, y_val.values)]
            fit_params["model__eval_metric"] = self._params.get("metric", "binary_logloss")
            # lgb 的 callbacks 参数
            try:
                fit_params["model__callbacks"] = [
                    lgb.early_stopping(stopping_rounds=30, verbose=False),
                    lgb.log_evaluation(period=-1),  # 静默
                ]
            except AttributeError:
                pass  # 旧版本 lgbm API 差异

        self._pipeline.fit(X_train, y_train, **fit_params)

        log.info("模型训练完成: {}", self.model_type)
        return self

    # ────────────────────────────────────────────────────────────
    # 推理
    # ────────────────────────────────────────────────────────────

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """
        预测类别标签。

        Args:
            X: 特征矩阵（列顺序必须与训练时一致）

        Returns:
            预测类别数组
        """
        self._check_fitted()
        X_aligned = self._align_features(X)
        return self._pipeline.predict(X_aligned)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """
        预测各类别概率。

        Returns:
            形状为 (n_samples, n_classes) 的概率数组
        """
        self._check_fitted()
        X_aligned = self._align_features(X)
        return self._pipeline.predict_proba(X_aligned)

    def predict_signal_proba(self, X: pd.DataFrame) -> np.ndarray:
        """
        预测"买入类"的概率（用于信号强弱排序）。

        Returns:
            买入类概率数组，值域 [0, 1]，越高信号越强
        """
        proba = self.predict_proba(X)
        # classes_ 中找 1（买入）的索引
        classes = list(self._pipeline.named_steps["model"].classes_)
        buy_idx = classes.index(1) if 1 in classes else -1
        if buy_idx == -1:
            return proba[:, -1]  # 取最后一类作为近似
        return proba[:, buy_idx]

    # ────────────────────────────────────────────────────────────
    # 模型解释
    # ────────────────────────────────────────────────────────────

    def get_feature_importance(self) -> pd.Series:
        """
        返回特征重要性（降序），值越大越重要。

        Returns:
            pd.Series（index=特征名，values=重要性分）

        Raises:
            RuntimeError: 模型未训练
        """
        self._check_fitted()
        model_step = self._pipeline.named_steps["model"]

        if hasattr(model_step, "feature_importances_"):
            importances = model_step.feature_importances_
        elif hasattr(model_step, "coef_"):
            importances = np.abs(model_step.coef_).mean(axis=0)
        else:
            return pd.Series(dtype=float)

        return pd.Series(
            importances,
            index=self._feature_names,
        ).sort_values(ascending=False)

    # ────────────────────────────────────────────────────────────
    # 持久化
    # ────────────────────────────────────────────────────────────

    def save(self, path: str | Path) -> Path:
        """
        保存整个 Pipeline（含归一化参数）到 pickle 文件。

        Args:
            path: 保存路径（.pkl 后缀）

        Returns:
            实际保存路径
        """
        self._check_fitted()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        model_data = {
            "pipeline": self._pipeline,
            "feature_names": self._feature_names,
            "model_type": self.model_type,
            "label_type": self.label_type,
            "params": self._params,
        }
        with open(path, "wb") as f:
            pickle.dump(model_data, f, protocol=pickle.HIGHEST_PROTOCOL)

        log.info("模型已保存: {} ({})", path, self.model_type)
        return path

    @classmethod
    def load(cls, path: str | Path) -> "SignalModel":
        """
        从 pickle 文件加载模型。

        Returns:
            已还原的 SignalModel 实例
        """
        path = Path(path)
        with open(path, "rb") as f:
            data = pickle.load(f)

        instance = cls(model_type=data["model_type"], label_type=data["label_type"])
        instance._pipeline = data["pipeline"]
        instance._feature_names = data["feature_names"]
        log.info("模型已加载: {} ({})", path, data["model_type"])
        return instance

    # ────────────────────────────────────────────────────────────
    # 私有方法
    # ────────────────────────────────────────────────────────────

    def _build_base_model(self):
        """构建基础分类器。"""
        if self.model_type == "lgbm" and _LGBM_AVAILABLE:
            return lgb.LGBMClassifier(**self._params)
        elif self.model_type == "lgbm" and not _LGBM_AVAILABLE:
            log.warning("LightGBM 未安装，降级为 RandomForest")
            return RandomForestClassifier(**_RF_DEFAULT_PARAMS)
        elif self.model_type == "rf":
            return RandomForestClassifier(**self._params)
        else:
            return LogisticRegression(**self._params)

    def _check_fitted(self) -> None:
        """检查模型是否已训练。"""
        if self._pipeline is None:
            raise RuntimeError("模型尚未训练，请先调用 fit()")

    def _align_features(self, X: pd.DataFrame) -> pd.DataFrame:
        """
        对齐推理时的特征列与训练时一致。

        处理特征列数量/顺序不一致的问题（防止特征对齐错误导致的静默错误）。
        """
        if self._feature_names is None:
            return X

        missing = set(self._feature_names) - set(X.columns)
        if missing:
            raise ValueError(
                f"推理特征缺少 {len(missing)} 列: {list(missing)[:5]}..."
            )

        extra = set(X.columns) - set(self._feature_names)
        if extra:
            log.debug("推理时发现额外特征列（已忽略）: {}", extra)

        return X[self._feature_names]
