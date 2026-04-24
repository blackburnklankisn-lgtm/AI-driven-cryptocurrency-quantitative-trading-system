"""
tests/test_phase3_w22.py — W22 SelfEvolutionEngine 单元测试

覆盖：
- evolution_types contracts (8 tests)
- CandidateRegistry (9 tests)
- ABTestManager (8 tests)
- PromotionGate (9 tests)
- RetirementPolicy (8 tests)
- EvolutionStateStore (7 tests)
- EvolutionScheduler (6 tests)
- ReportBuilder (5 tests)
- SelfEvolutionEngine (10 tests)

合计 ~70 tests
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from modules.alpha.contracts.evolution_types import (
    CandidateSnapshot,
    CandidateStatus,
    CandidateType,
    EvolutionReport,
    PromotionAction,
    PromotionDecision,
    RetirementRecord,
)
from modules.evolution.ab_test_manager import ABExperimentConfig, ABResult, ABTestManager
from modules.evolution.candidate_registry import CandidateRegistry
from modules.evolution.promotion_gate import (
    PromotionGate,
    PromotionGateConfig,
    StageGateConfig,
)
from modules.evolution.report_builder import ReportBuilder
from modules.evolution.retirement_policy import RetirementConfig, RetirementPolicy
from modules.evolution.scheduler import EvolutionScheduler, SchedulerConfig
from modules.evolution.self_evolution_engine import SelfEvolutionConfig, SelfEvolutionEngine
from modules.evolution.state_store import EvolutionStateStore


# ══════════════════════════════════════════════════════════════
# 共用 helpers
# ══════════════════════════════════════════════════════════════

def make_snapshot(
    candidate_id: str = "cand_001",
    status: CandidateStatus = CandidateStatus.CANDIDATE,
    sharpe: Optional[float] = None,
    max_dd: Optional[float] = None,
    win_rate: Optional[float] = None,
    ab_lift: Optional[float] = None,
    candidate_type: str = CandidateType.POLICY.value,
) -> CandidateSnapshot:
    return CandidateSnapshot(
        candidate_id=candidate_id,
        candidate_type=candidate_type,
        owner="rl/ppo",
        version="ppo_v1_20260424",
        status=status.value,
        created_at=datetime.now(tz=timezone.utc),
        sharpe_30d=sharpe,
        max_drawdown_30d=max_dd,
        win_rate_30d=win_rate,
        ab_lift=ab_lift,
    )


def make_promotion_decision(
    action: PromotionAction = PromotionAction.PROMOTE,
    from_status: CandidateStatus = CandidateStatus.CANDIDATE,
    to_status: CandidateStatus = CandidateStatus.SHADOW,
    candidate_id: str = "cand_001",
) -> PromotionDecision:
    return PromotionDecision(
        candidate_id=candidate_id,
        action=action.value,
        from_status=from_status.value,
        to_status=to_status.value,
        reason_codes=["GATE_PASSED"],
        effective_at=datetime.now(tz=timezone.utc),
    )


def make_tmp_dir() -> str:
    return tempfile.mkdtemp()


# ══════════════════════════════════════════════════════════════
# 一、evolution_types contracts (8 tests)
# ══════════════════════════════════════════════════════════════

class TestEvolutionTypesContracts:
    def test_candidate_status_values(self):
        assert CandidateStatus.CANDIDATE.value == "candidate"
        assert CandidateStatus.ACTIVE.value == "active"
        assert CandidateStatus.RETIRED.value == "retired"

    def test_promotion_action_values(self):
        assert PromotionAction.PROMOTE.value == "PROMOTE"
        assert PromotionAction.ROLLBACK.value == "ROLLBACK"

    def test_candidate_snapshot_is_active(self):
        snap = make_snapshot(status=CandidateStatus.ACTIVE)
        assert snap.is_active() is True

    def test_candidate_snapshot_is_retired(self):
        snap = make_snapshot(status=CandidateStatus.RETIRED)
        assert snap.is_retired() is True

    def test_passes_basic_gate_ok(self):
        snap = make_snapshot(sharpe=1.0, max_dd=0.05)
        assert snap.passes_basic_gate(min_sharpe=0.8, max_drawdown=0.07) is True

    def test_passes_basic_gate_fails_no_data(self):
        snap = make_snapshot()
        assert snap.passes_basic_gate() is False

    def test_promotion_decision_is_promotion(self):
        d = make_promotion_decision(PromotionAction.PROMOTE)
        assert d.is_promotion() is True

    def test_promotion_decision_is_rollback(self):
        d = make_promotion_decision(PromotionAction.ROLLBACK,
                                     from_status=CandidateStatus.ACTIVE,
                                     to_status=CandidateStatus.PAUSED)
        assert d.is_rollback() is True


# ══════════════════════════════════════════════════════════════
# 二、CandidateRegistry (9 tests)
# ══════════════════════════════════════════════════════════════

class TestCandidateRegistry:
    def make_registry(self) -> tuple[CandidateRegistry, str]:
        d = make_tmp_dir()
        reg = CandidateRegistry(registry_path=os.path.join(d, "candidates.json"))
        return reg, d

    def test_register_returns_snapshot(self):
        reg, _ = self.make_registry()
        snap = reg.register(CandidateType.POLICY, "rl/ppo", "v1")
        assert isinstance(snap, CandidateSnapshot)
        assert snap.status == CandidateStatus.CANDIDATE.value

    def test_register_default_status_is_candidate(self):
        reg, _ = self.make_registry()
        snap = reg.register(CandidateType.MODEL, "ml/rf", "v2")
        assert snap.status == CandidateStatus.CANDIDATE.value

    def test_transition_changes_status(self):
        reg, _ = self.make_registry()
        snap = reg.register(CandidateType.POLICY, "rl/ppo", "v1")
        updated = reg.transition(snap.candidate_id, CandidateStatus.SHADOW)
        assert updated.status == CandidateStatus.SHADOW.value

    def test_transition_nonexistent_returns_none(self):
        reg, _ = self.make_registry()
        result = reg.transition("nonexistent", CandidateStatus.SHADOW)
        assert result is None

    def test_update_metrics(self):
        reg, _ = self.make_registry()
        snap = reg.register(CandidateType.POLICY, "rl/ppo", "v1")
        updated = reg.update_metrics(snap.candidate_id, sharpe_30d=1.2, max_drawdown_30d=0.04)
        assert updated.sharpe_30d == pytest.approx(1.2)

    def test_list_by_status(self):
        reg, _ = self.make_registry()
        reg.register(CandidateType.POLICY, "rl/ppo", "v1")
        reg.register(CandidateType.POLICY, "rl/ppo", "v2")
        candidates = reg.list_by_status(CandidateStatus.CANDIDATE)
        assert len(candidates) == 2

    def test_list_active_empty_initially(self):
        reg, _ = self.make_registry()
        reg.register(CandidateType.POLICY, "rl/ppo", "v1")
        active = reg.list_active()
        assert len(active) == 0

    def test_persistence_round_trip(self):
        d = make_tmp_dir()
        path = os.path.join(d, "candidates.json")
        reg1 = CandidateRegistry(registry_path=path)
        snap = reg1.register(CandidateType.POLICY, "rl/ppo", "v1")
        # Re-load from disk
        reg2 = CandidateRegistry(registry_path=path)
        loaded = reg2.get(snap.candidate_id)
        assert loaded is not None
        assert loaded.candidate_id == snap.candidate_id

    def test_diagnostics_keys(self):
        reg, _ = self.make_registry()
        d = reg.diagnostics()
        assert "total_candidates" in d
        assert "by_status" in d


# ══════════════════════════════════════════════════════════════
# 三、ABTestManager (8 tests)
# ══════════════════════════════════════════════════════════════

class TestABTestManager:
    def make_manager(self, min_samples: int = 5) -> ABTestManager:
        return ABTestManager(ABExperimentConfig(min_samples=min_samples))

    def test_create_experiment_returns_id(self):
        mgr = self.make_manager()
        eid = mgr.create_experiment("ctrl", "test")
        assert isinstance(eid, str) and len(eid) > 0

    def test_record_and_get_status(self):
        mgr = self.make_manager()
        eid = mgr.create_experiment("ctrl", "test")
        mgr.record_step(eid, is_test=False, step_pnl=1.0)
        status = mgr.get_experiment_status(eid)
        assert status["control_n"] == 1

    def test_evaluate_insufficient_samples_fails(self):
        mgr = self.make_manager(min_samples=100)
        eid = mgr.create_experiment("ctrl", "test")
        mgr.record_step(eid, is_test=False, step_pnl=1.0)
        result = mgr.evaluate(eid)
        assert result.passes_gate is False
        assert "INSUFFICIENT_SAMPLES" in result.reason_codes

    def test_evaluate_sufficient_samples_passes(self):
        mgr = self.make_manager(min_samples=3)
        eid = mgr.create_experiment("ctrl", "test")
        for _ in range(5):
            mgr.record_step(eid, is_test=False, step_pnl=1.0)
            mgr.record_step(eid, is_test=True, step_pnl=2.0)
        result = mgr.evaluate(eid)
        assert result.passes_gate is True
        assert result.lift > 0

    def test_winner_is_test_when_passes(self):
        mgr = self.make_manager(min_samples=3)
        eid = mgr.create_experiment("ctrl", "test_candidate")
        for _ in range(5):
            mgr.record_step(eid, is_test=False, step_pnl=1.0)
            mgr.record_step(eid, is_test=True, step_pnl=2.0)
        result = mgr.evaluate(eid)
        if result.passes_gate:
            assert result.winner() == "test_candidate"

    def test_close_experiment_removes_it(self):
        mgr = self.make_manager()
        eid = mgr.create_experiment("ctrl", "test")
        mgr.close_experiment(eid)
        assert eid not in mgr.list_active_experiments()

    def test_diagnostics_keys(self):
        mgr = self.make_manager()
        d = mgr.diagnostics()
        assert "active_experiments" in d
        assert "completed_experiments" in d

    def test_result_summary_not_empty(self):
        mgr = self.make_manager(min_samples=2)
        eid = mgr.create_experiment("ctrl", "test")
        for _ in range(3):
            mgr.record_step(eid, False, 1.0)
            mgr.record_step(eid, True, 1.5)
        result = mgr.evaluate(eid)
        assert len(result.summary()) > 0


# ══════════════════════════════════════════════════════════════
# 四、PromotionGate (9 tests)
# ══════════════════════════════════════════════════════════════

class TestPromotionGate:
    def make_gate(self) -> PromotionGate:
        return PromotionGate(PromotionGateConfig(
            candidate_to_shadow=StageGateConfig(min_sharpe=0.8, max_drawdown=0.07),
            shadow_to_paper=StageGateConfig(min_sharpe=0.6, max_drawdown=0.09),
            paper_to_active=StageGateConfig(
                min_sharpe=0.5, max_drawdown=0.10,
                min_ab_lift=0.0, require_ab_completed=True,
            ),
        ))

    def test_candidate_to_shadow_passes(self):
        gate = self.make_gate()
        snap = make_snapshot(status=CandidateStatus.CANDIDATE, sharpe=1.0, max_dd=0.05)
        decision = gate.evaluate(snap)
        assert decision.action == PromotionAction.PROMOTE.value
        assert decision.to_status == CandidateStatus.SHADOW.value

    def test_candidate_to_shadow_fails_low_sharpe(self):
        gate = self.make_gate()
        snap = make_snapshot(status=CandidateStatus.CANDIDATE, sharpe=0.3, max_dd=0.05)
        decision = gate.evaluate(snap)
        assert decision.action == PromotionAction.HOLD.value

    def test_candidate_to_shadow_fails_high_drawdown(self):
        gate = self.make_gate()
        snap = make_snapshot(status=CandidateStatus.CANDIDATE, sharpe=1.5, max_dd=0.15)
        decision = gate.evaluate(snap)
        assert decision.action == PromotionAction.HOLD.value

    def test_candidate_to_shadow_fails_risk_violations(self):
        gate = PromotionGate(PromotionGateConfig(
            candidate_to_shadow=StageGateConfig(
                min_sharpe=0.8, max_drawdown=0.07, max_risk_violations=0
            )
        ))
        snap = make_snapshot(status=CandidateStatus.CANDIDATE, sharpe=1.0, max_dd=0.05)
        decision = gate.evaluate(snap, risk_violations=2)
        assert decision.action == PromotionAction.HOLD.value

    def test_paper_to_active_requires_ab(self):
        gate = self.make_gate()
        snap = make_snapshot(status=CandidateStatus.PAPER, sharpe=0.9, max_dd=0.05)
        decision = gate.evaluate(snap)  # ab_lift is None
        assert decision.action == PromotionAction.HOLD.value
        assert "AB_NOT_COMPLETED" in decision.reason_codes

    def test_paper_to_active_passes_with_ab(self):
        gate = self.make_gate()
        snap = make_snapshot(status=CandidateStatus.PAPER, sharpe=0.9, max_dd=0.05, ab_lift=0.1)
        decision = gate.evaluate(snap)
        assert decision.action == PromotionAction.PROMOTE.value

    def test_active_candidate_returns_hold(self):
        gate = self.make_gate()
        snap = make_snapshot(status=CandidateStatus.ACTIVE, sharpe=1.5, max_dd=0.02, ab_lift=0.5)
        decision = gate.evaluate(snap)
        assert decision.action == PromotionAction.HOLD.value

    def test_bulk_evaluate_returns_only_promotes(self):
        gate = self.make_gate()
        snaps = [
            make_snapshot("c1", CandidateStatus.CANDIDATE, sharpe=1.0, max_dd=0.05),
            make_snapshot("c2", CandidateStatus.CANDIDATE, sharpe=0.2, max_dd=0.05),
        ]
        decisions = gate.bulk_evaluate(snaps)
        assert all(d.action == PromotionAction.PROMOTE.value for d in decisions)
        assert len(decisions) == 1

    def test_shadow_to_paper_passes(self):
        gate = self.make_gate()
        snap = make_snapshot(status=CandidateStatus.SHADOW, sharpe=0.8, max_dd=0.07)
        decision = gate.evaluate(snap)
        assert decision.action == PromotionAction.PROMOTE.value
        assert decision.to_status == CandidateStatus.PAPER.value


# ══════════════════════════════════════════════════════════════
# 五、RetirementPolicy (8 tests)
# ══════════════════════════════════════════════════════════════

class TestRetirementPolicy:
    def make_policy(self, **kwargs) -> RetirementPolicy:
        cfg = RetirementConfig(**kwargs) if kwargs else RetirementConfig()
        return RetirementPolicy(cfg)

    def test_active_good_performance_holds(self):
        policy = self.make_policy()
        snap = make_snapshot(status=CandidateStatus.ACTIVE, sharpe=1.5, max_dd=0.03)
        decision = policy.evaluate(snap)
        assert decision.action == PromotionAction.HOLD.value

    def test_demote_on_low_sharpe(self):
        policy = self.make_policy(
            demote_sharpe_threshold=0.5, demote_consecutive_days=5
        )
        snap = make_snapshot(status=CandidateStatus.ACTIVE, sharpe=0.3, max_dd=0.05)
        decision = policy.evaluate(snap, consecutive_low_sharpe_days=10)
        assert decision.action == PromotionAction.DEMOTE.value
        assert decision.to_status == CandidateStatus.PAUSED.value

    def test_retire_on_sustained_neg_sharpe(self):
        policy = self.make_policy(
            retire_sharpe_threshold=0.0, retire_consecutive_days=10
        )
        snap = make_snapshot(status=CandidateStatus.ACTIVE, sharpe=-0.5, max_dd=0.05)
        decision = policy.evaluate(snap, consecutive_low_sharpe_days=15)
        assert decision.action in (PromotionAction.RETIRE.value, PromotionAction.ROLLBACK.value)

    def test_retire_on_risk_violations(self):
        policy = self.make_policy(retire_risk_violations=3)
        snap = make_snapshot(status=CandidateStatus.ACTIVE, sharpe=1.0, max_dd=0.05)
        decision = policy.evaluate(snap, risk_violations=5)
        assert decision.action in (PromotionAction.RETIRE.value, PromotionAction.ROLLBACK.value)

    def test_rollback_when_prev_version_available(self):
        policy = self.make_policy(
            retire_risk_violations=3, auto_rollback_on_retire=True
        )
        snap = make_snapshot(status=CandidateStatus.ACTIVE, sharpe=1.0, max_dd=0.05)
        decision = policy.evaluate(snap, risk_violations=5, has_previous_version=True)
        assert decision.action == PromotionAction.ROLLBACK.value

    def test_retire_not_rollback_when_no_prev(self):
        policy = self.make_policy(retire_risk_violations=3, auto_rollback_on_retire=True)
        snap = make_snapshot(status=CandidateStatus.ACTIVE, sharpe=1.0, max_dd=0.05)
        decision = policy.evaluate(snap, risk_violations=5, has_previous_version=False)
        assert decision.action == PromotionAction.RETIRE.value

    def test_bulk_evaluate_skips_non_active(self):
        policy = self.make_policy(retire_risk_violations=1)
        snaps = [
            make_snapshot("c1", CandidateStatus.SHADOW, sharpe=0.1, max_dd=0.5),
            make_snapshot("c2", CandidateStatus.ACTIVE, sharpe=1.0, max_dd=0.05),
        ]
        decisions = policy.bulk_evaluate(snaps, risk_violations_map={"c2": 0})
        # c1 is shadow so skipped; c2 performs OK
        assert all(d.candidate_id == "c2" for d in decisions)

    def test_make_retirement_record_fields(self):
        policy = self.make_policy(retire_risk_violations=3)
        snap = make_snapshot(status=CandidateStatus.ACTIVE, sharpe=-0.5, max_dd=0.20)
        decision = policy.evaluate(snap, risk_violations=5)
        record = policy.make_retirement_record(snap, decision)
        assert record.candidate_id == snap.candidate_id
        assert len(record.reason_codes) > 0


# ══════════════════════════════════════════════════════════════
# 六、EvolutionStateStore (7 tests)
# ══════════════════════════════════════════════════════════════

class TestEvolutionStateStore:
    def make_store(self) -> EvolutionStateStore:
        return EvolutionStateStore(make_tmp_dir())

    def test_append_and_load_decisions(self):
        store = self.make_store()
        d = make_promotion_decision()
        store.append_decision(d)
        loaded = store.load_decisions(limit=10)
        assert len(loaded) == 1
        assert loaded[0]["candidate_id"] == d.candidate_id

    def test_append_multiple_decisions(self):
        store = self.make_store()
        for i in range(5):
            store.append_decision(make_promotion_decision(candidate_id=f"cand_{i:03d}"))
        loaded = store.load_decisions(limit=10)
        assert len(loaded) == 5

    def test_save_and_load_report(self):
        store = self.make_store()
        snap = make_snapshot()
        d = make_promotion_decision()
        report = EvolutionReport(
            report_id="rpt_001",
            period_start=datetime.now(tz=timezone.utc),
            period_end=datetime.now(tz=timezone.utc),
            total_candidates=1,
            promoted=["cand_001"],
            demoted=[],
            retired=[],
            rollbacks=[],
            decisions=[d],
            active_snapshot=[snap],
        )
        store.save_report(report)
        loaded = store.load_report()
        assert loaded is not None
        assert loaded["report_id"] == "rpt_001"

    def test_load_report_none_when_empty(self):
        store = self.make_store()
        assert store.load_report() is None

    def test_scheduler_state_round_trip(self):
        store = self.make_store()
        state = {"last_run_at": "2026-04-24T00:00:00+00:00", "run_count": 5}
        store.save_scheduler_state(state)
        loaded = store.load_scheduler_state()
        assert loaded["run_count"] == 5

    def test_append_retirement(self):
        store = self.make_store()
        record = RetirementRecord(
            candidate_id="cand_001",
            reason_codes=["RISK_VIOLATIONS:5"],
            trigger_metrics={"sharpe_30d": -0.5},
            retired_at=datetime.now(tz=timezone.utc),
        )
        store.append_retirement(record)
        loaded = store.load_retirements(limit=10)
        assert len(loaded) == 1

    def test_diagnostics_keys(self):
        store = self.make_store()
        d = store.diagnostics()
        assert "total_decisions" in d
        assert "has_latest_report" in d


# ══════════════════════════════════════════════════════════════
# 七、EvolutionScheduler (6 tests)
# ══════════════════════════════════════════════════════════════

class TestEvolutionScheduler:
    def make_scheduler(self, interval_sec: float = 7200.0) -> EvolutionScheduler:
        return EvolutionScheduler(SchedulerConfig(interval_sec=interval_sec, cooldown_sec=0.0))

    def test_should_run_initially_true(self):
        sched = self.make_scheduler()
        assert sched.should_run() is True

    def test_should_run_false_in_cooldown(self):
        sched = EvolutionScheduler(SchedulerConfig(
            interval_sec=100.0, cooldown_sec=3600.0
        ))
        sched.record_run(success=True)
        assert sched.should_run() is False

    def test_force_run_bypasses_interval(self):
        sched = self.make_scheduler(interval_sec=999999.0)
        sched.record_run(success=True)
        assert sched.should_run(force=True) is True

    def test_record_run_updates_count(self):
        sched = self.make_scheduler()
        sched.record_run(success=True, promotions=2)
        assert sched.run_count() == 1

    def test_next_run_at_after_record(self):
        sched = self.make_scheduler(interval_sec=3600.0)
        sched.record_run(success=True)
        next_t = sched.next_run_at()
        assert next_t is not None
        assert next_t > datetime.now(tz=timezone.utc)

    def test_diagnostics_keys(self):
        sched = self.make_scheduler()
        d = sched.diagnostics()
        assert "run_count" in d
        assert "interval_sec" in d


# ══════════════════════════════════════════════════════════════
# 八、ReportBuilder (5 tests)
# ══════════════════════════════════════════════════════════════

class TestReportBuilder:
    def test_build_returns_evolution_report(self):
        builder = ReportBuilder()
        snap = make_snapshot(status=CandidateStatus.ACTIVE)
        d = make_promotion_decision()
        report = builder.build(
            period_start=datetime.now(tz=timezone.utc),
            decisions=[d],
            active_snapshot=[snap],
        )
        assert isinstance(report, EvolutionReport)

    def test_promoted_list_populated(self):
        builder = ReportBuilder()
        d = make_promotion_decision(PromotionAction.PROMOTE, candidate_id="cand_001")
        report = builder.build(
            period_start=datetime.now(tz=timezone.utc),
            decisions=[d],
            active_snapshot=[],
        )
        assert "cand_001" in report.promoted

    def test_demoted_list_populated(self):
        builder = ReportBuilder()
        d = make_promotion_decision(PromotionAction.DEMOTE,
                                     from_status=CandidateStatus.ACTIVE,
                                     to_status=CandidateStatus.PAUSED,
                                     candidate_id="cand_002")
        report = builder.build(
            period_start=datetime.now(tz=timezone.utc),
            decisions=[d],
            active_snapshot=[],
        )
        assert "cand_002" in report.demoted

    def test_hold_decisions_excluded_from_report(self):
        builder = ReportBuilder()
        d = make_promotion_decision(PromotionAction.HOLD,
                                     from_status=CandidateStatus.ACTIVE,
                                     to_status=CandidateStatus.ACTIVE)
        report = builder.build(
            period_start=datetime.now(tz=timezone.utc),
            decisions=[d],
            active_snapshot=[],
        )
        assert len(report.decisions) == 0

    def test_text_summary_not_empty(self):
        builder = ReportBuilder()
        d = make_promotion_decision()
        report = builder.build(
            period_start=datetime.now(tz=timezone.utc),
            decisions=[d],
            active_snapshot=[],
        )
        summary = builder.text_summary(report)
        assert len(summary) > 0
        assert "Report" in summary


# ══════════════════════════════════════════════════════════════
# 九、SelfEvolutionEngine (10 tests)
# ══════════════════════════════════════════════════════════════

class TestSelfEvolutionEngine:
    def make_engine(self) -> SelfEvolutionEngine:
        d = make_tmp_dir()
        config = SelfEvolutionConfig(
            state_dir=d,
            auto_run=False,
            scheduler=SchedulerConfig(interval_sec=0.0, cooldown_sec=0.0),
        )
        return SelfEvolutionEngine(config)

    def test_register_candidate(self):
        engine = self.make_engine()
        snap = engine.register_candidate(CandidateType.POLICY, "rl/ppo", "v1")
        assert snap.status == CandidateStatus.CANDIDATE.value

    def test_get_candidate_after_register(self):
        engine = self.make_engine()
        snap = engine.register_candidate(CandidateType.POLICY, "rl/ppo", "v1")
        loaded = engine.get_candidate(snap.candidate_id)
        assert loaded is not None
        assert loaded.candidate_id == snap.candidate_id

    def test_update_metrics(self):
        engine = self.make_engine()
        snap = engine.register_candidate(CandidateType.POLICY, "rl/ppo", "v1")
        updated = engine.update_metrics(snap.candidate_id, sharpe_30d=1.5, max_drawdown_30d=0.05)
        assert updated.sharpe_30d == pytest.approx(1.5)

    def test_run_cycle_returns_report(self):
        engine = self.make_engine()
        report = engine.run_cycle(force=True)
        assert report is not None

    def test_promotion_on_run_cycle(self):
        engine = self.make_engine()
        snap = engine.register_candidate(CandidateType.POLICY, "rl/ppo", "v1",
                                          candidate_id="cand_test")
        engine.update_metrics("cand_test", sharpe_30d=1.5, max_drawdown_30d=0.04)
        report = engine.run_cycle(force=True)
        assert "cand_test" in report.promoted

    def test_candidate_status_changes_to_shadow_after_promotion(self):
        engine = self.make_engine()
        snap = engine.register_candidate(CandidateType.POLICY, "rl/ppo", "v1",
                                          candidate_id="cand_shadow")
        engine.update_metrics("cand_shadow", sharpe_30d=1.5, max_drawdown_30d=0.04)
        engine.run_cycle(force=True)
        updated = engine.get_candidate("cand_shadow")
        assert updated.status == CandidateStatus.SHADOW.value

    def test_force_promote(self):
        engine = self.make_engine()
        snap = engine.register_candidate(CandidateType.POLICY, "rl/ppo", "v1",
                                          candidate_id="cand_fp")
        result = engine.force_promote("cand_fp", CandidateStatus.ACTIVE)
        assert result.status == CandidateStatus.ACTIVE.value

    def test_force_retire(self):
        engine = self.make_engine()
        snap = engine.register_candidate(CandidateType.POLICY, "rl/ppo", "v1",
                                          candidate_id="cand_retire")
        engine.force_retire("cand_retire")
        updated = engine.get_candidate("cand_retire")
        assert updated.status == CandidateStatus.RETIRED.value

    def test_ab_experiment_workflow(self):
        engine = self.make_engine()
        eid = engine.create_ab_experiment("ctrl", "test_c")
        for _ in range(5):
            engine.record_ab_step(eid, is_test=False, step_pnl=1.0)
            engine.record_ab_step(eid, is_test=True, step_pnl=2.0)
        result = engine.conclude_ab_experiment(eid)
        assert result is not None
        assert result.experiment_id == eid

    def test_diagnostics_keys(self):
        engine = self.make_engine()
        d = engine.diagnostics()
        assert "registry" in d
        assert "scheduler" in d
        assert "ab_manager" in d
