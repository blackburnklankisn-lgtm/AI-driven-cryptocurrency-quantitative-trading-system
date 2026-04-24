"""
tests/test_phase2_w11.py — Phase 2 W11 风控层单元测试

覆盖项：
- StateStore: save/load/delete/keys/wipe/diagnostics/原子写入/跨 key 隔离
- BudgetChecker: 正常检查、各类拒绝路径、record_order/record_close、reset_daily、
                 DCA 上限、日内上限、状态持久化恢复
- KillSwitch: 回撤触发、日损触发、连续拒绝触发、连续失败触发、
              数据源 stale 触发、手动激活/解除、自动冷却恢复、
              持久化恢复、状态计数器、health_snapshot
"""

from __future__ import annotations

import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modules.risk.budget_checker import BudgetChecker, BudgetConfig
from modules.risk.kill_switch import KillSwitch, KillSwitchConfig
from modules.risk.snapshot import RiskSnapshot
from modules.risk.state_store import StateStore


# ─────────────────────────────────────────────────────────────
# 测试工厂 / fixtures
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_store(tmp_path: Path) -> StateStore:
    """每个测试用独立临时目录的 StateStore。"""
    return StateStore(path=tmp_path / "risk_state.json")


@pytest.fixture
def checker(tmp_store: StateStore) -> BudgetChecker:
    """默认配置的 BudgetChecker（绑定临时 StateStore）。"""
    return BudgetChecker(config=BudgetConfig(), state_store=tmp_store)


@pytest.fixture
def ks(tmp_store: StateStore) -> KillSwitch:
    """默认配置的 KillSwitch（绑定临时 StateStore）。"""
    return KillSwitch(config=KillSwitchConfig(), state_store=tmp_store)


def make_snapshot(
    drawdown: float = 0.0,
    daily_loss: float = 0.0,
    circuit_broken: bool = False,
    kill_switch: bool = False,
    budget: float = 1.0,
) -> RiskSnapshot:
    return RiskSnapshot(
        current_drawdown=drawdown,
        daily_loss_pct=daily_loss,
        consecutive_losses=0,
        circuit_broken=circuit_broken,
        kill_switch_active=kill_switch,
        budget_remaining_pct=budget,
    )


# ─────────────────────────────────────────────────────────────
# StateStore 测试
# ─────────────────────────────────────────────────────────────

class TestStateStore:
    def test_save_and_load(self, tmp_store: StateStore):
        tmp_store.save("kill_switch", {"active": True, "reason": "测试"})
        data = tmp_store.load("kill_switch")
        assert data is not None
        assert data["active"] is True
        assert data["reason"] == "测试"

    def test_load_missing_key_returns_none(self, tmp_store: StateStore):
        assert tmp_store.load("nonexistent") is None

    def test_delete_existing_key(self, tmp_store: StateStore):
        tmp_store.save("budget", {"deployed_pct": 0.3})
        deleted = tmp_store.delete("budget")
        assert deleted is True
        assert tmp_store.load("budget") is None

    def test_delete_missing_key_returns_false(self, tmp_store: StateStore):
        assert tmp_store.delete("nonexistent") is False

    def test_keys_returns_user_keys(self, tmp_store: StateStore):
        tmp_store.save("kill_switch", {"active": False})
        tmp_store.save("budget", {"deployed_pct": 0.1})
        keys = tmp_store.keys()
        assert "kill_switch" in keys
        assert "budget" in keys
        # 内部元数据 key 不应该出现
        assert "_last_written_at" not in keys

    def test_wipe_clears_all(self, tmp_store: StateStore):
        tmp_store.save("kill_switch", {"active": True})
        tmp_store.wipe()
        assert tmp_store.keys() == []

    def test_overwrite_existing_key(self, tmp_store: StateStore):
        tmp_store.save("budget", {"deployed_pct": 0.1})
        tmp_store.save("budget", {"deployed_pct": 0.5})
        data = tmp_store.load("budget")
        assert data["deployed_pct"] == pytest.approx(0.5)

    def test_cross_key_isolation(self, tmp_store: StateStore):
        """写入一个 key 不影响另一个 key。"""
        tmp_store.save("kill_switch", {"active": True})
        tmp_store.save("budget", {"deployed_pct": 0.2})
        tmp_store.save("kill_switch", {"active": False})
        budget = tmp_store.load("budget")
        assert budget["deployed_pct"] == pytest.approx(0.2)

    def test_diagnostics_structure(self, tmp_store: StateStore):
        tmp_store.save("kill_switch", {"active": False})
        diag = tmp_store.diagnostics()
        assert "path" in diag
        assert "exists" in diag
        assert "keys" in diag
        assert "kill_switch" in diag["keys"]

    def test_fresh_store_no_file_returns_empty(self, tmp_path: Path):
        store = StateStore(path=tmp_path / "new_state.json")
        assert store.load("any_key") is None
        assert store.keys() == []


