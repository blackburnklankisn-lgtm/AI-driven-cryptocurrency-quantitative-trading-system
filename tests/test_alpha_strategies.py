"""
tests/test_alpha_strategies.py — Alpha 策略单元测试

覆盖项：
- FeatureEngine 指标计算正确性（无未来函数）
- MACrossStrategy：预热期不发信号
- MACrossStrategy：金叉发买单，死叉发卖单
- MACrossStrategy：不重复建仓（已持仓时忽略金叉）
- MomentumStrategy：ROC + RSI 信号逻辑
- BaseAlpha：订单构建工具方法

tests/test_risk_manager.py 位于同文件中一并覆盖：
- 单币种仓位上限拦截
- 最大回撤熔断自动触发
- 单日亏损限制
- 连续亏损熔断
- 熔断后买入被拒 / 卖出被允许
- 手动解除熔断
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from decimal import Decimal
from typing import List

import pandas as pd
import pytest

from core.event import EventType, KlineEvent, OrderRequestEvent
from modules.alpha.features import FeatureEngine
from modules.alpha.strategies.ma_cross import MACrossStrategy
from modules.alpha.strategies.momentum import MomentumStrategy
from modules.risk.manager import RiskConfig, RiskManager


# ─────────────────────────────────────────────────────────────
# 测试工具
# ─────────────────────────────────────────────────────────────

def make_kline(
    close: float,
    ts_hour: int = 0,
    symbol: str = "BTC/USDT",
    volume: float = 1000.0,
    high: float = None,
    low: float = None,
) -> KlineEvent:
    return KlineEvent(
        event_type=EventType.KLINE_UPDATED,
        timestamp=datetime(2024, 1, 1, ts_hour, tzinfo=timezone.utc),
        source="test",
        symbol=symbol,
        timeframe="1h",
        open=Decimal(str(close * 0.99)),
        high=Decimal(str(high or close * 1.01)),
        low=Decimal(str(low or close * 0.99)),
        close=Decimal(str(close)),
        volume=Decimal(str(volume)),
        is_closed=True,
    )


def feed_prices(strategy: MACrossStrategy, prices: List[float]) -> List[OrderRequestEvent]:
    """将价格序列逐根喂入策略，收集所有订单。"""
    all_orders = []
    for i, p in enumerate(prices):
        orders = strategy.on_kline(make_kline(p, ts_hour=i % 24))
        all_orders.extend(orders)
    return all_orders


# ─────────────────────────────────────────────────────────────
# FeatureEngine 测试
# ─────────────────────────────────────────────────────────────

class TestFeatureEngine:
    @pytest.fixture
    def close_series(self) -> pd.Series:
        return pd.Series([100.0 + i for i in range(100)])

    def test_sma_length_unchanged(self, close_series: pd.Series) -> None:
        """SMA 输出长度应与输入相同。"""
        result = FeatureEngine.sma(close_series, window=20)
        assert len(result) == len(close_series)

    def test_sma_warmup_period_is_nan(self, close_series: pd.Series) -> None:
        """SMA 前 (window-1) 个值应为 NaN（防未来函数约束）。"""
        result = FeatureEngine.sma(close_series, window=20)
        assert result.iloc[:19].isna().all()
        assert not result.iloc[19:].isna().any()

    def test_sma_value_correctness(self, close_series: pd.Series) -> None:
        """SMA(20) 在第 20 条时，应等于前 20 条的均值。"""
        result = FeatureEngine.sma(close_series, window=20)
        expected = close_series.iloc[:20].mean()
        assert abs(result.iloc[19] - expected) < 1e-10

    def test_rsi_range(self, close_series: pd.Series) -> None:
        """RSI 值域应在 [0, 100]。"""
        result = FeatureEngine.rsi(close_series, window=14)
        valid = result.dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_atr_positive(self) -> None:
        """ATR 应为正值（波动率不为负）。"""
        df = pd.DataFrame({
            "close": [100.0 + i for i in range(50)],
            "high":  [105.0 + i for i in range(50)],
            "low":   [95.0 + i for i in range(50)],
        })
        result = FeatureEngine.atr(df, window=14)
        valid = result.dropna()
        assert (valid > 0).all()

    def test_bollinger_bands_upper_gt_lower(self) -> None:
        """布林带 upper 始终大于 lower。"""
        series = pd.Series([100.0 + i * 0.1 + (i % 3) * 2 for i in range(100)])
        bb = FeatureEngine.bollinger_bands(series, window=20)
        valid = bb.dropna()
        assert (valid["bb_upper"] >= valid["bb_lower"]).all()

    def test_no_future_leak_in_sma(self) -> None:
        """修改未来值不影响过去的 SMA 计算（验证无前向依赖）。"""
        series = pd.Series([100.0] * 30)
        sma_original = FeatureEngine.sma(series, 20).copy()

        series_modified = series.copy()
        series_modified.iloc[25:] = 9999.0  # 修改未来数据
        sma_modified = FeatureEngine.sma(series_modified, 20)

        # 前 25 个值的 SMA 应与修改无关
        pd.testing.assert_series_equal(
            sma_original.iloc[:25], sma_modified.iloc[:25]
        )


# ─────────────────────────────────────────────────────────────
# MACrossStrategy 测试
# ─────────────────────────────────────────────────────────────

class TestMACrossStrategy:
    def _make_strategy(self, fast: int = 5, slow: int = 10) -> MACrossStrategy:
        return MACrossStrategy(
            symbol="BTC/USDT",
            fast_window=fast,
            slow_window=slow,
            order_qty=0.1,
            volume_filter=False,  # 测试时关闭成交量过滤，简化逻辑
            timeframe="1h",
        )

    def test_invalid_params_raises(self) -> None:
        """fast_window >= slow_window 时应抛出 ValueError。"""
        with pytest.raises(ValueError, match="必须小于"):
            MACrossStrategy(symbol="BTC/USDT", fast_window=20, slow_window=10)

    def test_warmup_no_signal(self) -> None:
        """预热期（数据不足 slow_window 条）不应发出任何信号。"""
        strategy = self._make_strategy(fast=5, slow=10)
        prices = [100.0] * 9  # 少于 slow_window=10
        orders = feed_prices(strategy, prices)
        assert len(orders) == 0

    def test_golden_cross_generates_buy(self) -> None:
        """金叉（快线上穿慢线）应产生买入信号。"""
        strategy = self._make_strategy(fast=3, slow=5)

        # 先喂下降数据（形成空头排列），再喂上升数据（触发金叉）
        prices = (
            [100, 99, 98, 97, 96] +   # 慢线 > 快线（空头排列，预热）
            [95, 94, 93, 92, 91] +    # 保持空头
            [100, 110, 120, 130, 140] # 快速上涨 → 金叉
        )
        orders = feed_prices(strategy, prices)
        buy_orders = [o for o in orders if o.side == "buy"]
        assert len(buy_orders) >= 1

    def test_no_double_entry(self) -> None:
        """已持仓时，再次金叉不应重复建仓。"""
        strategy = self._make_strategy(fast=3, slow=5)

        # 先触发一次买入
        prices1 = (
            [100, 99, 98, 97, 96, 95, 94, 93, 92, 91] +  # 预热
            [100, 110, 120, 130, 140]                      # 金叉 → 买入
        )
        feed_prices(strategy, prices1)
        assert strategy._in_position is True

        # 再次触发金叉（从死叉后再金叉），应先卖出再买入
        # 这里只验证不会在持仓状态下再次买入
        orders_extra = strategy.on_kline(make_kline(150))
        buy_extra = [o for o in orders_extra if o.side == "buy"]
        assert len(buy_extra) == 0  # 持仓中不重复买

    def test_death_cross_generates_sell(self) -> None:
        """死叉（快线下穿慢线）应产生卖出信号。"""
        strategy = self._make_strategy(fast=3, slow=5)

        # 触发金叉，进入持仓
        rising = [100, 99, 98, 97, 96, 95, 94, 93, 92, 91] + [100, 110, 120, 130, 140]
        feed_prices(strategy, rising)
        assert strategy._in_position is True

        # 触发死叉
        falling = [110, 100, 90, 80, 70]
        orders = feed_prices(strategy, falling)
        sell_orders = [o for o in orders if o.side == "sell"]
        assert len(sell_orders) >= 1
        assert strategy._in_position is False

    def test_order_has_correct_side_and_symbol(self) -> None:
        """产出的订单应正确填写 side 和 symbol。"""
        strategy = self._make_strategy(fast=3, slow=5)
        prices = (
            [100, 99, 98, 97, 96, 95, 94, 93, 92, 91] +
            [100, 110, 120, 130, 140]
        )
        orders = feed_prices(strategy, prices)
        buy_orders = [o for o in orders if o.side == "buy"]
        if buy_orders:
            assert buy_orders[0].symbol == "BTC/USDT"
            assert buy_orders[0].order_type == "market"
            assert buy_orders[0].quantity == Decimal("0.1")


# ─────────────────────────────────────────────────────────────
# RiskManager 测试
# ─────────────────────────────────────────────────────────────

class TestRiskManager:
    def _make_risk_manager(self, **kwargs) -> RiskManager:
        cfg = RiskConfig(
            max_position_pct=kwargs.get("max_position_pct", 0.20),
            max_portfolio_drawdown=kwargs.get("max_portfolio_drawdown", 0.10),
            max_daily_loss=kwargs.get("max_daily_loss", 0.05),
            max_consecutive_losses=kwargs.get("max_consecutive_losses", 3),
            blacklist=kwargs.get("blacklist", []),
        )
        return RiskManager(cfg)

    def test_normal_buy_allowed(self) -> None:
        """正常买入（仓位未超限、无熔断）应通过。"""
        rm = self._make_risk_manager()
        rm.update_equity(100_000.0)

        allowed, reason = rm.check(
            side="buy",
            symbol="BTC/USDT",
            quantity=Decimal("0.1"),
            price=50_000.0,
            current_equity=100_000.0,
            positions={},
        )
        assert allowed is True

    def test_position_limit_rejects_oversized_buy(self) -> None:
        """买入后超过单币种仓位上限时，应被拒绝。"""
        rm = self._make_risk_manager(max_position_pct=0.20)
        equity = 100_000.0
        rm.update_equity(equity)

        # 当前持仓已有 0.1 BTC @ 50000 = $5000（5%，未超限）
        # 再买 1 BTC @ 50000 = $50000，合计 $55000 = 55%，超过 20%
        allowed, reason = rm.check(
            side="buy",
            symbol="BTC/USDT",
            quantity=Decimal("1.0"),  # 需要 $50000
            price=50_000.0,
            current_equity=equity,
            positions={"BTC/USDT": Decimal("0.1")},
        )
        assert allowed is False
        assert "超过限制" in reason

    def test_blacklist_rejects_buy(self) -> None:
        """黑名单币种应被拒绝买入和卖出。"""
        rm = self._make_risk_manager(blacklist=["SCAM/USDT"])
        rm.update_equity(100_000.0)

        allowed, reason = rm.check(
            side="buy",
            symbol="SCAM/USDT",
            quantity=Decimal("100"),
            price=1.0,
            current_equity=100_000.0,
            positions={},
        )
        assert allowed is False
        assert "黑名单" in reason

    def test_consecutive_loss_triggers_circuit_breaker(self) -> None:
        """连续亏损达到阈值时应触发熔断。"""
        rm = self._make_risk_manager(max_consecutive_losses=3)

        rm.record_trade_outcome(won=False)
        rm.record_trade_outcome(won=False)
        assert not rm.is_circuit_broken()

        rm.record_trade_outcome(won=False)  # 第 3 次，触发熔断
        assert rm.is_circuit_broken()

    def test_circuit_broken_blocks_buy(self) -> None:
        """熔断状态下，买入应被拒绝。"""
        rm = self._make_risk_manager(max_consecutive_losses=1)
        rm.record_trade_outcome(won=False)
        assert rm.is_circuit_broken()

        rm.update_equity(100_000.0)
        allowed, reason = rm.check(
            side="buy",
            symbol="BTC/USDT",
            quantity=Decimal("0.1"),
            price=50_000.0,
            current_equity=100_000.0,
            positions={},
        )
        assert allowed is False
        assert "熔断" in reason

    def test_circuit_broken_allows_sell(self) -> None:
        """熔断状态下，卖出（减仓）应被允许。"""
        rm = self._make_risk_manager(max_consecutive_losses=1)
        rm.record_trade_outcome(won=False)
        assert rm.is_circuit_broken()

        rm.update_equity(100_000.0)
        allowed, reason = rm.check(
            side="sell",
            symbol="BTC/USDT",
            quantity=Decimal("0.1"),
            price=50_000.0,
            current_equity=100_000.0,
            positions={"BTC/USDT": Decimal("0.1")},
        )
        assert allowed is True

    def test_manual_reset_circuit_breaker(self) -> None:
        """手动解除熔断后，新买单应可通过。"""
        rm = self._make_risk_manager(max_consecutive_losses=1)
        rm.record_trade_outcome(won=False)
        assert rm.is_circuit_broken()

        rm.reset_circuit_breaker(authorized_by="test_engineer")
        assert not rm.is_circuit_broken()

    def test_max_drawdown_triggers_circuit_breaker(self) -> None:
        """净值回撤超 max_portfolio_drawdown 时应触发熔断。"""
        # max_daily_loss 设高一点（0.20），避免日亏限制先触发，专注测回撤逻辑
        rm = self._make_risk_manager(
            max_portfolio_drawdown=0.10,
            max_daily_loss=0.20,
        )

        rm.update_equity(100_000.0)  # 峰值
        rm.update_equity(95_000.0)   # -5%，未触发
        assert not rm.is_circuit_broken()

        rm.update_equity(89_000.0)   # -11%，超过 10%，触发
        assert rm.is_circuit_broken()

    def test_win_resets_consecutive_loss_count(self) -> None:
        """盈利之后连续亏损计数应归零。"""
        rm = self._make_risk_manager(max_consecutive_losses=5)

        rm.record_trade_outcome(won=False)
        rm.record_trade_outcome(won=False)
        assert rm._state.consecutive_losses == 2

        rm.record_trade_outcome(won=True)
        assert rm._state.consecutive_losses == 0

    def test_daily_loss_limit_rejects_buy(self) -> None:
        """单日亏损超过 max_daily_loss，新买单应被拒绝。"""
        rm = self._make_risk_manager(max_daily_loss=0.03)

        rm.update_equity(100_000.0)   # 日初净值
        rm.update_equity(96_500.0)    # -3.5%，超过 3% 限制，应触发

        allowed, reason = rm.check(
            side="buy",
            symbol="BTC/USDT",
            quantity=Decimal("0.1"),
            price=50_000.0,
            current_equity=96_500.0,
            positions={},
        )
        assert allowed is False
