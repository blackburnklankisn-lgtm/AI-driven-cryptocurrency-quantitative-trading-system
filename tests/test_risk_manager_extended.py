"""
tests/test_risk_manager_extended.py — 补充覆盖 modules/risk/manager.py 缺失分支

Target: lines 140-141, 229-232, 242-249, 261-267, 295, 320, 363
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from modules.risk.manager import RiskManager, RiskConfig, RiskState


def _default_manager(**kwargs) -> RiskManager:
    cfg = RiskConfig(**kwargs)
    return RiskManager(config=cfg)


class TestRiskManagerSellBranches:

    def test_sell_circuit_broken_allowed(self):
        """熔断时卖出(减仓)仍允许。"""
        rm = _default_manager()
        rm._state.circuit_broken = True
        rm._state.circuit_reason = "test"
        ok, reason = rm.check(
            side="sell",
            symbol="BTC/USDT",
            quantity=Decimal("0.1"),
            price=30000.0,
            current_equity=10000.0,
            positions={"BTC/USDT": Decimal("0.1")},
        )
        assert ok is True

    def test_sell_blacklisted_rejected(self):
        """卖出黑名单标的时被拒绝。"""
        rm = _default_manager(blacklist=["BTC/USDT"])
        ok, reason = rm.check(
            side="sell",
            symbol="BTC/USDT",
            quantity=Decimal("0.1"),
            price=30000.0,
            current_equity=10000.0,
            positions={},
        )
        assert ok is False
        assert "黑名单" in reason

    def test_sell_blacklisted_during_circuit_break_rejected(self):
        """熔断 + 黑名单卖出仍然被拒。"""
        rm = _default_manager(blacklist=["XRP/USDT"])
        rm._state.circuit_broken = True
        ok, reason = rm.check(
            side="sell",
            symbol="XRP/USDT",
            quantity=Decimal("100"),
            price=1.0,
            current_equity=10000.0,
            positions={},
        )
        assert ok is False


class TestPortfolioDrawdownCheck:

    def test_buy_rejected_by_portfolio_drawdown(self):
        """组合回撤超阈值时买入被拒。"""
        rm = _default_manager(max_portfolio_drawdown=0.10)
        # peak = 10000, current = 8000 → drawdown = 20% > 10%
        rm._state.peak_equity = Decimal("10000")
        rm._state.daily_start_equity = Decimal("10000")
        ok, reason = rm.check(
            side="buy",
            symbol="BTC/USDT",
            quantity=Decimal("0.01"),
            price=8000.0,
            current_equity=8000.0,
            positions={},
        )
        assert ok is False
        assert "回撤" in reason

    def test_buy_allowed_under_portfolio_drawdown(self):
        """回撤未超阈值时不触发组合回撤拒绝。"""
        rm = _default_manager(max_portfolio_drawdown=0.30)
        rm._state.peak_equity = Decimal("10000")
        rm._state.daily_start_equity = Decimal("10000")
        ok, reason = rm.check(
            side="buy",
            symbol="BTC/USDT",
            quantity=Decimal("0.001"),
            price=9500.0,
            current_equity=9500.0,
            positions={},
        )
        assert ok is True

    def test_check_portfolio_drawdown_zero_peak(self):
        """peak_equity=0 时不触发回撤检查（避免除零）。"""
        rm = _default_manager(max_portfolio_drawdown=0.10)
        rm._state.peak_equity = Decimal("0")
        ok, reason = rm._check_portfolio_drawdown(Decimal("1000"))
        assert ok is True


class TestUpdateEquityCircuitBreaker:

    def test_portfolio_drawdown_triggers_circuit_breaker(self):
        """净值下跌超最大回撤时应自动触发熔断。"""
        rm = _default_manager(max_portfolio_drawdown=0.15)
        rm.update_equity(10000.0)  # sets peak to 10000
        rm.update_equity(8400.0)  # 16% drawdown > 15%
        assert rm.is_circuit_broken()
        assert "回撤" in rm._state.circuit_reason or "熔断" in rm._state.circuit_reason

    def test_daily_loss_triggers_circuit_breaker(self):
        """单日亏损超限应触发熔断。"""
        rm = _default_manager(max_daily_loss=0.05)
        rm._state.daily_start_equity = Decimal("10000")
        rm._state.peak_equity = Decimal("10000")
        # update to equity below daily loss threshold
        rm.update_equity(9400.0)  # 6% daily loss > 5%
        assert rm.is_circuit_broken()

    def test_circuit_breaker_auto_reset_after_cooldown(self, monkeypatch):
        """冷却期过后 update_equity 自动解除熔断。"""
        rm = _default_manager(
            max_portfolio_drawdown=0.10,
            circuit_breaker_cooldown_minutes=30,
        )
        # Manually set circuit broken with old timestamp
        rm._state.circuit_broken = True
        rm._state.circuit_broken_at = datetime.now(tz=timezone.utc) - timedelta(minutes=31)
        rm._state.circuit_reason = "test"
        rm._state.peak_equity = Decimal("10000")
        rm._state.daily_start_equity = Decimal("10000")
        rm._state.daily_pnl = Decimal("0")

        rm.update_equity(10000.0)
        assert not rm.is_circuit_broken()

    def test_peak_equity_does_not_update_when_circuit_broken(self):
        """熔断中不应上修 peak_equity。"""
        rm = _default_manager()
        rm._state.peak_equity = Decimal("10000")
        rm._state.circuit_broken = True
        rm._state.daily_start_equity = Decimal("10000")
        rm.update_equity(11000.0)
        # peak should still be 10000
        assert rm._state.peak_equity == Decimal("10000")


class TestRestoreState:

    def test_restore_state_sets_values(self):
        rm = _default_manager()
        rm.restore_state(
            peak_equity=15000.0,
            daily_start_equity=14000.0,
            consecutive_losses=2,
        )
        assert float(rm._state.peak_equity) == 15000.0
        assert float(rm._state.daily_start_equity) == 14000.0
        assert rm._state.consecutive_losses == 2
        assert not rm._state.circuit_broken

    def test_restore_state_clears_circuit_breaker(self):
        rm = _default_manager()
        rm._state.circuit_broken = True
        rm.restore_state(peak_equity=10000.0, daily_start_equity=10000.0)
        assert not rm._state.circuit_broken
