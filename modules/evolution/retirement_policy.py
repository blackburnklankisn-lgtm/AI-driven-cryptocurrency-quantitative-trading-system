"""
modules/evolution/retirement_policy.py — 候选降权/暂停/淘汰规则

设计说明：
- 定义 ACTIVE → DEMOTE / PAUSE → RETIRE 的触发条件
- 检查：连续低 Sharpe 天数、重大回撤、风险违规次数、长期负 Sharpe
- 输出 PromotionDecision（DEMOTE / RETIRE / ROLLBACK / HOLD）
- 不修改候选状态，只做只读判断

根据文档规则：
- 连续 30 天 Sharpe < 0.5 → DEMOTE
- 连续 60 天 Sharpe < 0 或重大风险违规 → RETIRE / ROLLBACK

日志标签：[Retirement] [Evolution]
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
    RetirementRecord,
)

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class RetirementConfig:
    """
    淘汰规则配置。

    Attributes:
        demote_sharpe_threshold:      Sharpe 低于此值触发降级
        demote_consecutive_days:      连续低 Sharpe 天数触发降级
        retire_sharpe_threshold:      Sharpe 低于此值（更严苛）触发淘汰
        retire_consecutive_days:      连续严苛低 Sharpe 天数触发淘汰
        retire_risk_violations:       累计风险违规次数超过此值直接淘汰
        max_drawdown_retire:          回撤超过此值直接淘汰
        auto_rollback_on_retire:      淘汰时自动触发 ROLLBACK
    """

    demote_sharpe_threshold: float = 0.5
    demote_consecutive_days: int = 30
    retire_sharpe_threshold: float = 0.0
    retire_consecutive_days: int = 60
    retire_risk_violations: int = 5
    max_drawdown_retire: float = 0.15
    auto_rollback_on_retire: bool = True


# ══════════════════════════════════════════════════════════════
# 二、RetirementPolicy 主体
# ══════════════════════════════════════════════════════════════

class RetirementPolicy:
    """
    候选降级/淘汰规则执行器。

    evaluate(snapshot, ...) → PromotionDecision (DEMOTE / RETIRE / ROLLBACK / HOLD)
    """

    def __init__(self, config: Optional[RetirementConfig] = None) -> None:
        self._config = config or RetirementConfig()
        log.info("[Retirement] RetirementPolicy 初始化: config={}", self._config)

    def evaluate(
        self,
        snapshot: CandidateSnapshot,
        consecutive_low_sharpe_days: int = 0,
        risk_violations: int = 0,
        has_previous_version: bool = False,
    ) -> PromotionDecision:
        """
        评估 ACTIVE 候选是否应降级/淘汰。

        Args:
            snapshot:                   候选快照（需处于 ACTIVE 状态）
            consecutive_low_sharpe_days: 连续低 Sharpe 天数（由调度器追踪）
            risk_violations:             累计风险违规次数
            has_previous_version:        是否有可回滚的上一版本

        Returns:
            PromotionDecision (RETIRE / ROLLBACK / DEMOTE / HOLD)
        """
        reasons: list[str] = []
        action = PromotionAction.HOLD

        # 检查：立即淘汰条件
        if self._should_retire(snapshot, consecutive_low_sharpe_days, risk_violations):
            reasons = self._retirement_reasons(snapshot, consecutive_low_sharpe_days, risk_violations)
            if self._config.auto_rollback_on_retire and has_previous_version:
                action = PromotionAction.ROLLBACK
                reasons.append("AUTO_ROLLBACK")
            else:
                action = PromotionAction.RETIRE
            to_status = (
                CandidateStatus.PAUSED.value   # ROLLBACK 先 paused
                if action == PromotionAction.ROLLBACK
                else CandidateStatus.RETIRED.value
            )
        # 检查：降级条件
        elif self._should_demote(snapshot, consecutive_low_sharpe_days):
            reasons = [
                f"LOW_SHARPE_{consecutive_low_sharpe_days}d",
                f"SHARPE_BELOW:{self._config.demote_sharpe_threshold}",
            ]
            action = PromotionAction.DEMOTE
            to_status = CandidateStatus.PAUSED.value
        else:
            to_status = snapshot.status

        if action != PromotionAction.HOLD:
            log.info("[Retirement] 降级/淘汰决策: id={} action={} reasons={}",
                     snapshot.candidate_id, action.value, reasons)

        return PromotionDecision(
            candidate_id=snapshot.candidate_id,
            action=action.value,
            from_status=snapshot.status,
            to_status=to_status,
            reason_codes=reasons if reasons else ["PERFORMANCE_OK"],
            effective_at=datetime.now(tz=timezone.utc),
        )

    def _should_retire(
        self,
        snap: CandidateSnapshot,
        days: int,
        violations: int,
    ) -> bool:
        cfg = self._config
        # 连续严苛低 Sharpe
        if (snap.sharpe_30d is not None
                and snap.sharpe_30d < cfg.retire_sharpe_threshold
                and days >= cfg.retire_consecutive_days):
            return True
        # 风险违规过多
        if violations >= cfg.retire_risk_violations:
            return True
        # 回撤过大
        if (snap.max_drawdown_30d is not None
                and snap.max_drawdown_30d > cfg.max_drawdown_retire):
            return True
        return False

    def _should_demote(self, snap: CandidateSnapshot, days: int) -> bool:
        cfg = self._config
        return (
            snap.sharpe_30d is not None
            and snap.sharpe_30d < cfg.demote_sharpe_threshold
            and days >= cfg.demote_consecutive_days
        )

    def _retirement_reasons(
        self,
        snap: CandidateSnapshot,
        days: int,
        violations: int,
    ) -> list[str]:
        cfg = self._config
        reasons: list[str] = []
        if (snap.sharpe_30d is not None
                and snap.sharpe_30d < cfg.retire_sharpe_threshold
                and days >= cfg.retire_consecutive_days):
            reasons.append(f"SUSTAINED_NEG_SHARPE_{days}d")
        if violations >= cfg.retire_risk_violations:
            reasons.append(f"RISK_VIOLATIONS:{violations}")
        if (snap.max_drawdown_30d is not None
                and snap.max_drawdown_30d > cfg.max_drawdown_retire):
            reasons.append(f"MAX_DRAWDOWN_EXCEEDED:{snap.max_drawdown_30d:.3f}")
        return reasons

    def make_retirement_record(
        self,
        snapshot: CandidateSnapshot,
        decision: PromotionDecision,
        last_active_at: Optional[datetime] = None,
        rollback_to: Optional[str] = None,
    ) -> RetirementRecord:
        """生成可审计的淘汰记录。"""
        return RetirementRecord(
            candidate_id=snapshot.candidate_id,
            reason_codes=decision.reason_codes,
            trigger_metrics={
                "sharpe_30d": snapshot.sharpe_30d,
                "max_drawdown_30d": snapshot.max_drawdown_30d,
                "win_rate_30d": snapshot.win_rate_30d,
            },
            retired_at=decision.effective_at,
            last_active_at=last_active_at,
            was_rolled_back=decision.is_rollback(),
            rollback_to=rollback_to,
        )

    def bulk_evaluate(
        self,
        snapshots: list[CandidateSnapshot],
        consecutive_days_map: Optional[dict[str, int]] = None,
        risk_violations_map: Optional[dict[str, int]] = None,
        has_prev_map: Optional[dict[str, bool]] = None,
    ) -> list[PromotionDecision]:
        """批量评估多个 ACTIVE 候选，返回非 HOLD 决策列表。"""
        days_map = consecutive_days_map or {}
        rvmap = risk_violations_map or {}
        prev_map = has_prev_map or {}

        decisions = []
        for snap in snapshots:
            if snap.status != CandidateStatus.ACTIVE.value:
                continue
            decision = self.evaluate(
                snap,
                consecutive_low_sharpe_days=days_map.get(snap.candidate_id, 0),
                risk_violations=rvmap.get(snap.candidate_id, 0),
                has_previous_version=prev_map.get(snap.candidate_id, False),
            )
            if decision.action != PromotionAction.HOLD.value:
                decisions.append(decision)
        return decisions
