"""
modules/alpha/contracts/evolution_types.py — 自进化闭环核心数据契约 (W22)

设计说明：
- CandidateType:      候选类型枚举（model / strategy / policy / params）
- CandidateStatus:    候选生命周期状态枚举（candidate → shadow → paper → active → ...）
- PromotionAction:    晋升/降级/回滚动作枚举
- CandidateSnapshot:  候选的完整状态快照（不可变）
- PromotionDecision:  演进决策输出（晋升/降级/回滚/暂停）
- EvolutionReport:    周期性演进报告摘要
- RetirementRecord:   淘汰记录（含原因、触发指标）

约束：
- 所有输出类型为 frozen dataclass（不可变）
- 不包含执行逻辑，只定义数据结构和枚举
- 与 PolicyStatus（rl_types.py）对齐，但独立定义以覆盖 model/strategy/params 类型

日志标签：[Evolution] [Promotion] [Retirement]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ══════════════════════════════════════════════════════════════
# 一、枚举
# ══════════════════════════════════════════════════════════════

class CandidateType(str, Enum):
    """
    候选种类。

    model:    ML 模型版本（由 ContinuousLearner / ModelRegistry 产出）
    strategy: 做市/方向策略（参数集变更，不改代码）
    policy:   RL policy 版本（由 PolicyStore 产出）
    params:   纯参数调优候选（如 Avellaneda gamma / risk aversion）
    """
    MODEL    = "model"
    STRATEGY = "strategy"
    POLICY   = "policy"
    PARAMS   = "params"


class CandidateStatus(str, Enum):
    """
    候选生命周期状态。

    流转路径（正向）：
        candidate → shadow → paper → active

    降级路径：
        active → paused → retired
        active/paused → rollback (触发自动回滚到上一 active 版本)
    """
    CANDIDATE = "candidate"   # 新产出，待 replay/OOS 评估
    SHADOW    = "shadow"      # OOS 通过，shadow 监控（不影响真实仓位）
    PAPER     = "paper"       # shadow 稳定，进入 paper A/B 验证
    ACTIVE    = "active"      # A/B 通过，作为主策略运行
    PAUSED    = "paused"      # 临时暂停（等待人工审核或市场异常）
    RETIRED   = "retired"     # 淘汰，不再使用


class PromotionAction(str, Enum):
    """
    演进决策动作。

    PROMOTE:  向前晋升一个状态（candidate→shadow / shadow→paper / paper→active）
    HOLD:     维持当前状态，继续观察
    DEMOTE:   降级一个状态（active→paused）
    RETIRE:   直接淘汰（retired）
    ROLLBACK: 回滚到上一个 active 版本，当前版本进入 paused
    """
    PROMOTE  = "PROMOTE"
    HOLD     = "HOLD"
    DEMOTE   = "DEMOTE"
    RETIRE   = "RETIRE"
    ROLLBACK = "ROLLBACK"


# ══════════════════════════════════════════════════════════════
# 二、候选快照
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CandidateSnapshot:
    """
    候选的完整状态快照（任一时间点的不可变视图）。

    Attributes:
        candidate_id:      唯一候选标识（如 "ppo_btc_20260423_01"）
        candidate_type:    CandidateType
        owner:             所属模块（如 "rl/ppo", "ml/rf", "market_making/avellaneda"）
        version:           版本字符串（与 PolicyRecord.version 或 ModelVersion.version_id 对齐）
        status:            当前生命周期状态
        sharpe_30d:        近 30 天 Sharpe（评估值，None = 尚未评估）
        max_drawdown_30d:  近 30 天最大回撤（0~1）
        win_rate_30d:      近 30 天胜率（0~1）
        ab_lift:           A/B 相对 control 的提升量（None = 未跑 A/B）
        created_at:        候选创建时间
        promoted_at:       最后一次晋升时间（None = 尚未晋升）
        metadata:          可选附加字段（如模型超参、训练数据范围）
    """

    candidate_id: str
    candidate_type: str            # CandidateType.value
    owner: str
    version: str
    status: str                    # CandidateStatus.value
    created_at: datetime
    sharpe_30d: Optional[float] = None
    max_drawdown_30d: Optional[float] = None
    win_rate_30d: Optional[float] = None
    ab_lift: Optional[float] = None
    promoted_at: Optional[datetime] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_active(self) -> bool:
        return self.status == CandidateStatus.ACTIVE.value

    def is_retired(self) -> bool:
        return self.status == CandidateStatus.RETIRED.value

    def passes_basic_gate(
        self,
        min_sharpe: float = 0.8,
        max_drawdown: float = 0.07,
    ) -> bool:
        """简易门禁检查（数据不足时返回 False）。"""
        if self.sharpe_30d is None or self.max_drawdown_30d is None:
            return False
        return (
            self.sharpe_30d >= min_sharpe
            and self.max_drawdown_30d <= max_drawdown
        )

    def summary(self) -> str:
        sharpe = f"{self.sharpe_30d:.3f}" if self.sharpe_30d is not None else "N/A"
        dd = f"{self.max_drawdown_30d:.3f}" if self.max_drawdown_30d is not None else "N/A"
        return (
            f"[{self.candidate_type}] {self.candidate_id} v{self.version} "
            f"status={self.status} sharpe={sharpe} maxdd={dd}"
        )


# ══════════════════════════════════════════════════════════════
# 三、演进决策
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class PromotionDecision:
    """
    针对单个候选的演进决策输出。

    Attributes:
        candidate_id:  被操作的候选 ID
        action:        PromotionAction 枚举
        from_status:   操作前状态
        to_status:     操作后状态（HOLD 时与 from_status 相同）
        reason_codes:  决策原因码列表（如 ["SHARPE_BELOW_GATE", "DRAWDOWN_EXCEEDED"]）
        effective_at:  决策生效时间
        metadata:      调试附加信息
    """

    candidate_id: str
    action: str                    # PromotionAction.value
    from_status: str               # CandidateStatus.value
    to_status: str                 # CandidateStatus.value
    reason_codes: list[str]
    effective_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_promotion(self) -> bool:
        return self.action == PromotionAction.PROMOTE.value

    def is_demotion(self) -> bool:
        return self.action in (PromotionAction.DEMOTE.value, PromotionAction.RETIRE.value)

    def is_rollback(self) -> bool:
        return self.action == PromotionAction.ROLLBACK.value


# ══════════════════════════════════════════════════════════════
# 四、演进报告
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class EvolutionReport:
    """
    单次演进调度周期的摘要报告。

    Attributes:
        report_id:          报告唯一 ID
        period_start:       本次调度周期开始时间
        period_end:         本次调度周期结束时间
        total_candidates:   参与评估的候选总数
        promoted:           晋升的候选列表（candidate_id）
        demoted:            降级的候选列表
        retired:            淘汰的候选列表
        rollbacks:          发生回滚的候选列表
        decisions:          所有决策列表
        active_snapshot:    当前所有 ACTIVE 候选的快照
        metadata:           附加调试信息
    """

    report_id: str
    period_start: datetime
    period_end: datetime
    total_candidates: int
    promoted: list[str]
    demoted: list[str]
    retired: list[str]
    rollbacks: list[str]
    decisions: list[PromotionDecision]
    active_snapshot: list[CandidateSnapshot]
    metadata: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        duration = (self.period_end - self.period_start).total_seconds()
        return (
            f"[Evolution] Report {self.report_id}: "
            f"total={self.total_candidates} "
            f"promoted={len(self.promoted)} "
            f"demoted={len(self.demoted)} "
            f"retired={len(self.retired)} "
            f"rollbacks={len(self.rollbacks)} "
            f"duration={duration:.1f}s"
        )


# ══════════════════════════════════════════════════════════════
# 五、淘汰记录
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RetirementRecord:
    """
    候选淘汰记录（可审计）。

    Attributes:
        candidate_id:      被淘汰候选 ID
        reason_codes:      淘汰原因码
        trigger_metrics:   触发淘汰的关键指标快照
        retired_at:        淘汰时间
        last_active_at:    最后一次 ACTIVE 状态时间（None = 从未 active）
        was_rolled_back:   是否触发了回滚（切回旧版本）
        rollback_to:       回滚到的版本 ID（None = 无可回滚版本）
    """

    candidate_id: str
    reason_codes: list[str]
    trigger_metrics: dict[str, Any]
    retired_at: datetime
    last_active_at: Optional[datetime] = None
    was_rolled_back: bool = False
    rollback_to: Optional[str] = None
