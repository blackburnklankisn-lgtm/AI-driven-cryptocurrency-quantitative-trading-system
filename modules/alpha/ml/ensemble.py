"""
modules/alpha/ml/ensemble.py — 多模型集成容器 (W8)

设计要点：
- `ModelEnsemble` 持有多个命名模型（duck-typed，只要有 predict_proba 方法）
- 每根 bar 调用 `predict(X)` → 产出 `List[ModelVote]`
- 模型缺席 / NaN 输出时跳过该模型并打 WARNING，不崩溃
- 权重在添加模型时指定，随 ModelVote 一并透传给 MetaLearner

接口：
    EnsembleConfig(buy_threshold, sell_threshold)
    ModelEnsemble(config)
        .add_model(name, model, weight) → self
        .predict(X)                     → list[ModelVote]
        .model_names()                  → list[str]
        .diagnostics()                  → dict

日志标签：[Ensemble]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Protocol, runtime_checkable

import numpy as np
import pandas as pd

from core.logger import get_logger
from modules.alpha.contracts.ensemble_types import ModelVote

log = get_logger(__name__)


# ─────────────────────────────────────────────────────────────
# Protocol — 只依赖 predict_proba，不硬绑 SignalModel
# ─────────────────────────────────────────────────────────────

@runtime_checkable
class ProbabilisticModel(Protocol):
    """任何具备 predict_proba 的模型均可接入。"""

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray: ...


# ─────────────────────────────────────────────────────────────
# EnsembleConfig
# ─────────────────────────────────────────────────────────────

@dataclass
class EnsembleConfig:
    """ModelEnsemble 全局配置。"""

    buy_threshold: float = 0.60
    sell_threshold: float = 0.40

    def __post_init__(self) -> None:
        if not (0.0 < self.sell_threshold < self.buy_threshold < 1.0):
            raise ValueError(
                f"阈值必须满足 0 < sell({self.sell_threshold}) "
                f"< buy({self.buy_threshold}) < 1"
            )


# ─────────────────────────────────────────────────────────────
# 内部记录：每个已注册模型的元数据
# ─────────────────────────────────────────────────────────────

@dataclass
class _ModelEntry:
    name: str
    model: Any          # ProbabilisticModel（duck-typed）
    weight: float = 1.0
    n_calls: int = 0
    n_errors: int = 0


# ─────────────────────────────────────────────────────────────
# ModelEnsemble
# ─────────────────────────────────────────────────────────────

class ModelEnsemble:
    """
    轻量多模型集成容器。

    使用示例：
        ensemble = (
            ModelEnsemble()
            .add_model("lgbm", trained_lgbm, weight=2.0)
            .add_model("rf",   trained_rf,   weight=1.0)
            .add_model("lr",   trained_lr,   weight=0.5)
        )
        votes = ensemble.predict(feature_df)
    """

    def __init__(self, config: EnsembleConfig | None = None) -> None:
        self._config = config or EnsembleConfig()
        self._entries: dict[str, _ModelEntry] = {}
        log.info(
            "[Ensemble] 初始化: buy_thr={} sell_thr={}",
            self._config.buy_threshold,
            self._config.sell_threshold,
        )

    # ── 注册 ──────────────────────────────────────────────────

    def add_model(
        self,
        name: str,
        model: Any,
        weight: float = 1.0,
    ) -> "ModelEnsemble":
        """注册模型（可链式调用）。

        Args:
            name:   唯一模型名称（lgbm / rf / lr 等）
            model:  具备 predict_proba(X) 方法的模型对象
            weight: 投票权重（传递给 MetaLearner）
        """
        if weight <= 0:
            raise ValueError(f"模型 {name} 的 weight 必须 > 0，实际: {weight}")
        if name in self._entries:
            log.warning("[Ensemble] 模型 {} 已存在，将被覆盖", name)
        self._entries[name] = _ModelEntry(name=name, model=model, weight=weight)
        log.info("[Ensemble] 注册模型: name={} weight={}", name, weight)
        return self

    # ── 推理 ──────────────────────────────────────────────────

    def predict(self, X: pd.DataFrame) -> List[ModelVote]:
        """对每个注册模型运行推理，返回 ModelVote 列表。

        - 模型缺席（entries 为空）→ 返回空列表，打 WARNING
        - 模型预测异常 / 输出 NaN → 跳过该模型，打 WARNING
        - 所有正常投票都保留在结果列表中

        Args:
            X:  特征矩阵（至少 1 行；推理时通常只传最新 1 行）

        Returns:
            list[ModelVote]，按注册顺序排列
        """
        if not self._entries:
            log.warning("[Ensemble] 没有注册任何模型，返回空投票列表")
            return []

        votes: List[ModelVote] = []

        for name, entry in self._entries.items():
            entry.n_calls += 1
            try:
                raw = entry.model.predict_proba(X)

                # 兼容 (n_samples,) 和 (n_samples, n_classes) 两种形状
                if isinstance(raw, np.ndarray):
                    if raw.ndim == 2:
                        # 二分类：列 1 为 buy 概率；多分类暂取列 1
                        buy_prob = float(raw[-1, 1])
                    else:
                        buy_prob = float(raw[-1])
                else:
                    # 万一返回 list / scalar
                    arr = np.asarray(raw, dtype=float).ravel()
                    buy_prob = float(arr[-1])

                if np.isnan(buy_prob) or np.isinf(buy_prob):
                    raise ValueError(f"模型输出无效概率: {buy_prob}")

                # 买卖中性判断
                if buy_prob >= self._config.buy_threshold:
                    action = "BUY"
                elif buy_prob <= self._config.sell_threshold:
                    action = "SELL"
                else:
                    action = "HOLD"

                vote = ModelVote(
                    model_name=name,
                    buy_probability=buy_prob,
                    action=action,
                    weight=entry.weight,
                    debug_payload={
                        "buy_threshold": self._config.buy_threshold,
                        "sell_threshold": self._config.sell_threshold,
                    },
                )
                votes.append(vote)
                log.debug(
                    "[Ensemble] {} → action={} buy_prob={:.4f} weight={}",
                    name, action, buy_prob, entry.weight,
                )

            except Exception as exc:
                entry.n_errors += 1
                log.warning(
                    "[Ensemble] 模型 {} 预测失败 ({})，跳过此模型",
                    name, exc,
                )

        if not votes:
            log.warning("[Ensemble] 所有模型均预测失败，返回空投票列表")

        return votes

    # ── 查询 ──────────────────────────────────────────────────

    def model_names(self) -> List[str]:
        return list(self._entries.keys())

    def diagnostics(self) -> dict:
        models_info = {
            name: {
                "weight": e.weight,
                "n_calls": e.n_calls,
                "n_errors": e.n_errors,
                "error_rate": e.n_errors / max(e.n_calls, 1),
            }
            for name, e in self._entries.items()
        }
        return {
            "n_models": len(self._entries),
            "buy_threshold": self._config.buy_threshold,
            "sell_threshold": self._config.sell_threshold,
            "models": models_info,
        }
