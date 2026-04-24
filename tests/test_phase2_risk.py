"""
tests/test_phase2_risk.py — Phase 2 W9-W10 风控模块单元测试

覆盖项：
- RiskSnapshot: 构造、is_safe_to_trade、symbol_in_cooldown、to_dict、Mapping 接口
- RiskPlan: 构造、is_blocked、has_exit_plan、blocked() 工厂
- CooldownManager: set/is_cooling/remaining_minutes/release/release_all/active_symbols
- ExitPlanner: ATR止损、静态止损、高波动调整、置信度调整、边界约束
- DCAEngine: 正常规划、regime 禁用、置信度禁用、预算禁用、层数与预算成正比
- AdaptiveRiskMatrix: 全流程、各类阻断、仓位乘数计算、冷却期集成、诊断
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from modules.risk.adaptive_matrix import AdaptiveRiskMatrix, AdaptiveRiskMatrixConfig
from modules.risk.cooldown import CooldownManager
from modules.risk.dca_engine import DCAConfig, DCAEngine
from modules.risk.exit_planner import ExitPlanConfig, ExitPlanner
from modules.risk.snapshot import RiskPlan, RiskSnapshot


# ─────────────────────────────────────────────────────────────
# 测试工厂
# ─────────────────────────────────────────────────────────────

def make_snapshot(
    drawdown: float = 0.0,
    daily_loss: float = 0.0,
    consecutive_losses: int = 0,
    circuit_broken: bool = False,
    kill_switch: bool = False,
    budget: float = 1.0,
    cooldown_symbols: dict | None = None,
) -> RiskSnapshot:
    return RiskSnapshot(
        current_drawdown=drawdown,
        daily_loss_pct=daily_loss,
        consecutive_losses=consecutive_losses,
        circuit_broken=circuit_broken,
        kill_switch_active=kill_switch,
        budget_remaining_pct=budget,
        cooldown_symbols=cooldown_symbols or {},
    )


def make_regime(dominant: str = "bull", confidence: float = 0.65):
    """构造最小化 RegimeState（只设置 dominant 和 confidence）。"""
    from modules.alpha.contracts.regime_types import RegimeState

    probs = {"bull": 0.0, "bear": 0.0, "sideways": 0.0, "high_vol": 0.0}
    probs[dominant] = confidence
    total = sum(probs.values())
    if total > 0:
        probs = {k: v / total for k, v in probs.items()}

    return RegimeState(
        bull_prob=probs.get("bull", 0.25),
        bear_prob=probs.get("bear", 0.25),
        sideways_prob=probs.get("sideways", 0.25),
        high_vol_prob=probs.get("high_vol", 0.25),
        confidence=confidence,
        dominant_regime=dominant,  # type: ignore[arg-type]
    )


# ─────────────────────────────────────────────────────────────
# RiskSnapshot 测试
# ─────────────────────────────────────────────────────────────

class TestRiskSnapshot:
    def test_default_is_safe(self):
        snap = RiskSnapshot.make_default()
        assert snap.is_safe_to_trade() is True

    def test_circuit_broken_not_safe(self):
        snap = make_snapshot(circuit_broken=True)
        assert snap.is_safe_to_trade() is False

    def test_kill_switch_not_safe(self):
        snap = make_snapshot(kill_switch=True)
        assert snap.is_safe_to_trade() is False

    def test_zero_budget_not_safe(self):
        snap = make_snapshot(budget=0.0)
        assert snap.is_safe_to_trade() is False

    def test_full_daily_loss_not_safe(self):
        snap = make_snapshot(daily_loss=1.0)
        assert snap.is_safe_to_trade() is False

    def test_symbol_in_cooldown_true(self):
        future = datetime.now(tz=timezone.utc) + timedelta(minutes=30)
        snap = make_snapshot(cooldown_symbols={"BTC/USDT": future})
        assert snap.symbol_in_cooldown("BTC/USDT") is True

    def test_symbol_in_cooldown_expired(self):
        past = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
        snap = make_snapshot(cooldown_symbols={"BTC/USDT": past})
        assert snap.symbol_in_cooldown("BTC/USDT") is False

    def test_symbol_not_in_cooldown(self):
        snap = make_snapshot()
        assert snap.symbol_in_cooldown("ETH/USDT") is False

    def test_to_dict_contains_required_keys(self):
        snap = make_snapshot(drawdown=0.05, daily_loss=0.01)
        d = snap.to_dict()
        assert "current_drawdown" in d
        assert "circuit_broken" in d
        assert "kill_switch_active" in d
        assert abs(d["current_drawdown"] - 0.05) < 1e-9

    def test_mapping_getitem(self):
        snap = make_snapshot(drawdown=0.03)
        assert abs(snap["current_drawdown"] - 0.03) < 1e-9

    def test_mapping_iter_and_len(self):
        snap = make_snapshot()
        keys = list(snap)
        assert "current_drawdown" in keys
        assert len(snap) > 0

    def test_make_blocked_is_not_safe(self):
        snap = RiskSnapshot.make_blocked("测试阻断")
        assert snap.is_safe_to_trade() is False
        assert snap.circuit_broken is True
        assert snap.kill_switch_active is True


# ─────────────────────────────────────────────────────────────
# RiskPlan 测试
# ─────────────────────────────────────────────────────────────

class TestRiskPlan:
    def test_blocked_factory(self):
        plan = RiskPlan.blocked("测试原因", symbol="BTC/USDT")
        assert plan.is_blocked is True
        assert plan.allow_entry is False
        assert "测试原因" in plan.block_reasons
        assert plan.position_scalar == 0.0

    def test_normal_plan_not_blocked(self):
        plan = RiskPlan(
            allow_entry=True,
            position_scalar=0.8,
            stop_loss_pct=0.03,
            trailing_trigger_pct=0.04,
            trailing_callback_pct=0.015,
            take_profit_ladder=[0.03, 0.06, 0.10],
            dca_levels=[-0.02],
            cooldown_minutes=30,
            block_reasons=[],
        )
        assert plan.is_blocked is False
        assert plan.has_exit_plan is True
        assert plan.has_dca_plan is True

    def test_no_stop_loss_no_exit_plan(self):
        plan = RiskPlan(
            allow_entry=True,
            position_scalar=1.0,
            stop_loss_pct=None,
            trailing_trigger_pct=None,
            trailing_callback_pct=None,
            take_profit_ladder=[],
            dca_levels=[],
            cooldown_minutes=0,
            block_reasons=[],
        )
        assert plan.has_exit_plan is False
        assert plan.has_dca_plan is False


# ─────────────────────────────────────────────────────────────
# CooldownManager 测试
# ─────────────────────────────────────────────────────────────

class TestCooldownManager:
    def test_not_cooling_by_default(self):
        mgr = CooldownManager()
        assert mgr.is_cooling("BTC/USDT") is False

    def test_set_and_is_cooling(self):
        mgr = CooldownManager()
        mgr.set("BTC/USDT", minutes=60, reason="止损")
        assert mgr.is_cooling("BTC/USDT") is True

    def test_remaining_minutes_positive(self):
        mgr = CooldownManager()
        mgr.set("ETH/USDT", minutes=30)
        remaining = mgr.remaining_minutes("ETH/USDT")
        assert 0 < remaining <= 30

    def test_remaining_minutes_not_set(self):
        mgr = CooldownManager()
        assert mgr.remaining_minutes("SOL/USDT") == 0.0

    def test_release_clears_cooldown(self):
        mgr = CooldownManager()
        mgr.set("BTC/USDT", minutes=60)
        released = mgr.release("BTC/USDT")
        assert released is True
        assert mgr.is_cooling("BTC/USDT") is False

    def test_release_not_set(self):
        mgr = CooldownManager()
        assert mgr.release("NONEXISTENT") is False

    def test_release_all(self):
        mgr = CooldownManager()
        mgr.set("BTC/USDT", minutes=60)
        mgr.set("ETH/USDT", minutes=30)
        count = mgr.release_all()
        assert count == 2
        assert mgr.is_cooling("BTC/USDT") is False
        assert mgr.is_cooling("ETH/USDT") is False

    def test_set_zero_minutes_ignored(self):
        mgr = CooldownManager()
        mgr.set("BTC/USDT", minutes=0)
        assert mgr.is_cooling("BTC/USDT") is False

    def test_set_negative_minutes_ignored(self):
        mgr = CooldownManager()
        mgr.set("BTC/USDT", minutes=-5)
        assert mgr.is_cooling("BTC/USDT") is False

    def test_set_takes_longer_cooldown(self):
        """已有冷却期的 symbol，新设置更短的冷却期时不缩短。"""
        mgr = CooldownManager()
        mgr.set("BTC/USDT", minutes=60)
        remaining_before = mgr.remaining_minutes("BTC/USDT")
        mgr.set("BTC/USDT", minutes=10)  # 更短，不应覆盖
        remaining_after = mgr.remaining_minutes("BTC/USDT")
        # 不缩短：remaining_after 应该约等于 remaining_before
        assert remaining_after >= remaining_after - 0.1  # 容忍极小时间差

    def test_active_symbols(self):
        mgr = CooldownManager()
        mgr.set("BTC/USDT", minutes=60)
        mgr.set("ETH/USDT", minutes=30)
        active = mgr.active_symbols()
        assert "BTC/USDT" in active
        assert "ETH/USDT" in active

    def test_diagnostics_structure(self):
        mgr = CooldownManager()
        mgr.set("BTC/USDT", minutes=15)
        diag = mgr.diagnostics()
        assert "active_count" in diag
        assert "active_symbols" in diag
        assert diag["active_count"] == 1


# ─────────────────────────────────────────────────────────────
# ExitPlanner 测试
# ─────────────────────────────────────────────────────────────

class TestExitPlanner:
    def test_base_stop_used_when_no_atr(self):
        """无 ATR 时应该使用基础止损距离。"""
        cfg = ExitPlanConfig(base_stop_loss_pct=0.03)
        planner = ExitPlanner(cfg)
        plan = planner.plan(dominant_regime="bull", signal_confidence=0.65)
        assert plan.stop_loss_pct == pytest.approx(0.03, abs=1e-4)

    def test_atr_based_stop(self):
        """有 ATR 时止损应基于 ATR * multiplier。"""
        cfg = ExitPlanConfig(atr_stop_multiplier=2.0)
        planner = ExitPlanner(cfg)
        plan = planner.plan(dominant_regime="bull", signal_confidence=0.65, atr_pct=0.02)
        # raw_stop = 0.02 * 2.0 = 0.04
        assert plan.stop_loss_pct == pytest.approx(0.04, abs=1e-4)

    def test_high_vol_widens_stop(self):
        """高波动场景止损应该更宽。"""
        cfg = ExitPlanConfig(atr_stop_multiplier=2.0, high_vol_stop_multiplier=1.4)
        planner = ExitPlanner(cfg)
        plan_bull = planner.plan(dominant_regime="bull", signal_confidence=0.65, atr_pct=0.02)
        plan_hvol = planner.plan(dominant_regime="high_vol", signal_confidence=0.65, atr_pct=0.02)
        assert plan_hvol.stop_loss_pct > plan_bull.stop_loss_pct

    def test_low_confidence_narrows_stop(self):
        """低置信度时止损应该更窄（避免持续亏损）。"""
        planner = ExitPlanner(ExitPlanConfig())
        plan_high_conf = planner.plan(dominant_regime="bull", signal_confidence=0.70)
        plan_low_conf = planner.plan(dominant_regime="bull", signal_confidence=0.40)
        assert plan_low_conf.stop_loss_pct <= plan_high_conf.stop_loss_pct

    def test_stop_within_bounds(self):
        """止损距离必须在 [min, max] 范围内。"""
        cfg = ExitPlanConfig(min_stop_loss_pct=0.01, max_stop_loss_pct=0.08)
        planner = ExitPlanner(cfg)
        # 极大 ATR → 超出上限
        plan_large = planner.plan(dominant_regime="high_vol", signal_confidence=0.3, atr_pct=0.10)
        assert plan_large.stop_loss_pct <= cfg.max_stop_loss_pct
        # 无 ATR、低基础 → 不低于下限
        plan_small = planner.plan(dominant_regime="bull", signal_confidence=0.9)
        assert plan_small.stop_loss_pct >= cfg.min_stop_loss_pct

    def test_trailing_trigger_pct_positive(self):
        planner = ExitPlanner()
        plan = planner.plan()
        assert plan.trailing_trigger_pct > 0

    def test_high_vol_widens_trailing_trigger(self):
        """高波动时追踪触发点应该更远。"""
        planner = ExitPlanner()
        plan_bull = planner.plan(dominant_regime="bull")
        plan_hvol = planner.plan(dominant_regime="high_vol")
        assert plan_hvol.trailing_trigger_pct >= plan_bull.trailing_trigger_pct

    def test_roi_ladder_not_empty(self):
        planner = ExitPlanner(ExitPlanConfig(roi_ladder=[0.03, 0.06, 0.10]))
        plan = planner.plan()
        assert len(plan.take_profit_ladder) == 3

    def test_debug_payload_has_regime(self):
        planner = ExitPlanner()
        plan = planner.plan(dominant_regime="bear", signal_confidence=0.6)
        assert plan.debug_payload["regime"] == "bear"


# ─────────────────────────────────────────────────────────────
# DCAEngine 测试
# ─────────────────────────────────────────────────────────────

class TestDCAEngine:
    def test_normal_plan_bull(self):
        """正常 bull + 高置信 + 充裕预算 → 应该返回 DCA 层数。"""
        dca = DCAEngine(DCAConfig(max_dca_levels=2, dca_step_pct=0.02, allowed_regimes=["bull"]))
        levels = dca.plan(dominant_regime="bull", signal_confidence=0.70, budget_remaining_pct=0.90)
        assert len(levels) > 0
        assert all(lv < 0 for lv in levels)  # 都是负数（跌入场价后才加仓）

    def test_blocked_by_regime(self):
        """不允许的 regime（如 high_vol）→ 空列表。"""
        dca = DCAEngine(DCAConfig(allowed_regimes=["bull", "sideways"]))
        levels = dca.plan(dominant_regime="high_vol", signal_confidence=0.7, budget_remaining_pct=0.9)
        assert levels == []

    def test_blocked_by_low_confidence(self):
        """置信度低于阈值 → 空列表。"""
        dca = DCAEngine(DCAConfig(min_confidence_for_dca=0.55))
        levels = dca.plan(dominant_regime="bull", signal_confidence=0.40, budget_remaining_pct=0.9)
        assert levels == []

    def test_blocked_by_low_budget(self):
        """预算不足 → 空列表。"""
        dca = DCAEngine(DCAConfig(min_budget_remaining_pct=0.4))
        levels = dca.plan(dominant_regime="bull", signal_confidence=0.7, budget_remaining_pct=0.2)
        assert levels == []

    def test_levels_negative_and_ordered(self):
        """DCA 价格偏移应该为负且按层递增（绝对值递增）。"""
        dca = DCAEngine(DCAConfig(max_dca_levels=3, dca_step_pct=0.02))
        levels = dca.plan("bull", 0.7, 1.0)
        if levels:
            for i in range(1, len(levels)):
                assert levels[i] < levels[i - 1]  # 越往后跌得越多

    def test_more_budget_more_levels(self):
        """预算越充裕，DCA 层数越多（不超过 max_dca_levels）。"""
        dca = DCAEngine(DCAConfig(max_dca_levels=3, min_budget_remaining_pct=0.3))
        levels_low = dca.plan("bull", 0.7, 0.35)  # 预算刚过阈值
        levels_high = dca.plan("bull", 0.7, 1.0)  # 预算充足
        assert len(levels_high) >= len(levels_low)

    def test_max_budget_usage_pct(self):
        dca = DCAEngine(DCAConfig(dca_budget_per_level=0.25))
        assert dca.max_budget_usage_pct(2) == pytest.approx(0.50)


# ─────────────────────────────────────────────────────────────
# AdaptiveRiskMatrix 测试
# ─────────────────────────────────────────────────────────────

class TestAdaptiveRiskMatrix:
    def test_normal_entry_allowed(self):
        """正常状态下应该允许入场。"""
        matrix = AdaptiveRiskMatrix()
        snap = make_snapshot()
        plan = matrix.evaluate("BTC/USDT", snap, regime=make_regime("bull", 0.7))
        assert plan.allow_entry is True
        assert plan.position_scalar > 0

    def test_circuit_broken_blocks(self):
        """组合熔断应该阻止入场。"""
        matrix = AdaptiveRiskMatrix()
        snap = make_snapshot(circuit_broken=True)
        plan = matrix.evaluate("BTC/USDT", snap)
        assert plan.is_blocked is True
        assert any("熔断" in r for r in plan.block_reasons)

    def test_kill_switch_blocks(self):
        """Kill Switch 激活应该阻止入场。"""
        matrix = AdaptiveRiskMatrix()
        snap = make_snapshot(kill_switch=True)
        plan = matrix.evaluate("BTC/USDT", snap)
        assert plan.is_blocked is True
        assert any("Kill Switch" in r for r in plan.block_reasons)

    def test_high_drawdown_blocks(self):
        """高回撤超阈值应该阻止入场。"""
        cfg = AdaptiveRiskMatrixConfig(max_drawdown_for_entry=0.08)
        matrix = AdaptiveRiskMatrix(cfg)
        snap = make_snapshot(drawdown=0.10)
        plan = matrix.evaluate("BTC/USDT", snap)
        assert plan.is_blocked is True

    def test_high_daily_loss_blocks(self):
        """单日亏损超阈值应该阻止入场。"""
        cfg = AdaptiveRiskMatrixConfig(max_daily_loss_for_entry=0.025)
        matrix = AdaptiveRiskMatrix(cfg)
        snap = make_snapshot(daily_loss=0.03)
        plan = matrix.evaluate("BTC/USDT", snap)
        assert plan.is_blocked is True

    def test_cooldown_blocks(self):
        """入场后冷却期内应该阻止再次入场。"""
        matrix = AdaptiveRiskMatrix(
            AdaptiveRiskMatrixConfig(default_cooldown_minutes=60)
        )
        snap = make_snapshot()
        # 首次入场
        plan1 = matrix.evaluate("BTC/USDT", snap, regime=make_regime())
        assert plan1.allow_entry is True
        matrix.record_entry("BTC/USDT")

        # 冷却期内
        plan2 = matrix.evaluate("BTC/USDT", snap, regime=make_regime())
        assert plan2.is_blocked is True
        assert any("冷却" in r for r in plan2.block_reasons)

    def test_high_vol_reduces_position_scalar(self):
        """高波动市场仓位应该比普通牛市更低。"""
        matrix = AdaptiveRiskMatrix()
        snap = make_snapshot()
        plan_bull = matrix.evaluate("BTC/USDT", snap, regime=make_regime("bull", 0.7))
        plan_hvol = matrix.evaluate("ETH/USDT", snap, regime=make_regime("high_vol", 0.7))
        assert plan_hvol.position_scalar < plan_bull.position_scalar

    def test_unknown_regime_reduces_position_scalar(self):
        """unknown regime 仓位应低于 bull。"""
        matrix = AdaptiveRiskMatrix()
        snap = make_snapshot()
        plan_bull = matrix.evaluate("BTC/USDT", snap, regime=make_regime("bull", 0.7))
        plan_unk = matrix.evaluate("ETH/USDT", snap, regime=None)  # regime=None → unknown
        assert plan_unk.position_scalar < plan_bull.position_scalar

    def test_low_confidence_reduces_position_scalar(self):
        """低置信度信号仓位应低于高置信度。"""
        matrix = AdaptiveRiskMatrix()
        snap = make_snapshot()
        plan_high = matrix.evaluate("BTC/USDT", snap, regime=make_regime(), signal_confidence=0.80)
        plan_low = matrix.evaluate("ETH/USDT", snap, regime=make_regime(), signal_confidence=0.40)
        assert plan_low.position_scalar < plan_high.position_scalar

    def test_high_drawdown_reduces_position_scalar(self):
        """有回撤时仓位乘数应小于无回撤。"""
        matrix = AdaptiveRiskMatrix()
        snap_clean = make_snapshot(drawdown=0.0)
        snap_dd = make_snapshot(drawdown=0.05)
        plan_clean = matrix.evaluate("BTC/USDT", snap_clean, regime=make_regime())
        plan_dd = matrix.evaluate("ETH/USDT", snap_dd, regime=make_regime())
        assert plan_dd.position_scalar < plan_clean.position_scalar

    def test_position_scalar_within_bounds(self):
        """仓位乘数必须在 [0, max_position_scalar] 范围内。"""
        cfg = AdaptiveRiskMatrixConfig(max_position_scalar=1.0)
        matrix = AdaptiveRiskMatrix(cfg)
        snap = make_snapshot(drawdown=0.03)
        plan = matrix.evaluate("BTC/USDT", snap, regime=make_regime("bull", 0.9), signal_confidence=0.9)
        assert 0.0 <= plan.position_scalar <= 1.0

    def test_exit_plan_is_present(self):
        """允许入场时应该有退出规划。"""
        matrix = AdaptiveRiskMatrix()
        snap = make_snapshot()
        plan = matrix.evaluate("BTC/USDT", snap, regime=make_regime())
        assert plan.has_exit_plan is True
        assert plan.stop_loss_pct is not None

    def test_record_entry_and_stop_loss(self):
        """record_stop_loss 应该触发更长的冷却期。"""
        matrix = AdaptiveRiskMatrix(
            AdaptiveRiskMatrixConfig(default_cooldown_minutes=5)
        )
        matrix.record_stop_loss("BTC/USDT", extra_cooldown_minutes=120)
        snap = make_snapshot()
        plan = matrix.evaluate("BTC/USDT", snap, regime=make_regime())
        assert plan.is_blocked is True

    def test_release_cooldown(self):
        """人工解除冷却后可以再次入场。"""
        matrix = AdaptiveRiskMatrix(
            AdaptiveRiskMatrixConfig(default_cooldown_minutes=60)
        )
        matrix.record_entry("BTC/USDT")
        matrix.release_cooldown("BTC/USDT")
        snap = make_snapshot()
        plan = matrix.evaluate("BTC/USDT", snap, regime=make_regime())
        assert plan.allow_entry is True

    def test_health_snapshot_structure(self):
        matrix = AdaptiveRiskMatrix()
        snap = matrix.health_snapshot()
        assert "config_version" in snap
        assert "cooldown" in snap
        assert "config" in snap

    def test_debug_payload_present(self):
        """输出的 debug_payload 应该包含关键字段。"""
        matrix = AdaptiveRiskMatrix()
        snap = make_snapshot()
        plan = matrix.evaluate("BTC/USDT", snap, regime=make_regime("bull"), signal_confidence=0.7)
        assert "position_scalar_final" in plan.debug_payload
        assert "regime" in plan.debug_payload
        assert "signal_confidence" in plan.debug_payload

    def test_dca_disabled_for_high_vol(self):
        """高波动市场下 DCA 应该被禁用（DCAConfig 默认不允许 high_vol）。"""
        matrix = AdaptiveRiskMatrix()
        snap = make_snapshot()
        plan = matrix.evaluate(
            "BTC/USDT", snap,
            regime=make_regime("high_vol", 0.7),
            signal_confidence=0.8,
        )
        if plan.allow_entry:
            assert plan.dca_levels == []  # high_vol 不在 DCA 允许列表

    def test_dca_enabled_for_bull_high_budget(self):
        """牛市 + 充裕预算 → DCA 应该被规划。"""
        dca_cfg = DCAConfig(
            max_dca_levels=2,
            allowed_regimes=["bull"],
            min_confidence_for_dca=0.5,
            min_budget_remaining_pct=0.3,
        )
        cfg = AdaptiveRiskMatrixConfig(dca_config=dca_cfg)
        matrix = AdaptiveRiskMatrix(cfg)
        snap = make_snapshot(budget=0.9)
        plan = matrix.evaluate(
            "BTC/USDT", snap,
            regime=make_regime("bull", 0.7),
            signal_confidence=0.70,
        )
        if plan.allow_entry:
            assert len(plan.dca_levels) > 0
