"""
modules/evolution/ab_test_manager.py — Paper/Shadow A/B 验证管理

设计说明：
- 管理 control vs test 的 A/B 对比实验
- 每个实验跟踪：control_id, test_id, 样本量, 累计指标（pnl, sharpe, drawdown）
- 实验完成后输出 ABResult（lift, significant, winner）
- 纯统计对比，不做资金划拨，不直接执行订单
- 线程安全（RLock）

日志标签：[ABTest] [Evolution]
"""

from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、实验配置与结果
# ══════════════════════════════════════════════════════════════

@dataclass
class ABExperimentConfig:
    """
    A/B 实验配置。

    Attributes:
        min_samples:       单侧最小样本量（control 和 test 都要达到）
        lift_threshold:    test 相对 control PnL 的最小提升阈值（例如 0.0 = 不能亏损）
        max_drawdown_diff: test 最大回撤相对 control 的容忍差值（例如 0.02 = 允许多 2%）
    """

    min_samples: int = 100
    lift_threshold: float = 0.0
    max_drawdown_diff: float = 0.02


@dataclass(frozen=True)
class ABResult:
    """
    A/B 实验结论。

    Attributes:
        experiment_id:   实验 ID
        control_id:      control 候选 ID
        test_id:         test 候选 ID
        control_pnl:     control 累计 PnL
        test_pnl:        test 累计 PnL
        lift:            PnL 提升量（test_pnl - control_pnl）
        control_samples: control 样本量
        test_samples:    test 样本量
        passes_gate:     是否通过门禁
        reason_codes:    未通过时的原因码
        decided_at:      判断时间
    """

    experiment_id: str
    control_id: str
    test_id: str
    control_pnl: float
    test_pnl: float
    lift: float
    control_samples: int
    test_samples: int
    control_max_drawdown: float
    test_max_drawdown: float
    passes_gate: bool
    reason_codes: list[str]
    decided_at: datetime

    def winner(self) -> str:
        """返回赢家 ID（通过门禁时返回 test_id，否则返回 control_id）。"""
        return self.test_id if self.passes_gate else self.control_id

    def summary(self) -> str:
        return (
            f"A/B {self.experiment_id}: "
            f"lift={self.lift:+.4f} "
            f"ctrl_dd={self.control_max_drawdown:.3f} "
            f"test_dd={self.test_max_drawdown:.3f} "
            f"passes={self.passes_gate} "
            f"reasons={self.reason_codes}"
        )


# ══════════════════════════════════════════════════════════════
# 二、内部实验状态（可变）
# ══════════════════════════════════════════════════════════════

@dataclass
class _ExperimentState:
    """单个进行中实验的可变状态。"""

    experiment_id: str
    control_id: str
    test_id: str
    config: ABExperimentConfig
    started_at: datetime

    control_pnl_sum: float = 0.0
    control_n: int = 0
    control_peak_pnl: float = 0.0
    control_max_drawdown: float = 0.0

    test_pnl_sum: float = 0.0
    test_n: int = 0
    test_peak_pnl: float = 0.0
    test_max_drawdown: float = 0.0

    def record_step(self, is_test: bool, step_pnl: float) -> None:
        if is_test:
            self.test_pnl_sum += step_pnl
            self.test_n += 1
            if self.test_pnl_sum > self.test_peak_pnl:
                self.test_peak_pnl = self.test_pnl_sum
            dd = (self.test_peak_pnl - self.test_pnl_sum) / max(abs(self.test_peak_pnl), 1e-8)
            if dd > self.test_max_drawdown:
                self.test_max_drawdown = dd
        else:
            self.control_pnl_sum += step_pnl
            self.control_n += 1
            if self.control_pnl_sum > self.control_peak_pnl:
                self.control_peak_pnl = self.control_pnl_sum
            dd = (self.control_peak_pnl - self.control_pnl_sum) / max(abs(self.control_peak_pnl), 1e-8)
            if dd > self.control_max_drawdown:
                self.control_max_drawdown = dd

    def has_sufficient_samples(self) -> bool:
        return (
            self.control_n >= self.config.min_samples
            and self.test_n >= self.config.min_samples
        )

    def evaluate(self) -> ABResult:
        lift = self.test_pnl_sum - self.control_pnl_sum
        reasons: list[str] = []
        passes = True

        if not self.has_sufficient_samples():
            reasons.append("INSUFFICIENT_SAMPLES")
            passes = False

        if lift < self.config.lift_threshold:
            reasons.append("LIFT_BELOW_THRESHOLD")
            passes = False

        drawdown_diff = self.test_max_drawdown - self.control_max_drawdown
        if drawdown_diff > self.config.max_drawdown_diff:
            reasons.append("DRAWDOWN_EXCEEDED")
            passes = False

        return ABResult(
            experiment_id=self.experiment_id,
            control_id=self.control_id,
            test_id=self.test_id,
            control_pnl=self.control_pnl_sum,
            test_pnl=self.test_pnl_sum,
            lift=lift,
            control_samples=self.control_n,
            test_samples=self.test_n,
            control_max_drawdown=self.control_max_drawdown,
            test_max_drawdown=self.test_max_drawdown,
            passes_gate=passes,
            reason_codes=reasons,
            decided_at=datetime.now(tz=timezone.utc),
        )


