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
from modules.monitoring.trace import generate_trace_id, get_recorder
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


class ManualRollbackError(RuntimeError):
    """可结构化识别的手动回滚异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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
    weekly_params_optimizer_cron: str = ""
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
        # 运行时追踪：历史 active 候选（candidate_id → previous_active_candidate_id）
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

        _trace_id = generate_trace_id("ev")
        period_start = datetime.now(tz=timezone.utc)
        log.info(
            "[Evolution] 演进周期开始: trace_id={} period_start={}",
            _trace_id, period_start.isoformat(),
        )
        get_recorder().record(_trace_id, "ev", "CYCLE_START", {
            "period_start": period_start.isoformat(),
            "force": force,
        })

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

            log.info(
                "[Evolution] 演进周期完成: trace_id={} {}",
                _trace_id, report.summary(),
            )
            get_recorder().record(_trace_id, "ev", "CYCLE_END", {
                "period_start": period_start.isoformat(),
                "promotions": promotions,
                "retirements": retirements,
                "decisions": len(all_decisions),
            })
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

    def manual_rollback(
        self,
        *,
        family_key: Optional[str] = None,
        current_candidate_id: Optional[str] = None,
        rollback_to_candidate_id: Optional[str] = None,
        reason: str = "MANUAL_ROLLBACK",
    ) -> Optional[Any]:
        """按 family 或 candidate 精确执行一次手动回滚。"""
        current = self._select_manual_current_candidate(
            family_key=family_key,
            current_candidate_id=current_candidate_id,
        )

        previous = self._resolve_manual_rollback_target(
            current,
            rollback_to_candidate_id=rollback_to_candidate_id,
        )

        effective_at = datetime.now(tz=timezone.utc)
        self._registry.transition(
            candidate_id=current.candidate_id,
            new_status=CandidateStatus.PAUSED,
            reason=reason,
        )
        self._registry.transition(
            candidate_id=previous.candidate_id,
            new_status=CandidateStatus.ACTIVE,
            reason=f"ROLLBACK_FROM:{current.candidate_id}",
        )

        self._prev_active[current.candidate_id] = previous.candidate_id

        decision = PromotionDecision(
            candidate_id=current.candidate_id,
            action=PromotionAction.ROLLBACK.value,
            from_status=current.status,
            to_status=CandidateStatus.PAUSED.value,
            reason_codes=[reason, "MANUAL_OVERRIDE"],
            effective_at=effective_at,
            metadata={
                "rollback_to": previous.candidate_id,
                "family_key": self._candidate_family_key(current),
            },
        )
        self._state_store.append_decision(decision)

        record = self._retirement_policy.make_retirement_record(
            snapshot=current,
            decision=decision,
            rollback_to=previous.candidate_id,
        )
        self._state_store.append_retirement(record)

        report = self._report_builder.build(
            period_start=effective_at,
            decisions=[decision],
            active_snapshot=self._registry.list_active(),
            metadata={
                "manual": True,
                "reason": reason,
                "family_key": self._candidate_family_key(current),
                "rollback_from": current.candidate_id,
                "rollback_to": previous.candidate_id,
            },
        )
        self._state_store.save_report(report)
        log.info(
            "[Evolution] 手动回滚完成: current={} rollback_to={} reason={} family={}",
            current.candidate_id,
            previous.candidate_id,
            reason,
            self._candidate_family_key(current),
        )
        return report

    def manual_rollback_latest(
        self,
        reason: str = "MANUAL_ROLLBACK",
    ) -> Optional[Any]:
        """回滚最近一个存在可恢复上一版本的 active 候选。"""
        try:
            return self.manual_rollback(reason=reason)
        except ManualRollbackError:
            return None

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

    def ab_experiment_status(self, experiment_id: str) -> Optional[dict[str, Any]]:
        return self._ab_manager.get_experiment_status(experiment_id)

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

    @staticmethod
    def _cron_field_matches(
        expression: str,
        value: int,
        *,
        minimum: int,
        maximum: int,
        sunday_is_zero: bool = False,
    ) -> bool:
        expression = (expression or "").strip()
        if not expression:
            return False
        if expression == "*":
            return True

        def _normalize(raw: int) -> int:
            if sunday_is_zero and raw == 7:
                return 0
            return raw

        try:
            for token in expression.split(","):
                token = token.strip()
                if not token:
                    continue

                step = 1
                if "/" in token:
                    token, step_text = token.split("/", 1)
                    step = max(1, int(step_text))

                if token == "*":
                    start, end = minimum, maximum
                    if start <= value <= end and (value - start) % step == 0:
                        return True
                    continue

                if "-" in token:
                    start_text, end_text = token.split("-", 1)
                    start = _normalize(int(start_text))
                    end = _normalize(int(end_text))
                    if start <= value <= end and (value - start) % step == 0:
                        return True
                    continue

                candidate = _normalize(int(token))
                if minimum <= candidate <= maximum and candidate == value:
                    return True
        except ValueError:
            return False

        return False

    def weekly_params_optimizer_state(self) -> dict[str, Any]:
        return self._state_store.load_weekly_params_optimizer_state()

    def weekly_params_optimizer_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._state_store.load_weekly_params_optimizer_runs(limit=limit)

    def save_weekly_params_optimizer_state(self, state: dict[str, Any]) -> dict[str, Any]:
        self._state_store.save_weekly_params_optimizer_state(state)
        return self.weekly_params_optimizer_state()

    def get_due_weekly_params_optimizer_slot(
        self,
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        cron_expr = self.config.weekly_params_optimizer_cron
        if not isinstance(cron_expr, str) or not cron_expr.strip():
            return None

        fields = cron_expr.split()
        if len(fields) != 5:
            log.warning("[Evolution] 非法 weekly_params_optimizer_cron: {}", cron_expr)
            return None

        now = now or datetime.now(timezone.utc)
        minute_expr, hour_expr, day_expr, month_expr, weekday_expr = fields
        weekday_value = now.isoweekday() % 7
        if not self._cron_field_matches(minute_expr, now.minute, minimum=0, maximum=59):
            return None
        if not self._cron_field_matches(hour_expr, now.hour, minimum=0, maximum=23):
            return None
        if not self._cron_field_matches(day_expr, now.day, minimum=1, maximum=31):
            return None
        if not self._cron_field_matches(month_expr, now.month, minimum=1, maximum=12):
            return None
        if not self._cron_field_matches(
            weekday_expr,
            weekday_value,
            minimum=0,
            maximum=6,
            sunday_is_zero=True,
        ):
            return None

        slot_id = now.replace(second=0, microsecond=0).isoformat()
        state = self.weekly_params_optimizer_state()
        if state.get("last_successful_slot") == slot_id:
            return None
        return slot_id

    def record_weekly_params_optimizer_start(
        self,
        slot_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        state = dict(self.weekly_params_optimizer_state() or {})
        started_at = (now or datetime.now(timezone.utc)).isoformat()
        state.update(
            {
                "last_attempted_slot": slot_id,
                "last_attempted_at": started_at,
                "status": "running",
            }
        )
        return self.save_weekly_params_optimizer_state(state)

    def record_weekly_params_optimizer_finish(
        self,
        slot_id: str,
        *,
        status: str,
        optimized_symbols: Optional[list[dict[str, Any]]] = None,
        errors: Optional[dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        finished_at = (now or datetime.now(timezone.utc)).isoformat()
        state = dict(self.weekly_params_optimizer_state() or {})
        state.update(
            {
                "status": status,
                "last_finished_at": finished_at,
                "optimized_symbols": list(optimized_symbols or []),
                "last_error": errors or None,
            }
        )
        if status in {"success", "partial_success"}:
            state["last_successful_slot"] = slot_id
            state["last_successful_run_at"] = finished_at

        self._state_store.append_weekly_params_optimizer_run(
            {
                "slot_id": slot_id,
                "status": status,
                "finished_at": finished_at,
                "optimized_symbols": list(optimized_symbols or []),
                "errors": errors or None,
            }
        )
        return self.save_weekly_params_optimizer_state(state)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "registry": self._registry.diagnostics(),
            "state_store": self._state_store.diagnostics(),
            "scheduler": self._scheduler.diagnostics(),
            "weekly_params_optimizer": {
                "cron": self.config.weekly_params_optimizer_cron,
                "state": self.weekly_params_optimizer_state(),
            },
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
                    current_snap = self._registry.get(decision.candidate_id)
                    previous_active = self._find_active_family_members(
                        current_snap,
                        exclude_candidate_id=decision.candidate_id,
                    )
                    if previous_active:
                        self._prev_active[decision.candidate_id] = previous_active[0].candidate_id
                    for prev in previous_active:
                        self._registry.transition(
                            candidate_id=prev.candidate_id,
                            new_status=CandidateStatus.PAUSED,
                            reason=f"SUPERSEDED_BY:{decision.candidate_id}",
                        )

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
                if decision.action == PromotionAction.ROLLBACK.value:
                    rollback_candidate_id = self._prev_active.get(decision.candidate_id)
                    if rollback_candidate_id is not None:
                        self._registry.transition(
                            candidate_id=rollback_candidate_id,
                            new_status=CandidateStatus.ACTIVE,
                            reason=f"ROLLBACK_FROM:{decision.candidate_id}",
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

    def _find_active_family_members(
        self,
        snapshot: Optional[CandidateSnapshot],
        *,
        exclude_candidate_id: Optional[str] = None,
    ) -> list[CandidateSnapshot]:
        """查找与给定候选属于同一 family 的 active 候选。"""
        if snapshot is None:
            return []

        family_key = self._candidate_family_key(snapshot)
        if not family_key:
            return []

        active = self._registry.list_by_status(CandidateStatus.ACTIVE)
        family_members = [
            candidate
            for candidate in active
            if candidate.candidate_id != exclude_candidate_id
            and self._candidate_family_key(candidate) == family_key
        ]
        family_members.sort(
            key=lambda candidate: candidate.promoted_at or candidate.created_at,
            reverse=True,
        )
        return family_members

    def _resolve_manual_rollback_target(
        self,
        snapshot: CandidateSnapshot,
        *,
        rollback_to_candidate_id: Optional[str] = None,
    ) -> Optional[CandidateSnapshot]:
        family_key = self._candidate_family_key(snapshot)

        if rollback_to_candidate_id:
            target = self._registry.get(rollback_to_candidate_id)
            if target is None:
                raise ManualRollbackError(
                    "ROLLBACK_TARGET_NOT_FOUND",
                    f"Rollback target candidate does not exist: {rollback_to_candidate_id}",
                )
            if target.candidate_id == snapshot.candidate_id:
                raise ManualRollbackError(
                    "INVALID_ROLLBACK_TARGET",
                    "Rollback target candidate cannot be the same as current candidate",
                )
            if family_key and self._candidate_family_key(target) != family_key:
                raise ManualRollbackError(
                    "FAMILY_MISMATCH",
                    (
                        f"Rollback target family mismatch: current={family_key} "
                        f"target={self._candidate_family_key(target)}"
                    ),
                )
            if target.status == CandidateStatus.ACTIVE.value:
                raise ManualRollbackError(
                    "ROLLBACK_TARGET_ACTIVE",
                    "Rollback target candidate is already active",
                )
            return target

        previous_candidate_id = self._prev_active.get(snapshot.candidate_id)
        if previous_candidate_id:
            previous = self._registry.get(previous_candidate_id)
            if previous is not None and previous.status != CandidateStatus.ACTIVE.value:
                return previous

        if not family_key:
            raise ManualRollbackError(
                "NO_ROLLBACK_TARGET",
                "No rollback target is available for current candidate",
            )

        paused_family_members = [
            candidate
            for candidate in self._registry.list_by_status(CandidateStatus.PAUSED)
            if self._candidate_family_key(candidate) == family_key
        ]
        paused_family_members.sort(
            key=lambda candidate: candidate.promoted_at or candidate.created_at,
            reverse=True,
        )
        if paused_family_members:
            return paused_family_members[0]

        raise ManualRollbackError(
            "NO_ROLLBACK_TARGET",
            f"No paused rollback target is available in family: {family_key}",
        )

    def _select_manual_current_candidate(
        self,
        *,
        family_key: Optional[str],
        current_candidate_id: Optional[str],
    ) -> Optional[CandidateSnapshot]:
        active_candidates = sorted(
            self._registry.list_active(),
            key=lambda candidate: candidate.promoted_at or candidate.created_at,
            reverse=True,
        )
        if not active_candidates:
            raise ManualRollbackError(
                "NO_ACTIVE_CANDIDATE",
                "No active candidate is available for rollback",
            )

        if current_candidate_id:
            target = self._registry.get(current_candidate_id)
            if target is None:
                raise ManualRollbackError(
                    "CANDIDATE_NOT_FOUND",
                    f"Current candidate does not exist: {current_candidate_id}",
                )
            if target.status != CandidateStatus.ACTIVE.value:
                raise ManualRollbackError(
                    "CURRENT_NOT_ACTIVE",
                    f"Current candidate is not active: {current_candidate_id}",
                )
            if family_key and self._candidate_family_key(target) != family_key:
                raise ManualRollbackError(
                    "FAMILY_MISMATCH",
                    (
                        f"Current candidate family mismatch: expected={family_key} "
                        f"actual={self._candidate_family_key(target)}"
                    ),
                )
            return target

        if family_key:
            for candidate in active_candidates:
                if self._candidate_family_key(candidate) == family_key:
                    return candidate
            raise ManualRollbackError(
                "FAMILY_NO_ACTIVE_CANDIDATE",
                f"No active candidate found in family: {family_key}",
            )

        return active_candidates[0]

    @staticmethod
    def _candidate_family_key(snapshot: CandidateSnapshot) -> str:
        metadata = snapshot.metadata or {}
        return str(metadata.get("family_key") or snapshot.owner)
