"""
modules/evolution/self_evolution_engine.py — 自进化总协调器

设计说明：
- 统一协调候选注册、评估、A/B 验证、晋升、降级、淘汰、回滚
- 调用所有子模块（CandidateRegistry, PromotionGate, RetirementPolicy, ABTestManager,
  EvolutionScheduler, ReportBuilder, EvolutionStateStore）
- 不包含 ML 训练逻辑（ContinuousLearner 在外层调用）
- 不包含 live 执行逻辑（只管状态）
- 禁止绕过 paper/shadow 验证直接晋升到 active

生命周期方法：
    run_cycle()          — 执行一次完整演进周期（评估 → 晋升/降级/淘汰 → 报告）
    register_candidate() — 注册新候选（从 ContinuousLearner / PolicyStore 等产出）
    update_metrics()     — 更新候选评估指标
    force_promote()      — 手动强制晋升（仅限受控场景，会记录审计日志）
    force_retire()       — 手动强制淘汰
    diagnostics()        — 完整诊断快照

日志标签：[Evolution] [Promotion] [Retirement] [ABTest]
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.alpha.contracts.evolution_types import (
    CandidateStatus,
    CandidateType,
    CandidateSnapshot,
    PromotionAction,
    PromotionDecision,
)
from modules.evolution.ab_test_manager import ABExperimentConfig, ABTestManager
from modules.evolution.candidate_registry import CandidateRegistry
from modules.evolution.promotion_gate import PromotionGate, PromotionGateConfig
from modules.evolution.report_builder import ReportBuilder
from modules.evolution.retirement_policy import RetirementConfig, RetirementPolicy
from modules.evolution.scheduler import EvolutionScheduler, SchedulerConfig
from modules.evolution.state_store import EvolutionStateStore

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class SelfEvolutionConfig:
    """
    SelfEvolutionEngine 总配置。

    Attributes:
        state_dir:         持久化目录
        registry_path:     候选注册表路径（默认在 state_dir 下）
        auto_run:          是否在 update_metrics() 时自动触发 cycle（False = 需手动调 run_cycle()）
        scheduler:         调度器配置
        promotion_gate:    晋升门禁配置
        retirement:        淘汰规则配置
        ab_test:           A/B 实验配置
    """

    state_dir: str = "storage/evolution"
    registry_path: Optional[str] = None
    auto_run: bool = False
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    promotion_gate: PromotionGateConfig = field(default_factory=PromotionGateConfig)
    retirement: RetirementConfig = field(default_factory=RetirementConfig)
    ab_test: ABExperimentConfig = field(default_factory=ABExperimentConfig)


# ══════════════════════════════════════════════════════════════
# 二、SelfEvolutionEngine 主体
# ══════════════════════════════════════════════════════════════

class SelfEvolutionEngine:
    """
    自进化总协调器。

    负责：
    - 候选生命周期管理（注册 → 评估 → 晋升 → 降级/淘汰 → 回滚）
    - 调度 A/B 实验
    - 生成演进报告
    - 审计所有状态变更

    不直接：
    - 触发 ML 重训练
    - 发送 live 订单
    - 修改源代码
    """

    def __init__(self, config: Optional[SelfEvolutionConfig] = None) -> None:
        self.config = config or SelfEvolutionConfig()

        registry_path = (
            self.config.registry_path
            or f"{self.config.state_dir}/candidates.json"
        )

        self._state_store = EvolutionStateStore(self.config.state_dir)
        self._registry = CandidateRegistry(registry_path)
        self._promotion_gate = PromotionGate(self.config.promotion_gate)
        self._retirement_policy = RetirementPolicy(self.config.retirement)
        self._ab_manager = ABTestManager(self.config.ab_test)
        self._scheduler = EvolutionScheduler(self.config.scheduler, self._state_store)
        self._report_builder = ReportBuilder()

        # 运行时追踪：连续低 Sharpe 天数（candidate_id → days）
        self._consecutive_low_sharpe: dict[str, int] = {}
        # 运行时追踪：风险违规次数（candidate_id → count）
        self._risk_violations: dict[str, int] = {}
        # 运行时追踪：历史 active 版本（candidate_id → prev_version）
        self._prev_active: dict[str, str] = {}

        log.info("[Evolution] SelfEvolutionEngine 初始化: state_dir={}",
                 self.config.state_dir)

    # ─────────────────────────────────────────────
    # 对外接口：候选管理
    # ─────────────────────────────────────────────

    def register_candidate(
        self,
        candidate_type: CandidateType,
        owner: str,
        version: str,
        candidate_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> CandidateSnapshot:
        """
        注册新候选（初始状态为 CANDIDATE）。

        Args:
            candidate_type: 类型（model/strategy/policy/params）
            owner:          所属模块名称
            version:        版本字符串
            candidate_id:   显式 ID（None = 自动生成）
            metadata:       附加信息

        Returns:
            新候选的 CandidateSnapshot
        """
        snap = self._registry.register(
            candidate_type=candidate_type,
            owner=owner,
            version=version,
            candidate_id=candidate_id,
            metadata=metadata,
        )
        log.info("[Evolution] 新候选注册: id={} type={} owner={}",
                 snap.candidate_id, candidate_type.value, owner)
        return snap

    def update_metrics(
        self,
        candidate_id: str,
        sharpe_30d: Optional[float] = None,
        max_drawdown_30d: Optional[float] = None,
        win_rate_30d: Optional[float] = None,
        ab_lift: Optional[float] = None,
        risk_violations_delta: int = 0,
    ) -> Optional[CandidateSnapshot]:
        """
        更新候选评估指标。

        Args:
            candidate_id:          候选 ID
            sharpe_30d:            近 30 天 Sharpe
            max_drawdown_30d:      近 30 天最大回撤
            win_rate_30d:          近 30 天胜率
            ab_lift:               A/B lift
            risk_violations_delta: 本次新增的风险违规次数

        Returns:
            更新后的 CandidateSnapshot
        """
        if risk_violations_delta > 0:
            self._risk_violations[candidate_id] = (
                self._risk_violations.get(candidate_id, 0) + risk_violations_delta
            )

        snap = self._registry.update_metrics(
            candidate_id=candidate_id,
            sharpe_30d=sharpe_30d,
            max_drawdown_30d=max_drawdown_30d,
            win_rate_30d=win_rate_30d,
            ab_lift=ab_lift,
        )

        # 更新低 Sharpe 天数计数
        if snap and sharpe_30d is not None:
            cfg = self.config.retirement
            if sharpe_30d < cfg.demote_sharpe_threshold:
                self._consecutive_low_sharpe[candidate_id] = (
                    self._consecutive_low_sharpe.get(candidate_id, 0) + 1
                )
            else:
                self._consecutive_low_sharpe[candidate_id] = 0

        if self.config.auto_run:
            self.run_cycle()

        return snap

    def record_risk_violation(self, candidate_id: str, n: int = 1) -> None:
        """记录风险违规（独立接口，供风控模块回调）。"""
        self._risk_violations[candidate_id] = (
            self._risk_violations.get(candidate_id, 0) + n
        )
        log.warning("[Evolution] 风险违规记录: id={} cumulative={}",
                    candidate_id, self._risk_violations[candidate_id])

    # ─────────────────────────────────────────────
    # 对外接口：演进周期
    # ─────────────────────────────────────────────

    def run_cycle(self, force: bool = False) -> Optional[Any]:
        """
        执行一次完整演进周期。

        Returns:
            EvolutionReport；未到调度时间（且 force=False）时返回 None
        """
        if not self._scheduler.should_run(force=force):
            return None

        period_start = datetime.now(tz=timezone.utc)
        log.info("[Evolution] 演进周期开始: period_start={}", period_start.isoformat())

        all_decisions: list[PromotionDecision] = []
        promotions = 0
        retirements = 0

        try:
            # 1. 晋升评估（candidate / shadow / paper → 下一状态）
            promote_decisions = self._run_promotions()
            all_decisions.extend(promote_decisions)
            promotions = sum(
                1 for d in promote_decisions
                if d.action == PromotionAction.PROMOTE.value
            )

            # 2. 降级/淘汰评估（active → paused / retired）
            retire_decisions = self._run_retirements()
            all_decisions.extend(retire_decisions)
            retirements = sum(
                1 for d in retire_decisions
                if d.action in (PromotionAction.RETIRE.value, PromotionAction.ROLLBACK.value)
            )

            # 3. 持久化审计日志
            self._state_store.append_decisions(all_decisions)

            # 4. 构建报告
            active_snapshot = self._registry.list_active()
            report = self._report_builder.build(
                period_start=period_start,
                decisions=all_decisions,
                active_snapshot=active_snapshot,
            )
            self._state_store.save_report(report)

            # 5. 记录调度
            self._scheduler.record_run(
                success=True,
                candidates_evaluated=len(all_decisions),
                promotions=promotions,
                retirements=retirements,
            )

            log.info("[Evolution] 演进周期完成: {}", report.summary())
            return report

        except Exception as e:
            self._scheduler.record_run(success=False, error=str(e))
            log.exception("[Evolution] 演进周期异常: {}", e)
            return None

    # ─────────────────────────────────────────────
    # 对外接口：手动操作
    # ─────────────────────────────────────────────

    def force_promote(
        self,
        candidate_id: str,
        target_status: CandidateStatus,
        reason: str = "MANUAL_PROMOTE",
    ) -> Optional[CandidateSnapshot]:
        """
        手动强制晋升候选（不经过门禁，但会记录审计日志）。

        Returns:
            更新后的 CandidateSnapshot
        """
        snap = self._registry.get(candidate_id)
        if snap is None:
            log.warning("[Evolution] force_promote: 候选不存在: {}", candidate_id)
            return None

        updated = self._registry.transition(
            candidate_id=candidate_id,
            new_status=target_status,
            reason=reason,
        )
        if updated:
            decision = PromotionDecision(
                candidate_id=candidate_id,
                action=PromotionAction.PROMOTE.value,
                from_status=snap.status,
                to_status=target_status.value,
                reason_codes=[reason, "MANUAL_OVERRIDE"],
                effective_at=datetime.now(tz=timezone.utc),
            )
            self._state_store.append_decision(decision)
            log.info("[Evolution] 手动晋升: id={} {} → {} reason={}",
                     candidate_id, snap.status, target_status.value, reason)
        return updated

    def force_retire(
        self,
        candidate_id: str,
        reason: str = "MANUAL_RETIRE",
    ) -> Optional[CandidateSnapshot]:
        """手动强制淘汰候选（会记录审计日志）。"""
        snap = self._registry.get(candidate_id)
        if snap is None:
            return None

        updated = self._registry.transition(
            candidate_id=candidate_id,
            new_status=CandidateStatus.RETIRED,
            reason=reason,
        )
        if updated:
            decision = PromotionDecision(
                candidate_id=candidate_id,
                action=PromotionAction.RETIRE.value,
                from_status=snap.status,
                to_status=CandidateStatus.RETIRED.value,
                reason_codes=[reason, "MANUAL_OVERRIDE"],
                effective_at=datetime.now(tz=timezone.utc),
            )
            self._state_store.append_decision(decision)
        return updated

    # ─────────────────────────────────────────────
    # 对外接口：A/B 管理
    # ─────────────────────────────────────────────

    def create_ab_experiment(
        self,
        control_id: str,
        test_id: str,
        experiment_id: Optional[str] = None,
    ) -> str:
        return self._ab_manager.create_experiment(
            control_id=control_id,
            test_id=test_id,
            experiment_id=experiment_id,
        )

    def record_ab_step(
        self,
        experiment_id: str,
        is_test: bool,
        step_pnl: float,
    ) -> None:
        self._ab_manager.record_step(experiment_id, is_test=is_test, step_pnl=step_pnl)

    def conclude_ab_experiment(self, experiment_id: str) -> Optional[Any]:
        result = self._ab_manager.close_experiment(experiment_id)
        if result:
            # 更新 test 候选的 ab_lift
            self._registry.update_metrics(
                candidate_id=result.test_id,
                ab_lift=result.lift,
            )
            log.info("[ABTest] 实验结束: {}", result.summary())
        return result

    # ─────────────────────────────────────────────
    # 查询接口
    # ─────────────────────────────────────────────

    def get_candidate(self, candidate_id: str) -> Optional[CandidateSnapshot]:
        return self._registry.get(candidate_id)

    def list_active(self) -> list[CandidateSnapshot]:
        return self._registry.list_active()

    def list_by_status(
        self,
        status: CandidateStatus,
        candidate_type: Optional[CandidateType] = None,
    ) -> list[CandidateSnapshot]:
        return self._registry.list_by_status(status, candidate_type)

    def decision_history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._state_store.load_decisions(limit=limit)

    def latest_report(self) -> Optional[dict[str, Any]]:
        return self._state_store.load_report()

    def scheduler_should_run(self, force: bool = False) -> bool:
        return self._scheduler.should_run(force=force)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "registry": self._registry.diagnostics(),
            "state_store": self._state_store.diagnostics(),
            "scheduler": self._scheduler.diagnostics(),
            "ab_manager": self._ab_manager.diagnostics(),
            "risk_violations": dict(self._risk_violations),
            "consecutive_low_sharpe": dict(self._consecutive_low_sharpe),
        }

    # ─────────────────────────────────────────────
    # 内部：晋升周期
    # ─────────────────────────────────────────────

    def _run_promotions(self) -> list[PromotionDecision]:
        """评估所有可晋升候选（CANDIDATE / SHADOW / PAPER → 下一状态）。"""
        promote_statuses = [
            CandidateStatus.CANDIDATE,
            CandidateStatus.SHADOW,
            CandidateStatus.PAPER,
        ]
        candidates: list[CandidateSnapshot] = []
        for status in promote_statuses:
            candidates.extend(self._registry.list_by_status(status))

        decisions = self._promotion_gate.bulk_evaluate(
            snapshots=candidates,
            risk_violations_map=self._risk_violations,
        )

        for decision in decisions:
            if decision.is_promotion():
                target = CandidateStatus(decision.to_status)
                # 若晋升到 ACTIVE，记录上一版本
                if target == CandidateStatus.ACTIVE:
                    prev_active = self._registry.list_by_status(
                        CandidateStatus.ACTIVE
                    )
                    for prev in prev_active:
                        if prev.candidate_id != decision.candidate_id:
                            self._prev_active[decision.candidate_id] = prev.version

                self._registry.transition(
                    candidate_id=decision.candidate_id,
                    new_status=target,
                    reason="; ".join(decision.reason_codes),
                )

        return decisions

    # ─────────────────────────────────────────────
    # 内部：淘汰周期
    # ─────────────────────────────────────────────

    def _run_retirements(self) -> list[PromotionDecision]:
        """评估所有 ACTIVE 候选是否需要降级/淘汰。"""
        active = self._registry.list_active()
        decisions = self._retirement_policy.bulk_evaluate(
            snapshots=active,
            consecutive_days_map=self._consecutive_low_sharpe,
            risk_violations_map=self._risk_violations,
            has_prev_map={cid: True for cid in self._prev_active},
        )

        for decision in decisions:
            if decision.action != PromotionAction.HOLD.value:
                snap = self._registry.get(decision.candidate_id)
                target = CandidateStatus(decision.to_status)
                self._registry.transition(
                    candidate_id=decision.candidate_id,
                    new_status=target,
                    reason="; ".join(decision.reason_codes),
                )
                # 记录淘汰
                if decision.action in (
                    PromotionAction.RETIRE.value, PromotionAction.ROLLBACK.value
                ):
                    if snap:
                        record = self._retirement_policy.make_retirement_record(
                            snapshot=snap,
                            decision=decision,
                            rollback_to=self._prev_active.get(decision.candidate_id),
                        )
                        self._state_store.append_retirement(record)

        return decisions
