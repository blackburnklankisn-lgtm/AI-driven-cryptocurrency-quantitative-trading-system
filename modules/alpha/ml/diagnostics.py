"""
modules/alpha/ml/diagnostics.py — ML 诊断输出模块

设计说明：
- 为 WalkForwardResult、FeatureContract、CalibrationResult 提供统一的诊断摘要输出
- 支持打印到日志（[Diag] 标签）和序列化到 JSON 文件
- 不包含任何训练逻辑，只做观察/报告
- 可被 Orchestrator / DataKitchen / Predictor 在任何阶段调用

日志标签：[Diag]
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from core.logger import get_logger

log = get_logger(__name__)


class MLDiagnostics:
    """
    ML 诊断报告生成器。

    使用示例：
        diag = MLDiagnostics()

        # 诊断 WalkForwardResult
        diag.report_walk_forward(wf_result)

        # 诊断 FeatureContract
        diag.report_feature_contract(contract)

        # 诊断 CalibrationResult
        diag.report_calibration(cal_result)

        # 导出诊断报告到文件
        diag.save_report("./logs/ml_diag_20260423.json")
    """

    def __init__(self) -> None:
        self._reports: List[Dict[str, Any]] = []

    # ────────────────────────────────────────────────────────────
    # WalkForwardResult 诊断
    # ────────────────────────────────────────────────────────────

    def report_walk_forward(self, wf_result, tag: str = "") -> Dict[str, Any]:
        """
        生成 WalkForwardResult 的诊断摘要并打印到日志。

        Args:
            wf_result: WalkForwardResult 实例
            tag:       额外标识符（如模型类型）

        Returns:
            诊断字典
        """
        if not wf_result.fold_results:
            log.warning("[Diag] WalkForward 结果为空，无法生成诊断")
            return {}

        fold_metrics = [
            {
                "fold": r.fold_id,
                "train_size": r.train_size,
                "test_size": r.test_size,
                "accuracy": round(r.accuracy, 4),
                "f1": round(r.f1, 4),
                "auc": round(r.auc, 4) if not np.isnan(r.auc) else None,
                "optimal_threshold": round(r.optimal_threshold, 4),
            }
            for r in wf_result.fold_results
        ]

        avg = wf_result.avg_metrics()

        report = {
            "type": "walk_forward",
            "tag": tag,
            "n_folds": len(wf_result.fold_results),
            "avg_accuracy": round(avg.get("accuracy", 0), 4),
            "avg_f1": round(avg.get("f1", 0), 4),
            "avg_auc": round(
                float(np.mean([r.auc for r in wf_result.fold_results
                               if not np.isnan(r.auc)])),
                4,
            ),
            "optimal_buy_threshold": round(wf_result.optimal_buy_threshold, 4),
            "optimal_sell_threshold": round(wf_result.optimal_sell_threshold, 4),
            "feature_count": len(wf_result.feature_names),
            "fold_metrics": fold_metrics,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
        }

        log.info(
            "[Diag] WalkForward{}: n_folds={} avg_acc={:.4f} avg_f1={:.4f} "
            "avg_auc={:.4f} buy_threshold={:.4f} features={}",
            f"/{tag}" if tag else "",
            report["n_folds"], report["avg_accuracy"], report["avg_f1"],
            report["avg_auc"], report["optimal_buy_threshold"],
            report["feature_count"],
        )

        for fold in fold_metrics:
            log.debug(
                "[Diag] 折{}: train={} test={} acc={} f1={} auc={} threshold={}",
                fold["fold"], fold["train_size"], fold["test_size"],
                fold["accuracy"], fold["f1"], fold["auc"], fold["optimal_threshold"],
            )

        self._reports.append(report)
        return report

    # ────────────────────────────────────────────────────────────
    # FeatureContract 诊断
    # ────────────────────────────────────────────────────────────

    def report_feature_contract(self, contract, tag: str = "") -> Dict[str, Any]:
        """
        生成 FeatureContract 的诊断摘要。

        Args:
            contract: FeatureContract 实例
            tag:      额外标识符

        Returns:
            诊断字典
        """
        report = {
            "type": "feature_contract",
            "tag": tag,
            "version": contract.version,
            "signature": contract.signature,
            "alpha_feature_count": len(contract.alpha_features),
            "regime_feature_count": len(contract.regime_features),
            "diagnostic_feature_count": len(contract.diagnostic_features),
            "alpha_features_sample": contract.alpha_features[:10],
            "regime_features": contract.regime_features,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
        }

        log.info(
            "[Diag] FeatureContract{}: version={} signature={} "
            "alpha_features={} regime_features={} diag_features={}",
            f"/{tag}" if tag else "",
            contract.version, contract.signature,
            len(contract.alpha_features),
            len(contract.regime_features),
            len(contract.diagnostic_features),
        )

        self._reports.append(report)
        return report

    # ────────────────────────────────────────────────────────────
    # CalibrationResult 诊断
    # ────────────────────────────────────────────────────────────

    def report_calibration(self, cal_result, tag: str = "") -> Dict[str, Any]:
        """
        生成 CalibrationResult 的诊断摘要。

        Args:
            cal_result: CalibrationResult 实例
            tag:        额外标识符

        Returns:
            诊断字典
        """
        fold_details = [
            {
                "fold_id": ft.fold_id,
                "threshold": round(ft.optimal_buy_threshold, 4),
                "j_stat": round(ft.j_statistic, 4),
                "auc": round(ft.auc, 4),
            }
            for ft in cal_result.fold_thresholds
        ]

        thresholds_all = [ft.optimal_buy_threshold for ft in cal_result.fold_thresholds]
        threshold_std = float(np.std(thresholds_all)) if thresholds_all else 0.0

        report = {
            "type": "calibration",
            "tag": tag,
            "version": cal_result.version,
            "strategy": cal_result.aggregation_strategy,
            "recommended_buy": round(cal_result.recommended_buy_threshold, 4),
            "recommended_sell": round(cal_result.recommended_sell_threshold, 4),
            "buy_mean": round(cal_result.buy_threshold_mean, 4),
            "buy_median": round(cal_result.buy_threshold_median, 4),
            "buy_conservative": round(cal_result.buy_threshold_conservative, 4),
            "threshold_std": round(threshold_std, 4),
            "avg_auc": round(cal_result.avg_auc, 4),
            "avg_j_statistic": round(cal_result.avg_j_statistic, 4),
            "n_folds": len(cal_result.fold_thresholds),
            "fold_details": fold_details,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
        }

        log.info(
            "[Diag] Calibration{}: strategy={} recommended_buy={:.4f} "
            "recommended_sell={:.4f} avg_auc={:.4f} threshold_std={:.4f}",
            f"/{tag}" if tag else "",
            cal_result.aggregation_strategy,
            cal_result.recommended_buy_threshold,
            cal_result.recommended_sell_threshold,
            cal_result.avg_auc,
            threshold_std,
        )

        self._reports.append(report)
        return report

    # ────────────────────────────────────────────────────────────
    # OOS 概率分布诊断
    # ────────────────────────────────────────────────────────────

    def report_oos_probabilities(
        self,
        probabilities: pd.Series,
        threshold: float = 0.60,
        tag: str = "",
    ) -> Dict[str, Any]:
        """
        统计 OOS 买入概率分布（用于判断模型是否过于极端或保守）。

        Args:
            probabilities: OOS 买入概率序列（0~1）
            threshold:     当前 buy 阈值
            tag:           标识符

        Returns:
            诊断字典
        """
        p = probabilities.dropna()
        if len(p) == 0:
            return {}

        n_above = int((p >= threshold).sum())
        n_total = len(p)

        report = {
            "type": "oos_probabilities",
            "tag": tag,
            "n_total": n_total,
            "mean": round(float(p.mean()), 4),
            "std": round(float(p.std()), 4),
            "p25": round(float(p.quantile(0.25)), 4),
            "p50": round(float(p.quantile(0.50)), 4),
            "p75": round(float(p.quantile(0.75)), 4),
            "threshold": threshold,
            "n_above_threshold": n_above,
            "signal_rate": round(n_above / n_total, 4),
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
        }

        log.info(
            "[Diag] OOS概率分布{}: n={} mean={:.4f} std={:.4f} "
            "p50={:.4f} threshold={:.4f} signal_rate={:.2%}",
            f"/{tag}" if tag else "",
            n_total, report["mean"], report["std"],
            report["p50"], threshold, report["signal_rate"],
        )

        self._reports.append(report)
        return report

    # ────────────────────────────────────────────────────────────
    # 报告持久化
    # ────────────────────────────────────────────────────────────

    def save_report(self, path: str | Path) -> None:
        """将所有诊断报告序列化到 JSON 文件。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "report_count": len(self._reports),
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "reports": self._reports,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        log.info("[Diag] 诊断报告已保存: path={} reports={}", path, len(self._reports))

    def clear(self) -> None:
        """清空已收集的报告。"""
        self._reports.clear()

    @property
    def report_count(self) -> int:
        return len(self._reports)
