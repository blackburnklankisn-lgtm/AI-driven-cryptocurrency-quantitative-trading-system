"""
modules/alpha/ml/threshold_calibrator.py — OOS 阈值校准器

设计说明：
- 从 WalkForwardResult 的 OOS 概率分布中，计算每折最优 buy/sell 阈值
- 默认使用 Youden's J statistic (J = Sensitivity + Specificity - 1)
  使 TPR - FPR 最大化的阈值，是 ROC 曲线下最优工作点
- 支持多折聚合策略：mean / median / conservative（更严格）
- 输出结构化的 CalibrationResult，可序列化到磁盘
- Predictor 可在不改代码的情况下加载新阈值

Youden's J 公式：
  J = TPR - FPR = Sensitivity + Specificity - 1
  最优阈值 = argmax(TPR - FPR) over ROC curve

日志标签：[Threshold]
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd
from sklearn.metrics import roc_curve

from core.logger import get_logger

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 结果数据结构
# ══════════════════════════════════════════════════════════════

@dataclass
class FoldThreshold:
    """单折的阈值校准结果。"""
    fold_id: int
    optimal_buy_threshold: float    # Youden's J 最优 buy 阈值
    j_statistic: float              # 对应的 Youden's J 值
    auc: float                      # 该折 AUC
    n_positives: int                # 正样本数
    n_negatives: int                # 负样本数


@dataclass
class CalibrationResult:
    """跨折汇总校准结果（可序列化）。"""
    version: str                              # 版本标识，格式：cal_{yyyymmddTHHMMSS}
    fold_thresholds: List[FoldThreshold]

    # 聚合阈值（三种策略）
    buy_threshold_mean: float       # 各折均值
    buy_threshold_median: float     # 各折中位数
    buy_threshold_conservative: float  # 各折均值 + 0.5*std（更严格的过滤）

    sell_threshold_mean: float      # = 1 - buy_threshold_mean（对称）
    sell_threshold_median: float
    sell_threshold_conservative: float

    avg_auc: float
    avg_j_statistic: float

    # 推荐使用的阈值（由 aggregation_strategy 决定）
    recommended_buy_threshold: float
    recommended_sell_threshold: float
    aggregation_strategy: str       # "mean" | "median" | "conservative"

    created_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )

    def save(self, path: str | Path) -> None:
        """序列化保存到 JSON 文件。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": self.version,
            "buy_threshold_mean": self.buy_threshold_mean,
            "buy_threshold_median": self.buy_threshold_median,
            "buy_threshold_conservative": self.buy_threshold_conservative,
            "sell_threshold_mean": self.sell_threshold_mean,
            "sell_threshold_median": self.sell_threshold_median,
            "sell_threshold_conservative": self.sell_threshold_conservative,
            "avg_auc": self.avg_auc,
            "avg_j_statistic": self.avg_j_statistic,
            "recommended_buy_threshold": self.recommended_buy_threshold,
            "recommended_sell_threshold": self.recommended_sell_threshold,
            "aggregation_strategy": self.aggregation_strategy,
            "created_at": self.created_at,
            "fold_thresholds": [
                {
                    "fold_id": ft.fold_id,
                    "optimal_buy_threshold": ft.optimal_buy_threshold,
                    "j_statistic": ft.j_statistic,
                    "auc": ft.auc,
                    "n_positives": ft.n_positives,
                    "n_negatives": ft.n_negatives,
                }
                for ft in self.fold_thresholds
            ],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log.info(
            "[Threshold] 阈值结果已保存: path={} buy={:.4f} sell={:.4f} strategy={}",
            path, self.recommended_buy_threshold,
            self.recommended_sell_threshold, self.aggregation_strategy,
        )

    @classmethod
    def load(cls, path: str | Path) -> "CalibrationResult":
        """从 JSON 文件加载校准结果。"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        fold_thresholds = [
            FoldThreshold(**ft) for ft in data.pop("fold_thresholds", [])
        ]
        return cls(fold_thresholds=fold_thresholds, **data)


# ══════════════════════════════════════════════════════════════
# 校准器
# ══════════════════════════════════════════════════════════════

class ThresholdCalibrator:
    """
    OOS 阈值校准器：从 WalkForwardResult 中提取最优 buy/sell 阈值。

    使用示例：
        calibrator = ThresholdCalibrator(aggregation_strategy="median")
        result = calibrator.calibrate(walk_forward_result)
        result.save("./models/threshold_v1.json")

        # 加载并注入到 Predictor
        cal = CalibrationResult.load("./models/threshold_v1.json")
        predictor.set_thresholds(cal.recommended_buy_threshold,
                                  cal.recommended_sell_threshold)

    Args:
        aggregation_strategy: "mean" | "median" | "conservative"
    """

    def __init__(self, aggregation_strategy: str = "median") -> None:
        if aggregation_strategy not in ("mean", "median", "conservative"):
            raise ValueError(
                f"aggregation_strategy 必须是 mean/median/conservative, got {aggregation_strategy!r}"
            )
        self.aggregation_strategy = aggregation_strategy

    def calibrate_from_fold_results(
        self,
        fold_probabilities: list[pd.Series],
        fold_actuals: list[pd.Series],
        fold_aucs: list[float] | None = None,
    ) -> CalibrationResult:
        """
        直接从多折的 OOS 概率和真实标签序列校准阈值。

        Args:
            fold_probabilities: 每折的 buy 概率序列（pd.Series，值 0~1）
            fold_actuals:       每折的真实二分类标签（pd.Series，值 0/1）
            fold_aucs:          每折的 AUC 值（可选，无则从数据重算）

        Returns:
            CalibrationResult
        """
        if len(fold_probabilities) != len(fold_actuals):
            raise ValueError("fold_probabilities 和 fold_actuals 长度必须相同")
        if not fold_probabilities:
            raise ValueError("至少需要一折数据")

        fold_thresholds: List[FoldThreshold] = []

        for i, (proba, actual) in enumerate(zip(fold_probabilities, fold_actuals)):
            threshold, j_stat, auc = self._youden_j_threshold(proba, actual)
            if fold_aucs is not None:
                auc = fold_aucs[i]

            n_pos = int(actual.sum())
            n_neg = int(len(actual) - n_pos)

            fold_thresholds.append(FoldThreshold(
                fold_id=i,
                optimal_buy_threshold=threshold,
                j_statistic=j_stat,
                auc=auc,
                n_positives=n_pos,
                n_negatives=n_neg,
            ))

            log.info(
                "[Threshold] 折 {} 校准: optimal_threshold={:.4f} J={:.4f} "
                "auc={:.4f} pos={} neg={}",
                i, threshold, j_stat, auc, n_pos, n_neg,
            )

        return self._aggregate(fold_thresholds)

    def calibrate_from_wf_result(self, wf_result) -> CalibrationResult:
        """
        从 WalkForwardResult 对象校准（直接调用 fold_results）。

        Args:
            wf_result: WalkForwardResult 实例

        Returns:
            CalibrationResult
        """
        fold_probas = [fr.oos_probabilities for fr in wf_result.fold_results]
        fold_actuals = [fr.oos_actual for fr in wf_result.fold_results]
        fold_aucs = [fr.auc for fr in wf_result.fold_results]

        return self.calibrate_from_fold_results(fold_probas, fold_actuals, fold_aucs)

    # ────────────────────────────────────────────────────────────
    # 内部计算
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def _youden_j_threshold(
        proba: pd.Series,
        actual: pd.Series,
    ) -> tuple[float, float, float]:
        """
        计算 Youden's J 最优阈值。

        Returns:
            (optimal_threshold, j_statistic, auc)
        """
        y_true = actual.values.astype(int)
        y_score = proba.values.astype(float)

        n_unique_classes = len(np.unique(y_true))
        if n_unique_classes < 2:
            log.warning("[Threshold] 仅有单类标签，无法计算 ROC，使用默认阈值 0.5")
            return 0.5, 0.0, 0.5

        try:
            fpr, tpr, thresholds = roc_curve(y_true, y_score)

            # 计算 AUC（梯形积分）
            auc = float(np.trapz(tpr, fpr))
            auc = max(0.0, min(1.0, abs(auc)))  # 确保在 [0, 1]

            # Youden's J = TPR - FPR，最大化
            j_scores = tpr - fpr
            best_idx = int(np.argmax(j_scores))
            optimal_threshold = float(thresholds[best_idx])
            j_stat = float(j_scores[best_idx])

            # 阈值合理性检查（限制在 [0.4, 0.8] 范围内避免极端值）
            optimal_threshold = float(np.clip(optimal_threshold, 0.4, 0.8))

            return optimal_threshold, j_stat, auc

        except Exception:
            log.exception("[Threshold] ROC 计算失败，使用默认阈值 0.5")
            return 0.5, 0.0, 0.5

    def _aggregate(self, fold_thresholds: List[FoldThreshold]) -> CalibrationResult:
        """将多折校准结果聚合为最终阈值。"""
        buy_vals = [ft.optimal_buy_threshold for ft in fold_thresholds]
        aucs = [ft.auc for ft in fold_thresholds]
        j_vals = [ft.j_statistic for ft in fold_thresholds]

        buy_mean = float(np.mean(buy_vals))
        buy_median = float(np.median(buy_vals))
        buy_std = float(np.std(buy_vals))
        buy_conservative = float(np.clip(buy_mean + 0.5 * buy_std, 0.4, 0.85))

        strategy = self.aggregation_strategy
        if strategy == "mean":
            rec_buy = buy_mean
        elif strategy == "median":
            rec_buy = buy_median
        else:  # conservative
            rec_buy = buy_conservative

        rec_sell = float(np.clip(1.0 - rec_buy, 0.15, 0.60))

        version = f"cal_{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%S')}"

        result = CalibrationResult(
            version=version,
            fold_thresholds=fold_thresholds,
            buy_threshold_mean=buy_mean,
            buy_threshold_median=buy_median,
            buy_threshold_conservative=buy_conservative,
            sell_threshold_mean=float(np.clip(1.0 - buy_mean, 0.15, 0.60)),
            sell_threshold_median=float(np.clip(1.0 - buy_median, 0.15, 0.60)),
            sell_threshold_conservative=float(np.clip(1.0 - buy_conservative, 0.15, 0.60)),
            avg_auc=float(np.mean(aucs)),
            avg_j_statistic=float(np.mean(j_vals)),
            recommended_buy_threshold=rec_buy,
            recommended_sell_threshold=rec_sell,
            aggregation_strategy=strategy,
        )

        log.info(
            "[Threshold] 聚合完成: strategy={} buy_mean={:.4f} buy_median={:.4f} "
            "buy_conservative={:.4f} recommended_buy={:.4f} avg_auc={:.4f}",
            strategy, buy_mean, buy_median, buy_conservative,
            rec_buy, result.avg_auc,
        )

        return result
