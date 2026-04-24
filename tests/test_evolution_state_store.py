"""
tests/test_evolution_state_store.py — 演进引擎状态存储完整测试

覆盖项：
1. EvolutionStateStore 初始化
2. append_decision / append_decisions / load_decisions（JSONL 追加+尾部加载）
3. append_retirement / load_retirements
4. save_report / load_report（原子覆写）
5. save/load_scheduler_state
6. save/load_weekly_params_optimizer_state
7. append/load_weekly_params_optimizer_runs
8. diagnostics 字段
9. 文件不存在时的容错路径
10. 损坏文件的异常处理
11. _atomic_write / _append_line / _load_jsonl_tail 内部行为
12. 并发安全写入
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from modules.evolution.state_store import EvolutionStateStore, _json_default
from modules.alpha.contracts.evolution_types import (
    PromotionDecision,
    RetirementRecord,
    EvolutionReport,
)


# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def store(tmp_path: Path) -> EvolutionStateStore:
    return EvolutionStateStore(state_dir=str(tmp_path / "evolution_test"))


def _make_decision(
    candidate_id: str = "cand-001",
    action: str = "PROMOTE",
    from_status: str = "candidate",
    to_status: str = "active",
) -> PromotionDecision:
    return PromotionDecision(
        candidate_id=candidate_id,
        action=action,
        from_status=from_status,
        to_status=to_status,
        reason_codes=["test_reason"],
        effective_at=datetime.now(tz=timezone.utc),
    )


def _make_retirement(
    candidate_id: str = "cand-retire-001",
) -> RetirementRecord:
    return RetirementRecord(
        candidate_id=candidate_id,
        retired_at=datetime.now(tz=timezone.utc),
        reason_codes=["low_sharpe"],
        trigger_metrics={"sharpe": -0.1},
    )


def _make_report(report_id: str = "report-001") -> EvolutionReport:
    now = datetime.now(tz=timezone.utc)
    return EvolutionReport(
        report_id=report_id,
        period_start=now,
        period_end=now,
        total_candidates=5,
        promoted=[],
        demoted=[],
        retired=[],
        rollbacks=[],
        decisions=[],
        active_snapshot=[],
    )


# ══════════════════════════════════════════════════════════════
# 1. _json_default 辅助函数
# ══════════════════════════════════════════════════════════════

class TestJsonDefault:

    def test_datetime_serializes_to_isoformat(self):
        now = datetime(2024, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
        s = _json_default(now)
        assert "2024-03-10" in s

    def test_unknown_type_raises_type_error(self):
        with pytest.raises(TypeError):
            _json_default(object())


# ══════════════════════════════════════════════════════════════
# 2. 决策审计日志
# ══════════════════════════════════════════════════════════════

class TestDecisionLog:

    def test_append_one_decision_loadable(self, store):
        d = _make_decision("c-1")
        store.append_decision(d)
        decisions = store.load_decisions()
        assert len(decisions) == 1
        assert decisions[0]["candidate_id"] == "c-1"

    def test_load_decisions_newest_first(self, store):
        for i in range(5):
            store.append_decision(_make_decision(f"c-{i}"))
        decisions = store.load_decisions()
        assert len(decisions) == 5
        # load_decisions returns newest first (reversed JSONL tail)
        assert decisions[0]["candidate_id"] == "c-4"

    def test_load_decisions_limit(self, store):
        for i in range(20):
            store.append_decision(_make_decision(f"c-{i}"))
        decisions = store.load_decisions(limit=5)
        assert len(decisions) == 5

    def test_append_decisions_batch(self, store):
        ds = [_make_decision(f"c-{i}") for i in range(3)]
        store.append_decisions(ds)
        decisions = store.load_decisions()
        assert len(decisions) == 3

    def test_load_decisions_file_not_exists_returns_empty(self, store):
        decisions = store.load_decisions()
        assert decisions == []

    def test_decision_action_preserved(self, store):
        store.append_decision(_make_decision(action="ROLLBACK"))
        d = store.load_decisions()[0]
        assert d["action"] == "ROLLBACK"


# ══════════════════════════════════════════════════════════════
# 3. 淘汰记录
# ══════════════════════════════════════════════════════════════

class TestRetirementLog:

    def test_append_and_load_retirement(self, store):
        r = _make_retirement("cand-001")
        store.append_retirement(r)
        retirements = store.load_retirements()
        assert len(retirements) == 1
        assert retirements[0]["candidate_id"] == "cand-001"

    def test_load_retirements_file_not_exists_returns_empty(self, store):
        assert store.load_retirements() == []

    def test_multiple_retirements_ordered_newest_first(self, store):
        for i in range(4):
            store.append_retirement(_make_retirement(f"cand-{i:03d}"))
        retirements = store.load_retirements()
        assert len(retirements) == 4
        assert retirements[0]["candidate_id"] == "cand-003"

    def test_retirement_reason_codes_preserved(self, store):
        r = _make_retirement()
        r.reason_codes.append("excessive_drawdown")
        store.append_retirement(r)
        result = store.load_retirements()[0]
        assert "low_sharpe" in result["reason_codes"]


# ══════════════════════════════════════════════════════════════
# 4. 演进报告
# ══════════════════════════════════════════════════════════════

class TestReport:

    def test_save_and_load_report(self, store):
        rpt = _make_report("r-001")
        store.save_report(rpt)
        loaded = store.load_report()
        assert loaded is not None
        assert loaded["report_id"] == "r-001"

    def test_load_report_not_exists_returns_none(self, store):
        assert store.load_report() is None

    def test_save_report_overwrites_previous(self, store):
        store.save_report(_make_report("r-001"))
        store.save_report(_make_report("r-002"))
        loaded = store.load_report()
        assert loaded["report_id"] == "r-002"

    def test_save_report_is_atomic(self, store, tmp_path):
        """原子写入：.tmp 文件不应残留。"""
        store.save_report(_make_report("r-001"))
        state_dir = Path(str(tmp_path / "evolution_test"))
        tmp_files = list(state_dir.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_load_report_corrupted_returns_none(self, store, tmp_path):
        report_path = tmp_path / "evolution_test" / "latest_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("BAD JSON", encoding="utf-8")
        result = store.load_report()
        assert result is None


# ══════════════════════════════════════════════════════════════
# 5. 调度器状态
# ══════════════════════════════════════════════════════════════

class TestSchedulerState:

    def test_save_and_load_scheduler_state(self, store):
        state = {"last_run_slot": "2024-W10", "runs": 5}
        store.save_scheduler_state(state)
        loaded = store.load_scheduler_state()
        assert loaded["last_run_slot"] == "2024-W10"
        assert loaded["runs"] == 5

    def test_load_scheduler_state_not_exists_returns_empty(self, store):
        assert store.load_scheduler_state() == {}

    def test_save_scheduler_state_overwrites(self, store):
        store.save_scheduler_state({"v": 1})
        store.save_scheduler_state({"v": 2})
        assert store.load_scheduler_state()["v"] == 2

    def test_load_scheduler_state_corrupted_returns_empty(self, store, tmp_path):
        path = tmp_path / "evolution_test" / "scheduler_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("CORRUPT", encoding="utf-8")
        result = store.load_scheduler_state()
        assert result == {}


# ══════════════════════════════════════════════════════════════
# 6. 周级参数优化状态与审计
# ══════════════════════════════════════════════════════════════

class TestWeeklyParamsOptimizer:

    def test_save_and_load_weekly_state(self, store):
        state = {"last_slot": "2024-W10", "status": "completed"}
        store.save_weekly_params_optimizer_state(state)
        loaded = store.load_weekly_params_optimizer_state()
        assert loaded["last_slot"] == "2024-W10"

    def test_load_weekly_state_not_exists_returns_empty(self, store):
        assert store.load_weekly_params_optimizer_state() == {}

    def test_load_weekly_state_corrupted_returns_empty(self, store, tmp_path):
        path = tmp_path / "evolution_test" / "weekly_params_optimizer_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("INVALID", encoding="utf-8")
        assert store.load_weekly_params_optimizer_state() == {}

    def test_append_and_load_weekly_runs(self, store):
        for i in range(3):
            store.append_weekly_params_optimizer_run({
                "slot_id": f"2024-W{10+i}",
                "status": "done",
                "targets_count": 3,
            })
        runs = store.load_weekly_params_optimizer_runs()
        assert len(runs) == 3
        # newest first
        assert runs[0]["slot_id"] == "2024-W12"

    def test_load_weekly_runs_empty_when_no_file(self, store):
        assert store.load_weekly_params_optimizer_runs() == []

    def test_load_weekly_runs_limit(self, store):
        for i in range(20):
            store.append_weekly_params_optimizer_run({"slot_id": f"slot-{i}"})
        runs = store.load_weekly_params_optimizer_runs(limit=5)
        assert len(runs) == 5


# ══════════════════════════════════════════════════════════════
# 7. diagnostics
# ══════════════════════════════════════════════════════════════

class TestDiagnostics:

    def test_diagnostics_empty_store(self, store):
        diag = store.diagnostics()
        assert diag["total_decisions"] == 0
        assert diag["total_retirements"] == 0
        assert diag["has_latest_report"] is False
        assert "state_dir" in diag

    def test_diagnostics_after_data(self, store):
        store.append_decision(_make_decision())
        store.append_decision(_make_decision("c-002"))
        store.append_retirement(_make_retirement())
        store.save_report(_make_report())
        store.save_weekly_params_optimizer_state({"v": 1})
        store.append_weekly_params_optimizer_run({"s": "ok"})

        diag = store.diagnostics()
        assert diag["total_decisions"] == 2
        assert diag["total_retirements"] == 1
        assert diag["has_latest_report"] is True
        assert diag["has_weekly_params_optimizer_state"] is True
        assert diag["total_weekly_params_optimizer_runs"] == 1


# ══════════════════════════════════════════════════════════════
# 8. 并发写入安全
# ══════════════════════════════════════════════════════════════

class TestConcurrentWrites:

    def test_concurrent_append_decisions_no_corruption(self, store):
        errors = []

        def _append(i):
            try:
                store.append_decision(_make_decision(f"cand-{i:03d}"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_append, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        decisions = store.load_decisions(limit=100)
        assert len(decisions) == 30

    def test_concurrent_save_scheduler_state_no_corruption(self, store):
        errors = []

        def _save(i):
            try:
                store.save_scheduler_state({"round": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_save, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        state = store.load_scheduler_state()
        # Some valid state should exist
        assert "round" in state


# ══════════════════════════════════════════════════════════════
# 9. _load_jsonl_tail 静态方法
# ══════════════════════════════════════════════════════════════

class TestLoadJsonlTail:

    def test_load_empty_file_returns_empty(self, tmp_path):
        path = str(tmp_path / "empty.jsonl")
        open(path, "w").close()
        result = EvolutionStateStore._load_jsonl_tail(path, 10)
        assert result == []

    def test_load_nonexistent_file_returns_empty(self, tmp_path):
        result = EvolutionStateStore._load_jsonl_tail(str(tmp_path / "ghost.jsonl"), 10)
        assert result == []

    def test_load_valid_jsonl(self, tmp_path):
        path = str(tmp_path / "test.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for i in range(5):
                f.write(json.dumps({"i": i}) + "\n")
        result = EvolutionStateStore._load_jsonl_tail(path, 10)
        assert len(result) == 5
        # Newest first
        assert result[0]["i"] == 4

    def test_load_jsonl_limit_cuts_tail(self, tmp_path):
        path = str(tmp_path / "big.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for i in range(100):
                f.write(json.dumps({"i": i}) + "\n")
        result = EvolutionStateStore._load_jsonl_tail(path, 10)
        assert len(result) == 10
        # Should be the last 10 entries (newest first)
        assert result[0]["i"] == 99

    def test_load_jsonl_with_blank_lines(self, tmp_path):
        path = str(tmp_path / "sparse.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"i": 0}\n')
            f.write("\n")
            f.write('{"i": 1}\n')
        result = EvolutionStateStore._load_jsonl_tail(path, 10)
        assert len(result) == 2