# ─────────────────────────────────────────────────────────────
# BudgetChecker 测试
# ─────────────────────────────────────────────────────────────

class TestBudgetChecker:
    def test_normal_order_allowed(self, checker: BudgetChecker):
        allowed, reason, _ = checker.check(order_value_pct=0.10)
        assert allowed is True
        assert reason == "OK"

    def test_order_below_min_rejected(self, checker: BudgetChecker):
        """小于 min_order_budget_pct 的订单应被拒绝。"""
        cfg = BudgetConfig(min_order_budget_pct=0.01)
        c = BudgetChecker(config=cfg)
        allowed, reason, _ = c.check(order_value_pct=0.001)
        assert allowed is False
        assert "最小阈值" in reason

    def test_single_order_over_cap_rejected(self, checker: BudgetChecker):
        """单笔超过 max_single_order_budget_pct 应被拒绝。"""
        cfg = BudgetConfig(max_single_order_budget_pct=0.25)
        c = BudgetChecker(config=cfg)
        allowed, reason, _ = c.check(order_value_pct=0.30)
        assert allowed is False
        assert "单笔上限" in reason

    def test_budget_exhausted_rejected(self, checker: BudgetChecker):
        """累计部署预算超过 max_budget_usage_pct 应被拒绝。"""
        cfg = BudgetConfig(max_budget_usage_pct=0.30, fee_reserve_pct=0.0, slippage_reserve_pct=0.0)
        c = BudgetChecker(config=cfg)
        # 先部署 0.25
        c.record_order(0.25)
        # 再下 0.10 → 0.35 超过 0.30
        allowed, reason, _ = c.check(order_value_pct=0.10)
        assert allowed is False
        assert "总量超限" in reason or "预算不足" in reason

    def test_dca_over_cap_rejected(self):
        """DCA 部署超过 dca_budget_cap_pct 应被拒绝。"""
        cfg = BudgetConfig(
            dca_budget_cap_pct=0.30,
            max_budget_usage_pct=0.99,
            fee_reserve_pct=0.0,
            slippage_reserve_pct=0.0,
        )
        c = BudgetChecker(config=cfg)
        c.record_order(0.20, is_dca=True)
        # 再加 0.15 DCA → 0.35 > 0.30
        allowed, reason, _ = c.check(0.15, is_dca=True)
        assert allowed is False
        assert "DCA" in reason

    def test_intraday_cap_rejected(self):
        """日内累计超过 intraday_budget_cap_pct 应被拒绝。"""
        cfg = BudgetConfig(
            intraday_budget_cap_pct=0.40,
            max_budget_usage_pct=0.99,
            fee_reserve_pct=0.0,
            slippage_reserve_pct=0.0,
        )
        c = BudgetChecker(config=cfg)
        c.record_order(0.30)
        # 再下 0.20 → 日内 0.50 > 0.40
        allowed, reason, _ = c.check(0.20)
        assert allowed is False
        assert "日内" in reason

    def test_record_order_increases_deployed(self, checker: BudgetChecker):
        checker.record_order(0.10)
        snap = checker.snapshot()
        assert snap["deployed_pct"] > 0.0

    def test_record_close_decreases_deployed(self, checker: BudgetChecker):
        checker.record_order(0.20)
        deployed_before = checker.snapshot()["deployed_pct"]
        checker.record_close(0.20)
        deployed_after = checker.snapshot()["deployed_pct"]
        assert deployed_after < deployed_before

    def test_record_order_dca_tracks_separately(self, checker: BudgetChecker):
        checker.record_order(0.15, is_dca=True)
        snap = checker.snapshot()
        assert snap["dca_deployed_pct"] > 0.0

    def test_record_close_dca_reduces_dca_deployed(self, checker: BudgetChecker):
        checker.record_order(0.15, is_dca=True)
        checker.record_close(0.15, is_dca=True)
        snap = checker.snapshot()
        assert snap["dca_deployed_pct"] == pytest.approx(0.0, abs=1e-9)

    def test_reset_daily_clears_intraday(self, checker: BudgetChecker):
        checker.record_order(0.20)
        checker.reset_daily()
        snap = checker.snapshot()
        assert snap["intraday_used_pct"] == pytest.approx(0.0)

    def test_reset_daily_keeps_deployed(self, checker: BudgetChecker):
        """日内重置不应该清空仓位（仓位还在）。"""
        checker.record_order(0.20)
        checker.reset_daily()
        snap = checker.snapshot()
        assert snap["deployed_pct"] > 0.0

    def test_remaining_budget_pct_correct(self, checker: BudgetChecker):
        cfg = BudgetConfig(
            max_budget_usage_pct=0.80,
            fee_reserve_pct=0.0,
            slippage_reserve_pct=0.0,
        )
        c = BudgetChecker(config=cfg)
        c.record_order(0.20)
        remaining = c.remaining_budget_pct
        assert abs(remaining - 0.60) < 0.01

    def test_state_persisted_and_restored(self, tmp_path: Path):
        """状态应该在 StateStore 中持久化并可恢复。"""
        store = StateStore(path=tmp_path / "risk_state.json")
        cfg = BudgetConfig(fee_reserve_pct=0.0, slippage_reserve_pct=0.0)
        c1 = BudgetChecker(config=cfg, state_store=store)
        c1.record_order(0.25)
        deployed_before = c1.snapshot()["deployed_pct"]

        # 模拟重启：重新创建 BudgetChecker 读取同一 store
        c2 = BudgetChecker(config=cfg, state_store=store)
        deployed_after = c2.snapshot()["deployed_pct"]
        assert abs(deployed_before - deployed_after) < 1e-6

    def test_snapshot_has_required_keys(self, checker: BudgetChecker):
        snap = checker.snapshot()
        for key in ("deployed_pct", "dca_deployed_pct", "remaining_budget_pct",
                    "max_budget_usage_pct", "intraday_used_pct"):
            assert key in snap

    def test_reset_all_zeros_state(self, checker: BudgetChecker):
        checker.record_order(0.30)
        checker.reset_all()
        snap = checker.snapshot()
        assert snap["deployed_pct"] == pytest.approx(0.0)
        assert snap["dca_deployed_pct"] == pytest.approx(0.0)