# ══════════════════════════════════════════════════════════════
# 三、ABTestManager 主体
# ══════════════════════════════════════════════════════════════

class ABTestManager:
    """
    A/B 实验管理器。

    - 支持同时并发多个实验
    - 每步喂入 control/test 的 step PnL
    - 样本量满足后可随时调用 evaluate()
    - 线程安全
    """

    def __init__(self, config: Optional[ABExperimentConfig] = None) -> None:
        self._config = config or ABExperimentConfig()
        self._lock = threading.RLock()
        self._experiments: dict[str, _ExperimentState] = {}
        self._completed: list[ABResult] = []

        log.info("[ABTest] ABTestManager 初始化: min_samples={} lift_threshold={}",
                 self._config.min_samples, self._config.lift_threshold)

    def create_experiment(
        self,
        control_id: str,
        test_id: str,
        experiment_id: Optional[str] = None,
        config: Optional[ABExperimentConfig] = None,
    ) -> str:
        """
        创建一个新 A/B 实验。

        Returns:
            experiment_id
        """
        eid = experiment_id or f"ab_{uuid.uuid4().hex[:8]}"
        cfg = config or self._config
        with self._lock:
            self._experiments[eid] = _ExperimentState(
                experiment_id=eid,
                control_id=control_id,
                test_id=test_id,
                config=cfg,
                started_at=datetime.now(tz=timezone.utc),
            )
        log.info("[ABTest] 实验创建: id={} control={} test={}",
                 eid, control_id, test_id)
        return eid

    def record_step(
        self,
        experiment_id: str,
        is_test: bool,
        step_pnl: float,
    ) -> None:
        """喂入单步 PnL 数据。"""
        with self._lock:
            exp = self._experiments.get(experiment_id)
            if exp is None:
                return
            exp.record_step(is_test=is_test, step_pnl=step_pnl)

    def evaluate(self, experiment_id: str) -> Optional[ABResult]:
        """
        评估实验结果。

        Returns:
            ABResult；实验不存在时返回 None。
        """
        with self._lock:
            exp = self._experiments.get(experiment_id)
            if exp is None:
                return None
            result = exp.evaluate()
            if result.passes_gate or result.control_samples + result.test_samples > 0:
                self._completed.append(result)

        log.info("[ABTest] 评估完成: {}", result.summary())
        return result

    def close_experiment(self, experiment_id: str) -> Optional[ABResult]:
        """评估并移除实验（强制完结）。"""
        result = self.evaluate(experiment_id)
        with self._lock:
            self._experiments.pop(experiment_id, None)
        return result

    def get_experiment_status(self, experiment_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            exp = self._experiments.get(experiment_id)
            if exp is None:
                return None
            return {
                "experiment_id": exp.experiment_id,
                "control_id": exp.control_id,
                "test_id": exp.test_id,
                "control_n": exp.control_n,
                "test_n": exp.test_n,
                "control_pnl": exp.control_pnl_sum,
                "test_pnl": exp.test_pnl_sum,
                "has_sufficient_samples": exp.has_sufficient_samples(),
                "started_at": exp.started_at.isoformat(),
            }

    def list_active_experiments(self) -> list[str]:
        with self._lock:
            return list(self._experiments.keys())

    def completed_results(self) -> list[ABResult]:
        with self._lock:
            return list(self._completed)

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active_experiments": len(self._experiments),
                "completed_experiments": len(self._completed),
                "experiment_ids": list(self._experiments.keys()),
            }
