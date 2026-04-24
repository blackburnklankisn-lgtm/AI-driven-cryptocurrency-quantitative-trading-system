"""
modules/evolution/report_builder.py — 演进报告构建器

设计说明：
- 根据本次演进周期的决策列表和候选快照，生成 EvolutionReport
- 报告内容：晋升/降级/淘汰/回滚列表、当前所有 ACTIVE 候选、摘要统计
- 支持 text summary 输出（供日志和告警使用）
- 不包含业务逻辑，只做数据聚合

日志标签：[Evolution]
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.alpha.contracts.evolution_types import (
    CandidateSnapshot,
    CandidateStatus,
    EvolutionReport,
    PromotionAction,
    PromotionDecision,
)

log = get_logger(__name__)


class ReportBuilder:
    """
    演进报告构建器。

    无状态，每次 build() 返回新的 EvolutionReport。
    """

    def build(
        self,
        period_start: datetime,
        decisions: list[PromotionDecision],
        active_snapshot: list[CandidateSnapshot],
        metadata: Optional[dict[str, Any]] = None,
    ) -> EvolutionReport:
        """
        构建演进报告。

        Args:
            period_start:    本次演进周期的开始时间
            decisions:       本次所有 PromotionDecision（PROMOTE/DEMOTE/RETIRE/ROLLBACK/HOLD）
            active_snapshot: 当前所有 ACTIVE 候选快照
            metadata:        附加调试信息

        Returns:
            EvolutionReport
        """
        now = datetime.now(tz=timezone.utc)
        report_id = f"rpt_{uuid.uuid4().hex[:8]}"

        promoted = [
            d.candidate_id for d in decisions
            if d.action == PromotionAction.PROMOTE.value
        ]
        demoted = [
            d.candidate_id for d in decisions
            if d.action == PromotionAction.DEMOTE.value
        ]
        retired = [
            d.candidate_id for d in decisions
            if d.action == PromotionAction.RETIRE.value
        ]
        rollbacks = [
            d.candidate_id for d in decisions
            if d.action == PromotionAction.ROLLBACK.value
        ]

        # 去掉 HOLD 决策（只在报告中保留实质性决策）
        significant = [
            d for d in decisions
            if d.action != PromotionAction.HOLD.value
        ]

        report = EvolutionReport(
            report_id=report_id,
            period_start=period_start,
            period_end=now,
            total_candidates=len(decisions),
            promoted=promoted,
            demoted=demoted,
            retired=retired,
            rollbacks=rollbacks,
            decisions=significant,
            active_snapshot=active_snapshot,
            metadata=metadata or {},
        )

        log.info("[Evolution] {}", report.summary())
        return report

    def text_summary(self, report: EvolutionReport) -> str:
        """
        生成人类可读的文字摘要（供告警/邮件/日志使用）。
        """
        lines = [
            f"=== Evolution Report {report.report_id} ===",
            f"Period: {report.period_start.isoformat()} → {report.period_end.isoformat()}",
            f"Evaluated: {report.total_candidates} candidates",
            f"Promoted:  {len(report.promoted)} {report.promoted}",
            f"Demoted:   {len(report.demoted)} {report.demoted}",
            f"Retired:   {len(report.retired)} {report.retired}",
            f"Rollbacks: {len(report.rollbacks)} {report.rollbacks}",
            "",
            "Active candidates:",
        ]
        if report.active_snapshot:
            for snap in report.active_snapshot:
                lines.append(f"  - {snap.summary()}")
        else:
            lines.append("  (none)")

        if report.decisions:
            lines.append("")
            lines.append("Significant decisions:")
            for d in report.decisions:
                lines.append(
                    f"  [{d.action}] {d.candidate_id} "
                    f"{d.from_status}→{d.to_status} "
                    f"reasons={d.reason_codes}"
                )

        return "\n".join(lines)
