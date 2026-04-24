"""
modules/evolution/promotion_gate.py — 候选晋升门禁

设计说明：
- 定义候选在各阶段晋升的指标阈值（candidate→shadow / shadow→paper / paper→active）
- 对 CandidateSnapshot 执行多条件检查，返回 (passes, reason_codes)
- 不修改候选状态，只做只读判断
- 每个晋升阶段都有独立配置（GateConfig per transition）

日志标签：[Promotion]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.alpha.contracts.evolution_types import (
    CandidateSnapshot,
    CandidateStatus,
    PromotionAction,
    PromotionDecision,
)

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、门禁配置（分阶段）
# ══════════════════════════════════════════════════════════════

@dataclass
class StageGateConfig:
    """
    单个晋升阶段的门禁阈值。

    Attributes:
        min_sharpe:            最小 Sharpe 比率（None = 不检查）
        max_drawdown:          最大允许回撤（None = 不检查）
        min_win_rate:          最小胜率（None = 不检查）
        min_ab_lift:           A/B lift 最小值（None = 不检查）
        require_ab_completed:  是否强制要求 ab_lift 有值（即 A/B 已完成）
        max_risk_violations:   最大风险违规次数（None = 不检查）
    """

    min_sharpe: Optional[float] = None
    max_drawdown: Optional[float] = None
    min_win_rate: Optional[float] = None
    min_ab_lift: Optional[float] = None
    require_ab_completed: bool = False
    max_risk_violations: Optional[int] = None


@dataclass
class PromotionGateConfig:
    """
    三阶段晋升门禁总配置。

    根据文档建议：
    - candidate → shadow:  OOS Sharpe > 0.8, max_dd < 7%, 风险违规 = 0
    - shadow → paper:      7 天 shadow 行为稳定（此处用 sharpe + drawdown 代理）
    - paper → active:      A/B lift > 0, max_dd 不劣于基线
    """

    candidate_to_shadow: StageGateConfig = field(default_factory=lambda: StageGateConfig(
        min_sharpe=0.8,
        max_drawdown=0.07,
        max_risk_violations=0,
    ))
    shadow_to_paper: StageGateConfig = field(default_factory=lambda: StageGateConfig(
        min_sharpe=0.6,
        max_drawdown=0.09,
    ))
    paper_to_active: StageGateConfig = field(default_factory=lambda: StageGateConfig(
        min_sharpe=0.5,
        max_drawdown=0.10,
        min_ab_lift=0.0,
        require_ab_completed=True,
    ))


# ══════════════════════════════════════════════════════════════
# 二、PromotionGate 主体
# ══════════════════════════════════════════════════════════════

class PromotionGate:
    """
    候选晋升门禁。

    evaluate(snapshot) → PromotionDecision
    """

    def __init__(self, config: Optional[PromotionGateConfig] = None) -> None:
        self._config = config or PromotionGateConfig()
        log.info("[Promotion] PromotionGate 初始化")

    def evaluate(
        self,
        snapshot: CandidateSnapshot,
        risk_violations: int = 0,
    ) -> PromotionDecision:
        """
        评估候选是否可以晋升到下一状态。

        Args:
            snapshot:        候选当前快照
            risk_violations: 该候选累计风险违规次数

        Returns:
            PromotionDecision（PROMOTE / HOLD）
        """
        from_status = CandidateStatus(snapshot.status)
        gate_cfg, target_status = self._get_gate(from_status)

        if gate_cfg is None or target_status is None:
            # 已是 ACTIVE / PAUSED / RETIRED，不做正向晋升
            return self._hold(snapshot, ["ALREADY_TERMINAL_OR_ACTIVE"])

        passes, reasons = self._check(snapshot, gate_cfg, risk_violations)

        if passes:
            log.info("[Promotion] 候选通过门禁: id={} {} → {}",
                     snapshot.candidate_id, from_status.value, target_status.value)
            return PromotionDecision(
                candidate_id=snapshot.candidate_id,
                action=PromotionAction.PROMOTE.value,
                from_status=from_status.value,
                to_status=target_status.value,
                reason_codes=["GATE_PASSED"],
                effective_at=datetime.now(tz=timezone.utc),
            )
        else:
            log.debug("[Promotion] 候选未通过门禁: id={} reasons={}",
                      snapshot.candidate_id, reasons)
            return self._hold(snapshot, reasons)

    def _get_gate(
        self, from_status: CandidateStatus
    ) -> tuple[Optional[StageGateConfig], Optional[CandidateStatus]]:
        """根据当前状态返回对应门禁配置与目标状态。"""
        mapping: dict[CandidateStatus, tuple[StageGateConfig, CandidateStatus]] = {
            CandidateStatus.CANDIDATE: (
                self._config.candidate_to_shadow, CandidateStatus.SHADOW
            ),
            CandidateStatus.SHADOW: (
                self._config.shadow_to_paper, CandidateStatus.PAPER
            ),
            CandidateStatus.PAPER: (
                self._config.paper_to_active, CandidateStatus.ACTIVE
            ),
        }
        result = mapping.get(from_status)
        if result is None:
            return None, None
        return result

    def _check(
        self,
        snap: CandidateSnapshot,
        gate: StageGateConfig,
        risk_violations: int,
    ) -> tuple[bool, list[str]]:
        """执行所有门禁检查，返回 (passes, reason_codes)。"""
        reasons: list[str] = []

        if gate.min_sharpe is not None:
            if snap.sharpe_30d is None or snap.sharpe_30d < gate.min_sharpe:
                reasons.append(f"SHARPE_BELOW_GATE:{gate.min_sharpe}")

        if gate.max_drawdown is not None:
            if snap.max_drawdown_30d is None or snap.max_drawdown_30d > gate.max_drawdown:
                reasons.append(f"DRAWDOWN_EXCEEDED:{gate.max_drawdown}")

        if gate.min_win_rate is not None:
            if snap.win_rate_30d is None or snap.win_rate_30d < gate.min_win_rate:
                reasons.append(f"WIN_RATE_BELOW_GATE:{gate.min_win_rate}")

        if gate.require_ab_completed:
            if snap.ab_lift is None:
                reasons.append("AB_NOT_COMPLETED")
            elif gate.min_ab_lift is not None and snap.ab_lift < gate.min_ab_lift:
                reasons.append(f"AB_LIFT_BELOW_THRESHOLD:{gate.min_ab_lift}")

        if gate.max_risk_violations is not None:
            if risk_violations > gate.max_risk_violations:
                reasons.append(f"RISK_VIOLATIONS:{risk_violations}")

        return len(reasons) == 0, reasons

    @staticmethod
    def _hold(snap: CandidateSnapshot, reasons: list[str]) -> PromotionDecision:
        return PromotionDecision(
            candidate_id=snap.candidate_id,
            action=PromotionAction.HOLD.value,
            from_status=snap.status,
            to_status=snap.status,
            reason_codes=reasons,
            effective_at=datetime.now(tz=timezone.utc),
        )

    def bulk_evaluate(
        self,
        snapshots: list[CandidateSnapshot],
        risk_violations_map: Optional[dict[str, int]] = None,
    ) -> list[PromotionDecision]:
        """批量评估多个候选，返回决策列表（只含 PROMOTE 决策）。"""
        rvmap = risk_violations_map or {}
        decisions = []
        for snap in snapshots:
            rv = rvmap.get(snap.candidate_id, 0)
            decision = self.evaluate(snap, rv)
            if decision.is_promotion():
                decisions.append(decision)
        return decisions
