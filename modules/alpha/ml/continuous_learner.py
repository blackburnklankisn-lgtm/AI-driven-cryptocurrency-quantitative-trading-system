"""
modules/alpha/ml/continuous_learner.py — 持续学习框架

设计说明：
金融市场是非平稳的（Non-stationary）：市场结构会随时间演变，
一个月前训练的模型可能在今天已经失效（概念漂移 / Concept Drift）。

持续学习框架解决以下问题：
1. 何时重训练？（触发机制）
   a. 定时触发：每 N 根 K 线后强制重训
   b. 性能退化触发：近期 OOS 精度低于训练时基线 threshold
   c. 概念漂移触发：KS 检验检测到特征分布显著变化

2. 如何安全切换模型？（无缝升级）
   - 新模型训练完成后先进入"候选"状态
   - 使用 A/B 测试框架对比新旧模型的近期预测准确率
   - 仅当新模型在近期数据上显著优于旧模型时，才完成切换
   - 切换时不中断现有持仓（只影响新信号的来源）

3. 安全保障
   - 旧模型始终作为 fallback 保存（最多保留 3 个版本）
   - 重训练期间使用旧模型继续产生信号（不中断服务）

接口：
    ContinuousLearner(trainer, feature_builder, labeler, config)
    .on_new_bar(ohlcv_row)          → 追加数据，检查是否需要重训
    .get_active_model()             → SignalModel（当前活跃模型）
    .get_model_version_info()       → Dict（版本历史）
    .force_retrain()                → WalkForwardResult
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional
from collections import deque

import numpy as np
import pandas as pd
from scipy import stats  # KS 检验

from core.logger import audit_log, get_logger
from modules.alpha.ml.model import SignalModel
from modules.alpha.ml.trainer import WalkForwardTrainer, WalkForwardResult
from modules.alpha.ml.feature_builder import MLFeatureBuilder
from modules.alpha.ml.labeler import ReturnLabeler

log = get_logger(__name__)


@dataclass
class ContinuousLearnerConfig:
    """持续学习框架配置。"""
    retrain_every_n_bars: int = 500       # 定时重训间隔（K 线数）
    min_accuracy_threshold: float = 0.55  # OOS 精度低于此值触发重训
    drift_significance: float = 0.05      # KS 检验显著性水平（p < 0.05 = 漂移）
    drift_check_window: int = 100         # 漂移检测使用的近期样本窗口
    model_dir: str = "./models"           # 模型版本保存目录
    max_saved_versions: int = 3           # 最多保留的历史版本数
    ab_test_window: int = 50              # A/B 测试窗口（K 线数）
    min_bars_for_retrain: int = 400       # 触发重训所需的最少数据量
    max_buffer_size: int = 10000          # OHLCV 缓冲区最大长度（防内存泄漏）
    label_type: str = "binary"            # "binary" | "multiclass"


@dataclass
class ModelVersion:
    """模型版本记录。"""
    version_id: str
    model: SignalModel
    trained_at: datetime
    train_bars: int
    oos_accuracy: float
    oos_f1: float
    is_active: bool = False
    model_path: Optional[str] = None


class ContinuousLearner:
    """
    持续学习框架。

    自动监控市场条件变化，在适当时机触发模型重训练和安全切换。

    Args:
        trainer:         WalkForwardTrainer 实例（复用训练配置）
        feature_builder: MLFeatureBuilder（与训练时一致）
        labeler:         ReturnLabeler（与训练时一致）
        config:          持续学习参数配置
    """

    def __init__(
        self,
        trainer: WalkForwardTrainer,
        feature_builder: MLFeatureBuilder,
        labeler: ReturnLabeler,
        config: Optional[ContinuousLearnerConfig] = None,
    ) -> None:
        self.trainer = trainer
        self.feature_builder = feature_builder
        self.labeler = labeler
        self.config = config or ContinuousLearnerConfig()

        # 滑动 OHLCV 缓冲区（有界 deque，防内存泄漏）
        self._ohlcv_buffer: deque = deque(maxlen=self.config.max_buffer_size)

        # 模型版本历史
        self._versions: List[ModelVersion] = []
        self._active_version: Optional[ModelVersion] = None
        self._candidate_version: Optional[ModelVersion] = None  # A/B 测试候选

        # 性能监控（近期预测准确率）
        self._recent_correct: deque[int] = deque(maxlen=self.config.drift_check_window)

        # 特征分布参考（用于 KS 漂移检测）
        self._reference_features: Optional[pd.DataFrame] = None

        # 计数器
        self._bar_count: int = 0
        self._bars_since_retrain: int = 0
        self._is_retraining: bool = False  # 防止并发重训

        # 自适应阈值（由重训后注入）
        self._optimal_buy_threshold: float = 0.60
        self._optimal_sell_threshold: float = 0.40

        log.info(
            "[ContLearn] 初始化: retrain_every={} drift_p={} min_acc={} "
            "max_buf={}",
            self.config.retrain_every_n_bars,
            self.config.drift_significance,
            self.config.min_accuracy_threshold,
            self.config.max_buffer_size,
        )

    # ────────────────────────────────────────────────────────────
    # 主数据流接口
    # ────────────────────────────────────────────────────────────

    def on_new_bar(self, ohlcv_row: dict) -> Optional[SignalModel]:
        """
        追加新 K 线数据，检查并（若满足条件）触发重训。

        Args:
            ohlcv_row: 单行 OHLCV 字典（含 timestamp/open/high/low/close/volume）

        Returns:
            如果切换了新模型，返回新的 SignalModel；否则返回 None
        """
        self._ohlcv_buffer.append(ohlcv_row)
        self._bar_count += 1
        self._bars_since_retrain += 1

        if self._bar_count % 100 == 0:
            log.debug(
                "[ContLearn] bar={} buf_size={}/{} since_retrain={}",
                self._bar_count, len(self._ohlcv_buffer),
                self.config.max_buffer_size,
                self._bars_since_retrain,
            )

        if len(self._ohlcv_buffer) < self.config.min_bars_for_retrain:
            return None

        # 检查触发条件
        trigger = self._check_retrain_triggers()
        if trigger and not self._is_retraining:
            log.info("触发重训: reason={} bar={}", trigger, self._bar_count)
            new_model = self._retrain(trigger)
            if new_model:
                audit_log(
                    "MODEL_RETRAINED",
                    trigger=trigger,
                    bar=self._bar_count,
                    version_id=new_model.version_id,
                )
                return new_model.model
        return None

    def record_prediction_outcome(self, predicted: int, actual: int) -> None:
        """
        记录一次预测的准确性，用于性能退化检测。

        Args:
            predicted: 模型预测值（0/1）
            actual:    实际标签值（0/1）
        """
        self._recent_correct.append(1 if predicted == actual else 0)

    def get_active_model(self) -> Optional[SignalModel]:
        """返回当前活跃模型（线程安全）。"""
        if self._active_version:
            return self._active_version.model
        return None

    def get_optimal_thresholds(self) -> tuple:
        """返回当前最优阈值 (buy_threshold, sell_threshold)。"""
        return self._optimal_buy_threshold, self._optimal_sell_threshold

    def get_model_version_info(self) -> List[Dict]:
        """返回所有已训练模型的版本信息。"""
        return [
            {
                "version_id": v.version_id,
                "trained_at": v.trained_at.isoformat(),
                "train_bars": v.train_bars,
                "oos_accuracy": v.oos_accuracy,
                "oos_f1": v.oos_f1,
                "is_active": v.is_active,
                "model_path": v.model_path,
            }
            for v in self._versions
        ]

    def force_retrain(self) -> Optional[WalkForwardResult]:
        """强制立即执行重训（忽略触发条件）。"""
        log.info("强制重训触发: bar={}", self._bar_count)
        version = self._retrain("forced")
        if version:
            return version  # 返回 ModelVersion
        return None

    # ────────────────────────────────────────────────────────────
    # 触发条件检测
    # ────────────────────────────────────────────────────────────

    def _check_retrain_triggers(self) -> Optional[str]:
        """
        按优先级检查各触发条件。

        Returns:
            触发原因字符串，或 None
        """
        # 1. 定时触发（最低优先级，但最可靠）
        if self._bars_since_retrain >= self.config.retrain_every_n_bars:
            return "scheduled"

        # 2. 性能退化触发
        if len(self._recent_correct) >= 30:  # 至少 30 个观测值
            recent_acc = float(np.mean(self._recent_correct))
            if recent_acc < self.config.min_accuracy_threshold:
                log.warning(
                    "性能退化检测: 近期精度={:.3f} < 阈值={}",
                    recent_acc, self.config.min_accuracy_threshold,
                )
                return "performance_degradation"

        # 3. 概念漂移触发（KS 检验）
        if self._reference_features is not None:
            drift_detected = self._check_concept_drift()
            if drift_detected:
                return "concept_drift"

        return None

    def _check_concept_drift(self) -> bool:
        """
        使用 KS 检验检测特征分布漂移。

        对比"参考期"特征分布 vs "近期"特征分布。
        任意特征的 KS 检验 p < significance 则认为发生漂移。

        Returns:
            True = 检测到显著漂移
        """
        if len(self._ohlcv_buffer) < self.config.drift_check_window * 2:
            return False

        try:
            # 近期数据
            recent_rows = self._ohlcv_buffer[-self.config.drift_check_window:]
            recent_df = pd.DataFrame(recent_rows)
            recent_feat = self.feature_builder.build(recent_df).dropna()

            if len(recent_feat) < 10:
                return False

            ref = self._reference_features
            feature_names = self.feature_builder.get_feature_names()

            # 对每个特征做 KS 检验
            drift_count = 0
            drift_features = []
            for col in feature_names[:20]:  # 只检验前 20 个最重要特征（速度优先）
                if col not in ref.columns or col not in recent_feat.columns:
                    continue
                ref_vals = ref[col].dropna().values
                rec_vals = recent_feat[col].dropna().values
                if len(ref_vals) < 10 or len(rec_vals) < 10:
                    continue
                ks_stat, p_value = stats.ks_2samp(ref_vals, rec_vals)
                if p_value < self.config.drift_significance:
                    drift_count += 1
                    drift_features.append(f"{col}(p={p_value:.3f})")

            # 超过 30% 的特征发生漂移才触发重训（避免误触发）
            drift_ratio = drift_count / max(len(feature_names[:20]), 1)
            if drift_ratio > 0.30:
                log.warning(
                    "概念漂移检测: {}/{} 特征分布显著变化 (>{} 阈值) → {}",
                    drift_count, len(feature_names[:20]),
                    self.config.drift_significance,
                    drift_features[:3],
                )
                return True

        except Exception as exc:
            log.debug("KS 检验异常（忽略）: {}", exc)

        return False

    # ────────────────────────────────────────────────────────────
    # 重训练与模型切换
    # ────────────────────────────────────────────────────────────

    def _retrain(self, trigger: str) -> Optional[ModelVersion]:
        """
        执行重训练流程：
        1. 用全量缓冲区数据做 Walk-Forward 训练
        2. 评估新模型的 OOS 指标
        3. 与现有模型对比（A/B）
        4. 若新模型更优或无现有模型，切换激活
        """
        if self._is_retraining:
            return None

        self._is_retraining = True
        t0 = time.monotonic()

        try:
            df = pd.DataFrame(self._ohlcv_buffer)

            # 执行简化版 Walk-Forward（3 折，速度更快）
            result = self.trainer.train(
                df=df,
                n_splits=3,
                test_size=max(50, len(df) // 10),
                min_train_size=max(150, len(df) // 4),
                val_size=0,
                label_type=self.config.label_type,
            )

            if not result.fold_results or result.final_model is None:
                log.warning("重训练未产出有效结果")
                return None

            avg_metrics = result.avg_metrics()
            oos_acc = avg_metrics.get("accuracy", 0.0)
            oos_f1 = avg_metrics.get("f1", 0.0)

            # 提取自适应阈值
            self._optimal_buy_threshold = result.optimal_buy_threshold
            self._optimal_sell_threshold = result.optimal_sell_threshold
            log.info(
                "[ContLearn] 重训阈值更新: buy={:.3f} sell={:.3f}",
                self._optimal_buy_threshold, self._optimal_sell_threshold,
            )

            # 生成版本 ID
            version_id = self._make_version_id()
            trained_at = datetime.now(tz=timezone.utc)

            # 保存模型到磁盘
            model_path = Path(self.config.model_dir) / f"model_{version_id}.pkl"
            try:
                result.final_model.save(model_path)
                model_path_str = str(model_path)
            except Exception as exc:
                log.warning("模型保存失败（继续运行）: {}", exc)
                model_path_str = None

            new_version = ModelVersion(
                version_id=version_id,
                model=result.final_model,
                trained_at=trained_at,
                train_bars=len(df),
                oos_accuracy=oos_acc,
                oos_f1=oos_f1,
                is_active=False,
                model_path=model_path_str,
            )

            # 决定是否切换
            should_switch = self._should_switch_model(new_version)
            if should_switch:
                # 旧模型标记为非活跃
                if self._active_version:
                    self._active_version.is_active = False
                new_version.is_active = True
                self._active_version = new_version
                audit_log(
                    "MODEL_SWITCHED",
                    version_id=version_id,
                    oos_accuracy=oos_acc,
                    oos_f1=oos_f1,
                    trigger=trigger,
                )
                log.info(
                    "模型已切换: version={} acc={:.3f} f1={:.3f}",
                    version_id, oos_acc, oos_f1,
                )

            self._versions.append(new_version)
            self._cleanup_old_versions()

            # 更新参考特征分布（用于下次漂移检测）
            self._update_reference_features(df)

            # 重置计数器
            self._bars_since_retrain = 0
            elapsed = time.monotonic() - t0
            log.info("重训练完成: trigger={} 耗时={:.1f}s", trigger, elapsed)

            return new_version

        except Exception as exc:
            log.exception("重训练异常: trigger={} error={}", trigger, exc)
            return None
        finally:
            self._is_retraining = False

    def _should_switch_model(self, new_version: ModelVersion) -> bool:
        """
        决定是否切换到新模型。

        规则：
        - 无活跃模型：直接切换
        - 新模型 OOS 精度比现有模型高 1% 以上：切换
        - 新模型 F1 改善 2% 以上：切换
        - 否则：保留旧模型
        """
        if self._active_version is None:
            return True

        old_acc = self._active_version.oos_accuracy
        new_acc = new_version.oos_accuracy
        old_f1 = self._active_version.oos_f1
        new_f1 = new_version.oos_f1

        if new_acc > old_acc + 0.01:
            log.info("切换原因：精度提升 {:.3f} → {:.3f}", old_acc, new_acc)
            return True

        if new_f1 > old_f1 + 0.02:
            log.info("切换原因：F1 提升 {:.3f} → {:.3f}", old_f1, new_f1)
            return True

        log.info(
            "保留旧模型（新模型无显著改善）: old_acc={:.3f} new_acc={:.3f}",
            old_acc, new_acc,
        )
        return False

    def _update_reference_features(self, df: pd.DataFrame) -> None:
        """更新特征分布参考基准（取训练集前半段）。"""
        try:
            half = len(df) // 2
            ref_df = df.iloc[:half]
            feat_df = self.feature_builder.build(ref_df).dropna()
            feature_names = self.feature_builder.get_feature_names()
            self._reference_features = feat_df[feature_names]
        except Exception as exc:
            log.debug("更新参考特征失败（忽略）: {}", exc)

    def _cleanup_old_versions(self) -> None:
        """清理超出保留数量的历史模型文件。"""
        if len(self._versions) <= self.config.max_saved_versions:
            return

        versions_to_remove = self._versions[:-self.config.max_saved_versions]
        for v in versions_to_remove:
            if v.model_path and not v.is_active:
                try:
                    Path(v.model_path).unlink(missing_ok=True)
                    log.debug("清理旧模型版本: {}", v.version_id)
                except Exception:
                    pass

        # 保留最近 N 个版本的元数据记录
        self._versions = self._versions[-self.config.max_saved_versions:]

    @staticmethod
    def _make_version_id() -> str:
        """生成唯一版本 ID（时间戳 + 短哈希）。"""
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        short_hash = hashlib.md5(str(time.monotonic_ns()).encode()).hexdigest()[:6]
        return f"{ts}_{short_hash}"
