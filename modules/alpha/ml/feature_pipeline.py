"""
modules/alpha/ml/feature_pipeline.py — 特征流水线：可组合的 Stage 编排器

设计说明：
- FeaturePipelineStage 协议：任何实现 fit/transform 的对象都可作为 stage
- FeaturePipeline 按注册顺序依次执行各 stage
- 记录每个 stage 的输入列数、输出列数、耗时
- 提供 fit_transform 一步完成

日志标签：[Pipeline] [PipelineStage]
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from core.logger import get_logger

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# Stage 协议
# ══════════════════════════════════════════════════════════════

@runtime_checkable
class FeaturePipelineStage(Protocol):
    """可组合 stage 的最小接口（sklearn-like）。"""

    def fit(self, X: pd.DataFrame) -> "FeaturePipelineStage": ...
    def transform(self, X: pd.DataFrame) -> pd.DataFrame: ...
    def diagnostics(self) -> dict[str, Any]: ...


# ══════════════════════════════════════════════════════════════
# 流水线
# ══════════════════════════════════════════════════════════════

@dataclass
class StageRecord:
    """单个 stage 的执行记录（用于诊断输出）。"""
    name: str
    input_cols: int
    output_cols: int
    elapsed_ms: float
    diagnostics: dict[str, Any] = field(default_factory=dict)


class FeaturePipeline:
    """
    可组合的特征处理流水线。

    使用方式：
        pipeline = FeaturePipeline()
        pipeline.add_stage("variance_filter", VarianceFilter())
        pipeline.add_stage("decorrelator", Decorrelator())

        X_out = pipeline.fit_transform(X_train)  # 训练时
        X_inf = pipeline.transform(X_infer)      # 推理时（使用 fit 的状态）

    Args:
        name: pipeline 名称（用于日志前缀）
    """

    def __init__(self, name: str = "default") -> None:
        self.name = name
        self._stages: list[tuple[str, FeaturePipelineStage]] = []
        self._stage_records: list[StageRecord] = []
        self._fitted = False

    def add_stage(self, name: str, stage: FeaturePipelineStage) -> "FeaturePipeline":
        """
        追加一个 stage。返回 self，支持链式调用。

        Args:
            name:  stage 名称（日志用）
            stage: 实现 fit/transform/diagnostics 的对象
        """
        self._stages.append((name, stage))
        return self

    def fit(self, X: pd.DataFrame) -> "FeaturePipeline":
        """依次 fit 每个 stage，最终 transform 后传给下一个 stage。"""
        self._stage_records = []
        current = X

        log.info(
            "[Pipeline:{}] fit开始: 输入行={} 列={}",
            self.name, len(current), len(current.columns),
        )

        for stage_name, stage in self._stages:
            t0 = time.perf_counter()
            cols_in = len(current.columns)

            stage.fit(current)
            current = stage.transform(current)

            elapsed_ms = (time.perf_counter() - t0) * 1000
            record = StageRecord(
                name=stage_name,
                input_cols=cols_in,
                output_cols=len(current.columns),
                elapsed_ms=elapsed_ms,
                diagnostics=stage.diagnostics(),
            )
            self._stage_records.append(record)

            log.debug(
                "[PipelineStage:{}:{}] 输入列={} → 输出列={} elapsed={:.1f}ms",
                self.name, stage_name, cols_in, len(current.columns), elapsed_ms,
            )

        self._fitted = True
        log.info(
            "[Pipeline:{}] fit完成: 最终列={}",
            self.name, len(current.columns),
        )
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        """使用 fit 阶段学到的参数做转换（推理期入口）。"""
        if not self._fitted:
            raise RuntimeError(
                f"FeaturePipeline({self.name}).transform() 必须先调用 fit()"
            )

        current = X
        for stage_name, stage in self._stages:
            cols_in = len(current.columns)
            current = stage.transform(current)
            log.debug(
                "[PipelineStage:{}:{}] transform 输入={} → 输出={}",
                self.name, stage_name, cols_in, len(current.columns),
            )

        return current

    def fit_transform(self, X: pd.DataFrame) -> pd.DataFrame:
        """fit + transform 一步完成（训练时常用入口）。"""
        self.fit(X)
        # fit() 内部已经做了全部 transform，但各 stage 是 stateful 的，
        # 我们重新走一遍 transform 以确保最终输出与 transform() 完全一致。
        result = X
        for _, stage in self._stages:
            result = stage.transform(result)
        return result

    # ────────────────────────────────────────────────────────────
    # 诊断
    # ────────────────────────────────────────────────────────────

    def diagnostics(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "stage_count": len(self._stages),
            "fitted": self._fitted,
            "stages": [
                {
                    "name": r.name,
                    "input_cols": r.input_cols,
                    "output_cols": r.output_cols,
                    "elapsed_ms": round(r.elapsed_ms, 2),
                    **r.diagnostics,
                }
                for r in self._stage_records
            ],
        }

    @property
    def stage_names(self) -> list[str]:
        return [name for name, _ in self._stages]