# ─────────────────────────────────────────────────────────────
# KillSwitch 测试
# ─────────────────────────────────────────────────────────────

class TestKillSwitch:
    def test_not_active_by_default(self, ks: KillSwitch):
        assert ks.is_active is False

    def test_drawdown_triggers_kill_switch(self):
        cfg = KillSwitchConfig(drawdown_trigger=0.10)
        ks = KillSwitch(config=cfg)
        snap = make_snapshot(drawdown=0.12)
        result = ks.evaluate(risk_snapshot=snap)
        assert result is True
        assert ks.is_active is True
        assert "回撤" in ks._state.reason

    def test_daily_loss_triggers_kill_switch(self):
        cfg = KillSwitchConfig(daily_loss_trigger=0.03)
        ks = KillSwitch(config=cfg)
        snap = make_snapshot(daily_loss=0.04)
        result = ks.evaluate(risk_snapshot=snap)
        assert result is True
        assert ks.is_active is True
        assert "日内" in ks._state.reason

    def test_below_threshold_not_triggered(self, ks: KillSwitch):
        snap = make_snapshot(drawdown=0.05, daily_loss=0.01)
        result = ks.evaluate(risk_snapshot=snap)
        assert result is False

    def test_consecutive_rejections_trigger(self):
        cfg = KillSwitchConfig(max_consecutive_rejections=3)
        ks = KillSwitch(config=cfg)
        for _ in range(3):
            ks.record_order_rejection("测试拒绝")
        result = ks.evaluate()
        assert result is True
        assert "拒绝" in ks._state.reason

    def test_consecutive_failures_trigger(self):
        cfg = KillSwitchConfig(max_consecutive_failures=2)
        ks = KillSwitch(config=cfg)
        for _ in range(2):
            ks.record_order_failure("交易所错误")
        result = ks.evaluate()
        assert result is True
        assert "失败" in ks._state.reason

    def test_order_success_resets_counters(self):
        cfg = KillSwitchConfig(max_consecutive_rejections=5)
        ks = KillSwitch(config=cfg)
        for _ in range(3):
            ks.record_order_rejection()
        ks.record_order_success()
        assert ks._state.consecutive_rejections == 0
        assert ks._state.consecutive_failures == 0

    def test_stale_data_source_triggers(self):
        cfg = KillSwitchConfig(
            stale_data_timeout_sec=0,   # 立即 stale
            stale_sources_trigger_count=2,
        )
        ks = KillSwitch(config=cfg)
        ks.record_data_health("onchain", is_fresh=False)
        ks.record_data_health("sentiment", is_fresh=False)
        result = ks.evaluate()
        assert result is True
        assert "stale" in ks._state.reason.lower() or "数据源" in ks._state.reason

    def test_fresh_data_source_not_counted(self, ks: KillSwitch):
        """新鲜的数据源不应该被算入 stale 计数。"""
        ks.record_data_health("technical", is_fresh=True)
        result = ks.evaluate()
        assert result is False

    def test_manual_activate(self, ks: KillSwitch):
        ks.manual_activate("人工紧急停机")
        assert ks.is_active is True
        assert "人工" in ks._state.reason

    def test_manual_reset_deactivates(self, ks: KillSwitch):
        ks.manual_activate("紧急情况")
        ks.manual_reset("已人工确认恢复")
        assert ks.is_active is False

    def test_auto_recover_requires_manual_for_manual_activate(self):
        """手动激活 + manual_activate_requires_manual_reset=True → 自动恢复应失败。"""
        cfg = KillSwitchConfig(
            manual_activate_requires_manual_reset=True,
            auto_recover_minutes=0,  # 冷却 0 分钟
        )
        ks = KillSwitch(config=cfg)
        ks.manual_activate("人工激活")
        # 即便冷却时间已到，手动激活不允许自动恢复
        recovered = ks.try_auto_recover()
        assert recovered is False
        assert ks.is_active is True

    def test_auto_recover_after_cooldown(self):
        """非手动激活 + 冷却期满 → 自动恢复。"""
        cfg = KillSwitchConfig(
            drawdown_trigger=0.05,
            auto_recover_minutes=0,  # 冷却期设为 0 分钟（测试用）
        )
        ks = KillSwitch(config=cfg)
        # 触发自动激活
        ks.evaluate(risk_snapshot=make_snapshot(drawdown=0.06))
        assert ks.is_active is True
        # 立即尝试恢复（auto_recover_minutes=0 → 冷却期已过）
        recovered = ks.try_auto_recover()
        assert recovered is True
        assert ks.is_active is False

    def test_state_persisted_on_activate(self, tmp_path: Path):
        """激活后状态应持久化到 StateStore。"""
        store = StateStore(path=tmp_path / "ks_state.json")
        cfg = KillSwitchConfig(drawdown_trigger=0.05)
        ks1 = KillSwitch(config=cfg, state_store=store)
        ks1.evaluate(risk_snapshot=make_snapshot(drawdown=0.06))
        assert ks1.is_active is True

        # 模拟重启
        ks2 = KillSwitch(config=cfg, state_store=store)
        assert ks2.is_active is True
        assert "回撤" in ks2._state.reason

    def test_health_snapshot_structure(self, ks: KillSwitch):
        snap = ks.health_snapshot()
        required = (
            "active", "reason", "activated_at", "auto_recover_at",
            "manual_activated", "consecutive_rejections",
            "consecutive_failures", "stale_source_count",
            "stale_source_names", "config_version",
        )
        for key in required:
            assert key in snap

    def test_evaluate_already_active_skips_checks(self):
        """已激活时 evaluate() 不再重复评估，直接返回 True。"""
        cfg = KillSwitchConfig(drawdown_trigger=0.10, auto_recover_minutes=999)
        ks = KillSwitch(config=cfg)
        ks.manual_activate("先手动激活")
        # 即便 snapshot 正常也不应该解除
        result = ks.evaluate(risk_snapshot=make_snapshot(drawdown=0.0))
        assert result is True

    def test_no_snapshot_only_checks_counters(self, ks: KillSwitch):
        """不传 risk_snapshot 时只检查计数器和数据源。"""
        result = ks.evaluate(risk_snapshot=None)
        assert result is False  # 没有计数器触发，应该不激活
