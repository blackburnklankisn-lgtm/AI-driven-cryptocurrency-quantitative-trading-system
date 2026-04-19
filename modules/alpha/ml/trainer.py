"""
modules/alpha/ml/trainer.py — Walk-Forward 验证训练框架

这是防时序泄露的核心模块。

Walk-Forward 验证原理：
┌─────────────────────────────────────────────────────────────────┐
│  时间轴 ──────────────────────────────────────────────────────► │
│                                                                 │
│  第 1 折：[=====TRAIN=====][EMB][==TEST==]                      │
│  第 2 折：[========TRAIN========][EMB][==TEST==]                │
│  第 3 折：[===========TRAIN===========][EMB][==TEST==]          │
│  ...                                                            │
│                                                                 │
│  注意：                                                          │
│  - EMB = Embargo（隔离期，长度 = forward_bars）                 │
│  - 每折测试集不重叠，且总是在训练集之后                         │
│  - 训练集可以逐步扩大（expanding window）或固定大小（rolling）  │
└─────────────────────────────────────────────────────────────────┘

关键设计决策：
- 强制 Embargo 期：训练集末尾的 E 行丢弃（因为其标签使用了测试集范围数据）
- 使用 Expanding Window（非 Rolling）：更多历史数据通常更好
- 每折独立训练一个模型，不跨折共享参数
- 汇报每折的 OOS（样本外）精度，而不是最后一折

接口：
    WalkForwardTrainer(feature_builder, labeler, model_config)
    .train(df, n_splits, test_size, min_train_size)
    → WalkForwardResult（含 OOS 预测序列、每折指标、最终模型）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
    roc_curve,
)

from core.exceptions import FutureLookAheadError
from core.logger import get_logger
from modules.alpha.ml.feature_builder import FeatureConfig, MLFeatureBuilder
from modules.alpha.ml.labeler import ReturnLabeler
from modules.alpha.ml.model import SignalModel

log = get_logger(__name__)


@dataclass
class FoldResult:
    """单折回测结果。"""
    fold_id: int
    train_size: int
    test_size: int
    train_start: Any
    train_end: Any
    test_start: Any
    test_end: Any
    accuracy: float
    f1: float
    precision: float
    recall: float
    auc: float
    oos_predictions: pd.Series         # 样本外预测（分类标签）
    oos_probabilities: pd.Series       # 样本外买入概率
    oos_actual: pd.Series              # 样本外真实标签
    optimal_threshold: float = 0.5      # Youden's J 最优阈值


@dataclass
class WalkForwardResult:
    """Walk-Forward 完整训练结果。"""
    fold_results: List[FoldResult] = field(default_factory=list)
    final_model: Optional[SignalModel] = None
    oos_predictions: Optional[pd.Series] = None     # 所有折拼接后的 OOS 预测
    oos_probabilities: Optional[pd.Series] = None
    feature_names: List[str] = field(default_factory=list)
    feature_importance_avg: Optional[pd.Series] = None  # 各折特征重要性平均值
    optimal_buy_threshold: float = 0.60   # 各折 Youden's J 最优阈值均值
    optimal_sell_threshold: float = 0.40  # 卖出阈值（= 1 - buy_threshold）
    selected_features: Optional[List[str]] = None  # SHAP 筛选后的特征子集

    def summary(self) -> pd.DataFrame:
        """返回各折指标汇总表。"""
        if not self.fold_results:
            return pd.DataFrame()
        rows = []
        for r in self.fold_results:
            rows.append({
                "fold": r.fold_id,
                "train_size": r.train_size,
                "test_size": r.test_size,
                "accuracy": round(r.accuracy, 4),
                "f1": round(r.f1, 4),
                "precision": round(r.precision, 4),
                "recall": round(r.recall, 4),
                "auc": round(r.auc, 4) if not np.isnan(r.auc) else "N/A",
            })
        return pd.DataFrame(rows)

    def avg_metrics(self) -> Dict[str, float]:
        """返回所有折的平均指标。"""
        if not self.fold_results:
            return {}
        metrics = ["accuracy", "f1", "precision", "recall"]
        return {
            m: float(np.mean([getattr(r, m) for r in self.fold_results]))
            for m in metrics
        }


class WalkForwardTrainer:
    """
    Walk-Forward 验证训练器。

    Args:
        feature_builder: MLFeatureBuilder 实例（用于构建特征矩阵）
        labeler:         ReturnLabeler 实例（用于生成标签）
        model_type:      基础分类器类型（"lgbm" | "rf" | "lr"）
        model_params:    额外的模型超参数
        expanding:       True = Expanding Window（默认），False = Rolling Window
    """

    def __init__(
        self,
        feature_builder: Optional[MLFeatureBuilder] = None,
        labeler: Optional[ReturnLabeler] = None,
        model_type: str = "rf",      # 默认 rf，避免 lgbm 未安装时的初始失败
        model_params: Optional[Dict[str, Any]] = None,
        expanding: bool = True,
        calibrate: bool = True,        # 是否对模型做概率校准
        feature_selection: bool = True, # 是否基于重要性筛选特征
        importance_threshold: float = 0.001,  # 特征重要性筛选阈值
    ) -> None:
        self.feature_builder = feature_builder or MLFeatureBuilder()
        self.labeler = labeler or ReturnLabeler()
        self.model_type = model_type
        self.model_params = model_params or {}
        self.expanding = expanding
        self.calibrate = calibrate
        self.feature_selection = feature_selection
        self.importance_threshold = importance_threshold

    def train(
        self,
        df: pd.DataFrame,
        n_splits: int = 5,
        test_size: int = 100,
        min_train_size: int = 200,
        val_size: int = 50,
        label_type: str = "binary",
    ) -> WalkForwardResult:
        """
        执行 Walk-Forward 验证训练。

        Args:
            df:            原始 OHLCV DataFrame（时间升序）
            n_splits:      折数（默认 5 折）
            test_size:     每折测试集大小（K 线条数）
            min_train_size:最小训练集大小（条数），保证训练样本充足
            val_size:      验证集大小（从训练集末尾切出，用于 early stopping）
            label_type:    "binary" | "multiclass"

        Returns:
            WalkForwardResult 对象（含各折指标和 OOS 预测序列）
        """
        log.info(
            "Walk-Forward 训练启动: n_splits={} test_size={} min_train={} model={} "
            "calibrate={} feat_selection={}",
            n_splits, test_size, min_train_size, self.model_type,
            self.calibrate, self.feature_selection,
        )

        # ── 1. 构建特征矩阵和标签 ────────────────────────────────
        log.info("构建特征矩阵...")
        feat_df = self.feature_builder.build(df)
        feature_names = self.feature_builder.get_feature_names()

        log.info("生成标签...")
        if label_type == "binary":
            labels = self.labeler.label_binary(df)
        else:
            labels = self.labeler.label_classification(df)

        # 合并特征和标签，对齐索引
        feat_df["_label"] = labels.values
        # 丢弃任何含 NaN 的行（预热期 + 标签末尾 NaN）
        clean_df = feat_df.dropna(subset=feature_names + ["_label"])

        log.info(
            "有效样本: {}/{} 行 ({:.1f}%)",
            len(clean_df),
            len(feat_df),
            100 * len(clean_df) / max(len(feat_df), 1),
        )

        if len(clean_df) < min_train_size + test_size:
            raise ValueError(
                f"有效样本不足！需要至少 {min_train_size + test_size} 行，"
                f"实际只有 {len(clean_df)} 行。请增加历史数据量。"
            )

        X = clean_df[feature_names]
        y = clean_df["_label"]
        embargo = self.labeler.forward_bars

        # ── 2. 生成 Walk-Forward 切分 ─────────────────────────────
        splits = self._generate_splits(
            n=len(clean_df),
            n_splits=n_splits,
            test_size=test_size,
            min_train_size=min_train_size,
            embargo=embargo,
        )

        if not splits:
            raise ValueError("数据量不足以生成任何有效切分，请增加数据量或减少折数。")

        log.info("成功生成 {} 个切分", len(splits))

        # ── 3. 逐折训练 ──────────────────────────────────────────
        result = WalkForwardResult(feature_names=feature_names)
        all_importance_series = []
        oos_preds_list = []
        oos_proba_list = []
        fold_thresholds = []  # 收集每折 Youden's J 阈值

        for fold_id, (train_idx, test_idx) in enumerate(splits, start=1):
            log.info(
                "=== Fold {}/{}: train[{}-{}] test[{}-{}] ===",
                fold_id, len(splits),
                train_idx[0], train_idx[-1],
                test_idx[0], test_idx[-1],
            )

            # 验证时序隔离（含 embargo 检查）
            try:
                self.labeler.check_no_leak(
                    pd.Index(train_idx),
                    pd.Index(test_idx),
                    embargo_bars=embargo,
                )
            except FutureLookAheadError as e:
                log.error("时序泄露检测失败（跳过本折）: {}", e)
                continue

            X_train = X.iloc[train_idx]
            y_train = y.iloc[train_idx]
            X_test = X.iloc[test_idx]
            y_test = y.iloc[test_idx]

            # 切出验证集（从训练集末尾）
            if val_size > 0 and len(X_train) > val_size + min_train_size // 2:
                X_val = X_train.iloc[-val_size:]
                y_val = y_train.iloc[-val_size:]
                X_train = X_train.iloc[:-val_size]
                y_train = y_train.iloc[:-val_size]
            else:
                X_val, y_val = None, None

            # 训练
            model = SignalModel(
                model_type=self.model_type,
                params=self.model_params,
                label_type=label_type,
                calibrate=self.calibrate,
            )
            model.fit(X_train, y_train, X_val, y_val)

            # 预测
            preds = model.predict(X_test)
            try:
                proba = model.predict_signal_proba(X_test)
            except Exception:  # noqa: BLE001
                proba = np.zeros(len(preds))

            # 计算指标
            fold_metrics = self._compute_metrics(y_test.values, preds, proba)

            # Youden's J 最优阈值
            opt_thresh = self._compute_optimal_threshold(y_test.values, proba)
            fold_thresholds.append(opt_thresh)

            log.info(
                "Fold {} OOS: acc={:.3f} f1={:.3f} auc={:.3f} optimal_thresh={:.3f}",
                fold_id, fold_metrics["accuracy"], fold_metrics["f1"],
                fold_metrics["auc"], opt_thresh,
            )

            fold_result = FoldResult(
                fold_id=fold_id,
                train_size=len(X_train),
                test_size=len(X_test),
                train_start=clean_df.index[train_idx[0]],
                train_end=clean_df.index[train_idx[-1]],
                test_start=clean_df.index[test_idx[0]],
                test_end=clean_df.index[test_idx[-1]],
                oos_predictions=pd.Series(preds, index=X_test.index),
                oos_probabilities=pd.Series(proba, index=X_test.index),
                oos_actual=y_test,
                optimal_threshold=opt_thresh,
                **fold_metrics,
            )
            result.fold_results.append(fold_result)

            oos_preds_list.append(fold_result.oos_predictions)
            oos_proba_list.append(fold_result.oos_probabilities)

            # 特征重要性
            imp = model.get_feature_importance()
            if len(imp) > 0:
                all_importance_series.append(imp)

            # 保存最后一折的模型作为 final_model（用于实盘推理）
            result.final_model = model

        # ── 4. 汇总结果 ──────────────────────────────────────────
        if oos_preds_list:
            result.oos_predictions = pd.concat(oos_preds_list).sort_index()
            result.oos_probabilities = pd.concat(oos_proba_list).sort_index()

        if all_importance_series:
            # 多折特征重要性取均值（更稳定）
            imp_df = pd.concat(all_importance_series, axis=1)
            result.feature_importance_avg = imp_df.mean(axis=1).sort_values(ascending=False)

            # P2: 基于重要性的特征筛选
            if self.feature_selection and result.feature_importance_avg is not None:
                selected = result.feature_importance_avg[
                    result.feature_importance_avg > self.importance_threshold
                ].index.tolist()
                if len(selected) >= 10:  # 至少保留 10 个特征
                    result.selected_features = selected
                    dropped = len(feature_names) - len(selected)
                    log.info(
                        "[FeatSelect] 特征筛选: {} → {} (剔除 {} 个低重要性特征, "
                        "threshold={})",
                        len(feature_names), len(selected), dropped,
                        self.importance_threshold,
                    )
                else:
                    log.debug(
                        "[FeatSelect] 筛选后只剩 {} 个特征（< 10），保留全部",
                        len(selected),
                    )

        # ── 自适应阈值（各折 Youden's J 的中位数）────────────────
        if fold_thresholds:
            result.optimal_buy_threshold = float(np.median(fold_thresholds))
            result.optimal_sell_threshold = max(
                0.30, 1.0 - result.optimal_buy_threshold
            )  # 卖出阈值 = 1 - 买入阈值，下限 0.30
            log.info(
                "[Threshold] 自适应阈值: buy={:.3f} sell={:.3f} "
                "(各折: {})",
                result.optimal_buy_threshold,
                result.optimal_sell_threshold,
                ["{:.3f}".format(t) for t in fold_thresholds],
            )

        # 打印汇总
        summary = result.summary()
        avg = result.avg_metrics()
        log.info("\n{}", summary.to_string(index=False))
        log.info(
            "平均 OOS 指标: acc={:.3f} f1={:.3f} precision={:.3f} recall={:.3f}",
            avg.get("accuracy", 0),
            avg.get("f1", 0),
            avg.get("precision", 0),
            avg.get("recall", 0),
        )

        return result

    # ────────────────────────────────────────────────────────────
    # 私有工具方法
    # ────────────────────────────────────────────────────────────

    def _generate_splits(
        self,
        n: int,
        n_splits: int,
        test_size: int,
        min_train_size: int,
        embargo: int,
    ) -> List[Tuple[np.ndarray, np.ndarray]]:
        """
        生成 Walk-Forward 切分索引。

        时间轴结构（每折）：
        [  TRAIN  ][EMBARGO][TEST]

        Returns:
            [(train_indices, test_indices), ...] 列表
        """
        splits = []
        total_needed = min_train_size + embargo + test_size

        if n < total_needed:
            return splits

        # 测试集起始位置：从 min_train_size + embargo 开始，步进 test_size
        test_starts = np.linspace(
            min_train_size + embargo,
            n - test_size,
            n_splits + 1,
            dtype=int,
        )

        for i in range(len(test_starts) - 1):
            test_start = int(test_starts[i])
            test_end = min(test_start + test_size, n)

            if self.expanding:
                train_start = 0
            else:
                # Rolling window：固定长度 min_train_size
                train_start = max(0, test_start - embargo - min_train_size)

            train_end = test_start - embargo  # 跳过隔离期

            if train_end - train_start < min_train_size // 2:
                log.debug("训练集太小，跳过折 {}", i + 1)
                continue

            train_idx = np.arange(train_start, train_end)
            test_idx = np.arange(test_start, test_end)

            splits.append((train_idx, test_idx))

        return splits

    @staticmethod
    def _compute_metrics(
        y_true: np.ndarray,
        y_pred: np.ndarray,
        y_proba: np.ndarray,
    ) -> Dict[str, float]:
        """计算分类指标。"""
        accuracy = float(accuracy_score(y_true, y_pred))

        unique_classes = np.unique(y_true)
        is_binary = len(unique_classes) <= 2

        if is_binary:
            avg = "binary"
            zero_div = 0
        else:
            avg = "weighted"
            zero_div = 0

        f1 = float(f1_score(y_true, y_pred, average=avg, zero_division=zero_div))
        precision = float(precision_score(y_true, y_pred, average=avg, zero_division=zero_div))
        recall = float(recall_score(y_true, y_pred, average=avg, zero_division=zero_div))

        # AUC（只有二分类且有概率时才计算）
        try:
            if is_binary and len(y_proba) == len(y_true):
                auc = float(roc_auc_score(y_true, y_proba))
            else:
                auc = float("nan")
        except Exception:  # noqa: BLE001
            auc = float("nan")

        return {
            "accuracy": accuracy,
            "f1": f1,
            "precision": precision,
            "recall": recall,
            "auc": auc,
        }

    @staticmethod
    def _compute_optimal_threshold(
        y_true: np.ndarray,
        y_proba: np.ndarray,
    ) -> float:
        """
        用 Youden's J 统计量找最优分类阈值。

        J = Sensitivity + Specificity - 1 = TPR - FPR
        threshold* = argmax_t J(t)

        Returns:
            最优阈值 (0, 1)，若无法计算返回 0.50
        """
        try:
            unique_classes = np.unique(y_true)
            if len(unique_classes) > 2 or len(y_proba) == 0:
                return 0.50

            fpr, tpr, thresholds = roc_curve(y_true, y_proba)
            j_scores = tpr - fpr
            best_idx = int(np.argmax(j_scores))
            optimal = float(thresholds[best_idx])

            # 限制在合理范围 [0.35, 0.80]
            optimal = max(0.35, min(0.80, optimal))

            log.debug(
                "[YoudenJ] best_J={:.3f} threshold={:.3f} (raw_idx={}/{})",
                j_scores[best_idx], optimal, best_idx, len(thresholds),
            )
            return optimal
        except Exception as exc:
            log.debug("[YoudenJ] 计算失败（返回默认 0.50）: {}", exc)
            return 0.50
