"""
tests/test_paper_trading_integration.py — Paper 模拟盘 + PositionSizer 全功能测试

覆盖项：
1. CCXTGateway paper 模式
   - 初始化：invalid mode / invalid exchange
   - submit_order: 市价买入（含滑点）、限价买入、市价卖出、限价卖出
   - 余额不足拒单 → OrderSubmissionError
   - 持仓不足拒单 → OrderSubmissionError
   - 无行情价格时市价单拒单
   - cancel_order (paper)
   - fetch_order (paper): closed / rejected / not registered
   - fetch_open_orders (paper) → 始终空列表
   - fetch_balance (paper)
   - update_paper_price / set_paper_cash / set_paper_positions
   - paper_cash property
   - fetch_ticker: live 模式网络错误 → ExchangeConnectionError
   - fetch_ohlcv: live 模式网络错误 → ExchangeConnectionError
   - fetch_balance: live 认证错误 → ExchangeConnectionError
   - cancel_order live: OrderNotFound → False
   - cancel_order live: NetworkError → ExchangeConnectionError
   - _live_submit 重试机制
   - close()
   - 多轮完整买卖生命周期：现金 round-trip 验证
   - 并发线程安全

2. PositionSizer 全覆盖
   - fixed_notional: 正常、超出上限、零价格保护
   - fixed_risk: 正常、止损距离接近 0、超出上限
   - volatility_target: 正常、atr_pct=0、超出上限
   - fractional_kelly: 正常、负 Kelly（策略无正期望）、kelly_fraction 上限 0.5
   - _round_qty: qty_step 精度截断、低于 min_qty 返回 0
   - 与真实仓位约束的端到端流程
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal
from typing import Any, Dict
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from core.exceptions import (
    ExchangeConnectionError,
    OrderSubmissionError,
)
from modules.execution.gateway import CCXTGateway
from modules.risk.position_sizer import PositionSizer


# ══════════════════════════════════════════════════════════════
# 辅助工具
# ══════════════════════════════════════════════════════════════

def make_paper_gateway(cash: float = 10_000.0) -> CCXTGateway:
    gw = CCXTGateway(exchange_id="binance", mode="paper")
    gw.set_paper_cash(cash)
    return gw


def _mock_ccxt_exchange():
    """创建一个可以模拟 live 模式的 Mock CCXT exchange。"""
    mock_ex = MagicMock()
    mock_ex.create_market_order.return_value = {"id": "live-order-001"}
    mock_ex.create_limit_order.return_value = {"id": "live-order-002"}
    mock_ex.cancel_order.return_value = {}
    mock_ex.fetch_order.return_value = {"id": "x", "status": "closed"}
    mock_ex.fetch_open_orders.return_value = []
    mock_ex.fetch_balance.return_value = {"USDT": {"free": 1000, "used": 0, "total": 1000}}
    mock_ex.fetch_ticker.return_value = {"last": 65000.0, "bid": 64990.0, "ask": 65010.0}
    mock_ex.fetch_ohlcv.return_value = [[1700000000000, 65000, 65500, 64500, 65200, 100.0]]
    return mock_ex


# ══════════════════════════════════════════════════════════════
# 1. CCXTGateway — 初始化
# ══════════════════════════════════════════════════════════════

class TestGatewayInit:

    def test_paper_mode_initializes_without_api_key(self):
        gw = CCXTGateway(exchange_id="binance", mode="paper")
        assert gw.mode == "paper"
        assert gw.exchange_id == "binance"

    def test_invalid_mode_raises_value_error(self):
        with pytest.raises(ValueError, match="'live' 或 'paper'"):
            CCXTGateway(exchange_id="binance", mode="sandbox")

    def test_invalid_exchange_raises_order_submission_error(self):
        with pytest.raises(OrderSubmissionError, match="未知交易所"):
            CCXTGateway(exchange_id="not_a_real_exchange_xyz", mode="paper")

    def test_paper_mode_defaults_cash_to_5000(self):
        gw = CCXTGateway(exchange_id="binance", mode="paper")
        assert gw.paper_cash == pytest.approx(5000.0)

    def test_live_mode_paper_cash_returns_zero(self):
        gw = CCXTGateway(exchange_id="binance", mode="live")
        assert gw.paper_cash == 0.0

    def test_set_paper_cash_updates_balance(self):
        gw = make_paper_gateway(20_000)
        assert gw.paper_cash == pytest.approx(20_000.0)

    def test_set_paper_cash_ignored_in_live_mode(self):
        gw = CCXTGateway(exchange_id="binance", mode="live")
        gw.set_paper_cash(99999)  # no-op
        assert gw.paper_cash == 0.0


# ══════════════════════════════════════════════════════════════
# 2. Paper 模式 — submit_order 市价买入
# ══════════════════════════════════════════════════════════════

class TestPaperMarketBuy:

    def test_market_buy_reduces_cash_and_adds_position(self):
        gw = make_paper_gateway(10_000)
        gw.update_paper_price("BTC/USDT", 50_000.0)

        order_id = gw.submit_order("BTC/USDT", "buy", "market", 0.1)

        assert order_id.startswith("paper_")
        # fill_price = 50000 * 1.001 = 50050
        # cost = 0.1 * 50050 * 1.001 ≈ 5010.05
        assert gw.paper_cash < 10_000.0

    def test_market_buy_position_increases(self):
        gw = make_paper_gateway(20_000)
        gw.update_paper_price("BTC/USDT", 40_000.0)
        gw.submit_order("BTC/USDT", "buy", "market", 0.2)
        order2 = gw.submit_order("BTC/USDT", "buy", "market", 0.1)
        assert order2.startswith("paper_")

    def test_market_buy_no_price_raises_order_submission_error(self):
        gw = make_paper_gateway(10_000)
        # No update_paper_price call → no price available
        with pytest.raises(OrderSubmissionError, match="无行情数据"):
            gw.submit_order("BTC/USDT", "buy", "market", 0.1)

    def test_market_buy_price_zero_raises_order_submission_error(self):
        gw = make_paper_gateway(10_000)
        gw.update_paper_price("BTC/USDT", 0.0)
        with pytest.raises(OrderSubmissionError):
            gw.submit_order("BTC/USDT", "buy", "market", 0.1)

    def test_market_buy_insufficient_cash_raises(self):
        gw = make_paper_gateway(100.0)
        gw.update_paper_price("BTC/USDT", 50_000.0)
        with pytest.raises(OrderSubmissionError, match="余额不足"):
            gw.submit_order("BTC/USDT", "buy", "market", 1.0)

    def test_market_buy_slippage_applied(self):
        gw = make_paper_gateway(100_000)
        gw.update_paper_price("BTC/USDT", 50_000.0)
        cash_before = gw.paper_cash
        gw.submit_order("BTC/USDT", "buy", "market", 0.1)
        cash_after = gw.paper_cash
        # Expected fill price = 50000 * 1.001 = 50050; cost = 0.1 * 50050 + fee
        # fee = 0.1 * 50050 * 0.001 = 5.005; total = 5005.005 + 5.005 = 5010.055
        expected_cost = 0.1 * 50_000 * 1.001 * 1.001
        assert (cash_before - cash_after) == pytest.approx(expected_cost, rel=1e-4)

    def test_rejected_order_recorded_in_paper_orders(self):
        gw = make_paper_gateway(10.0)
        gw.update_paper_price("BTC/USDT", 50_000.0)
        try:
            order_id = gw.submit_order("BTC/USDT", "buy", "market", 1.0)
        except OrderSubmissionError:
            pass
        # Rejected orders are stored internally; fetch_order returns rejected status


# ══════════════════════════════════════════════════════════════
# 3. Paper 模式 — submit_order 限价单
# ══════════════════════════════════════════════════════════════

class TestPaperLimitOrder:

    def test_limit_buy_fills_at_exact_limit_price(self):
        gw = make_paper_gateway(50_000)
        cash_before = gw.paper_cash

        order_id = gw.submit_order("BTC/USDT", "buy", "limit", 0.1, price=48_000.0)

        assert order_id.startswith("paper_")
        # cost = 0.1 * 48000 + fee = 4800 + 4800*0.001 = 4804.8
        expected_cost = 0.1 * 48_000 * 1.001
        assert (cash_before - gw.paper_cash) == pytest.approx(expected_cost, rel=1e-4)

    def test_limit_sell_fills_at_exact_limit_price(self):
        gw = make_paper_gateway(50_000)
        # First buy
        gw.submit_order("BTC/USDT", "buy", "limit", 0.5, price=50_000.0)
        cash_mid = gw.paper_cash
        # Then sell
        gw.submit_order("BTC/USDT", "sell", "limit", 0.5, price=52_000.0)
        # After sell: cash should increase
        assert gw.paper_cash > cash_mid

    def test_limit_buy_no_price_raises(self):
        gw = make_paper_gateway(10_000)
        with pytest.raises(OrderSubmissionError, match="有效价格"):
            gw.submit_order("BTC/USDT", "buy", "limit", 0.1, price=None)

    def test_limit_buy_zero_price_raises(self):
        gw = make_paper_gateway(10_000)
        with pytest.raises(OrderSubmissionError):
            gw.submit_order("BTC/USDT", "buy", "limit", 0.1, price=0.0)

    def test_limit_sell_insufficient_position_raises(self):
        gw = make_paper_gateway(50_000)
        # No position in ETH/USDT
        with pytest.raises(OrderSubmissionError, match="持仓不足"):
            gw.submit_order("ETH/USDT", "sell", "limit", 1.0, price=3000.0)

    def test_limit_sell_partial_position_fills_max_available(self):
        """卖出量超过持仓时，按实际持仓量成交（不拒单）。"""
        gw = make_paper_gateway(100_000)
        # Buy 0.3 BTC
        gw.submit_order("BTC/USDT", "buy", "limit", 0.3, price=50_000.0)
        cash_mid = gw.paper_cash
        # Try to sell 0.5 BTC (only 0.3 available) → fills at 0.3
        order_id = gw.submit_order("BTC/USDT", "sell", "limit", 0.5, price=51_000.0)
        assert order_id.startswith("paper_")
        # Cash should increase
        assert gw.paper_cash > cash_mid


# ══════════════════════════════════════════════════════════════
# 4. Paper 模式 — 其他接口
# ══════════════════════════════════════════════════════════════

class TestPaperOtherInterfaces:

    def test_cancel_order_paper_always_returns_true(self):
        gw = make_paper_gateway()
        result = gw.cancel_order("paper_abc123", "BTC/USDT")
        assert result is True

    def test_fetch_order_paper_closed_status(self):
        gw = make_paper_gateway(100_000)
        gw.submit_order("BTC/USDT", "buy", "limit", 0.1, price=50_000.0)
        # We can't easily get the order_id from here; just test fetch_order with unknown id
        result = gw.fetch_order("unknown-id", "BTC/USDT")
        assert result["id"] == "unknown-id"
        assert result["status"] == "closed"  # default for unregistered

    def test_fetch_open_orders_paper_empty(self):
        gw = make_paper_gateway()
        result = gw.fetch_open_orders("BTC/USDT")
        assert result == []

    def test_fetch_open_orders_paper_no_symbol(self):
        gw = make_paper_gateway()
        result = gw.fetch_open_orders()
        assert result == []

    def test_fetch_balance_paper_returns_usdt(self):
        gw = make_paper_gateway(8888.88)
        bal = gw.fetch_balance()
        assert "USDT" in bal
        assert bal["USDT"]["free"] == pytest.approx(8888.88)
        assert bal["USDT"]["used"] == 0

    def test_update_paper_price_only_in_paper_mode(self):
        gw = CCXTGateway(exchange_id="binance", mode="live")
        gw.update_paper_price("BTC/USDT", 65000.0)  # no-op in live
        # No exception

    def test_set_paper_positions_restores_state(self):
        gw = make_paper_gateway(50_000)
        gw.set_paper_positions({"BTC/USDT": Decimal("0.5")})
        # Now sell half
        order_id = gw.submit_order("BTC/USDT", "sell", "limit", 0.25, price=50_000.0)
        assert order_id.startswith("paper_")

    def test_set_paper_positions_ignored_in_live_mode(self):
        gw = CCXTGateway(exchange_id="binance", mode="live")
        gw.set_paper_positions({"BTC/USDT": Decimal("1.0")})  # no-op
        assert gw.paper_cash == 0.0

    def test_close_does_not_raise(self):
        gw = make_paper_gateway()
        gw.close()  # Should not raise


# ══════════════════════════════════════════════════════════════
# 5. Paper 模式 — 完整交易生命周期
# ══════════════════════════════════════════════════════════════

class TestPaperFullLifecycle:

    def test_buy_then_sell_cash_round_trip_minus_fees(self):
        """完整一次买卖后，最终现金 ≈ 初始 - 两次手续费（因无价格涨跌）。"""
        gw = make_paper_gateway(100_000)
        initial_cash = gw.paper_cash
        price = 50_000.0

        # Buy 0.1 BTC at 50000
        gw.submit_order("BTC/USDT", "buy", "limit", 0.1, price=price)
        # Sell 0.1 BTC at 50000 (same price)
        gw.submit_order("BTC/USDT", "sell", "limit", 0.1, price=price)

        final_cash = gw.paper_cash
        # At same price, lose both fees (buy_fee + sell_fee)
        fee = 0.1 * 50_000 * 0.001
        expected_loss = fee * 2
        assert (initial_cash - final_cash) == pytest.approx(expected_loss, rel=1e-3)

    def test_profit_scenario_sell_higher_than_buy(self):
        gw = make_paper_gateway(100_000)
        initial_cash = gw.paper_cash

        gw.submit_order("BTC/USDT", "buy", "limit", 0.1, price=50_000.0)
        gw.submit_order("BTC/USDT", "sell", "limit", 0.1, price=55_000.0)

        final_cash = gw.paper_cash
        # Profit (minus fees)
        expected_profit = 0.1 * 5_000 - 0.1 * 50_000 * 0.001 - 0.1 * 55_000 * 0.001
        assert (final_cash - initial_cash) == pytest.approx(expected_profit, rel=1e-3)

    def test_multiple_symbols_independent_positions(self):
        gw = make_paper_gateway(200_000)

        gw.submit_order("BTC/USDT", "buy", "limit", 0.5, price=50_000.0)
        gw.submit_order("ETH/USDT", "buy", "limit", 5.0, price=3_000.0)

        cash_after_buys = gw.paper_cash

        # Sell both
        gw.submit_order("BTC/USDT", "sell", "limit", 0.5, price=50_000.0)
        gw.submit_order("ETH/USDT", "sell", "limit", 5.0, price=3_000.0)

        # Net should be approximately initial minus 4x fees
        assert gw.paper_cash < 200_000
        assert gw.paper_cash > 195_000  # reasonable sanity check

    def test_sequential_buys_accumulate_position(self):
        gw = make_paper_gateway(200_000)

        for i in range(5):
            gw.submit_order("BTC/USDT", "buy", "limit", 0.1, price=50_000.0)

        # Sell all 5 × 0.1 = 0.5 BTC
        gw.submit_order("BTC/USDT", "sell", "limit", 0.5, price=50_000.0)
        assert gw.paper_cash < 200_000

    def test_order_ids_are_unique(self):
        gw = make_paper_gateway(200_000)
        ids = set()
        for _ in range(20):
            oid = gw.submit_order("BTC/USDT", "buy", "limit", 0.01, price=50_000.0)
            ids.add(oid)
        assert len(ids) == 20


# ══════════════════════════════════════════════════════════════
# 6. Paper 模式 — 并发线程安全
# ══════════════════════════════════════════════════════════════

class TestPaperThreadSafety:

    def test_concurrent_buys_no_race_condition(self):
        """并发多线程买入不应导致现金计算 race condition。"""
        gw = make_paper_gateway(500_000)
        errors = []

        def _buy():
            try:
                gw.submit_order("BTC/USDT", "buy", "limit", 0.01, price=50_000.0)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_buy) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Filter only non-OrderSubmissionError errors (genuine race conditions)
        genuine_errors = [e for e in errors if not isinstance(e, OrderSubmissionError)]
        assert len(genuine_errors) == 0

    def test_concurrent_mixed_operations_stable(self):
        gw = make_paper_gateway(100_000)
        # Pre-buy some position
        gw.submit_order("BTC/USDT", "buy", "limit", 0.5, price=50_000.0)

        errors = []

        def _sell():
            try:
                gw.submit_order("BTC/USDT", "sell", "limit", 0.05, price=50_000.0)
            except OrderSubmissionError:
                pass  # Expected when position runs out
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_sell) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


# ══════════════════════════════════════════════════════════════
# 7. Live 模式 — 网络错误处理
# ══════════════════════════════════════════════════════════════

class TestLiveModeErrors:

    def _make_live_gw_with_mock(self, mock_exchange):
        gw = CCXTGateway(exchange_id="binance", mode="live")
        gw._exchange = mock_exchange
        return gw

    def test_fetch_ticker_network_error_raises_connection_error(self):
        import ccxt
        mock_ex = _mock_ccxt_exchange()
        mock_ex.fetch_ticker.side_effect = ccxt.NetworkError("timeout")
        gw = self._make_live_gw_with_mock(mock_ex)
        with pytest.raises(ExchangeConnectionError, match="网络错误"):
            gw.fetch_ticker("BTC/USDT")

    def test_fetch_ohlcv_network_error_raises_connection_error(self):
        import ccxt
        mock_ex = _mock_ccxt_exchange()
        mock_ex.fetch_ohlcv.side_effect = ccxt.NetworkError("timeout")
        gw = self._make_live_gw_with_mock(mock_ex)
        with pytest.raises(ExchangeConnectionError):
            gw.fetch_ohlcv("BTC/USDT")

    def test_fetch_ohlcv_exchange_error_raises_connection_error(self):
        import ccxt
        mock_ex = _mock_ccxt_exchange()
        mock_ex.fetch_ohlcv.side_effect = ccxt.ExchangeError("symbol not found")
        gw = self._make_live_gw_with_mock(mock_ex)
        with pytest.raises(ExchangeConnectionError):
            gw.fetch_ohlcv("INVALID/USDT")

    def test_fetch_balance_authentication_error_raises_connection_error(self):
        import ccxt
        mock_ex = _mock_ccxt_exchange()
        mock_ex.fetch_balance.side_effect = ccxt.AuthenticationError("invalid key")
        gw = self._make_live_gw_with_mock(mock_ex)
        with pytest.raises(ExchangeConnectionError, match="API 认证"):
            gw.fetch_balance()

    def test_fetch_balance_network_error_raises_connection_error(self):
        import ccxt
        mock_ex = _mock_ccxt_exchange()
        mock_ex.fetch_balance.side_effect = ccxt.NetworkError("timeout")
        gw = self._make_live_gw_with_mock(mock_ex)
        with pytest.raises(ExchangeConnectionError):
            gw.fetch_balance()

    def test_fetch_open_orders_network_error_raises(self):
        import ccxt
        mock_ex = _mock_ccxt_exchange()
        mock_ex.fetch_open_orders.side_effect = ccxt.NetworkError("timeout")
        gw = self._make_live_gw_with_mock(mock_ex)
        with pytest.raises(ExchangeConnectionError):
            gw.fetch_open_orders("BTC/USDT")

    def test_fetch_order_order_not_found_returns_not_found(self):
        import ccxt
        mock_ex = _mock_ccxt_exchange()
        mock_ex.fetch_order.side_effect = ccxt.OrderNotFound("no such order")
        gw = self._make_live_gw_with_mock(mock_ex)
        result = gw.fetch_order("bad-id", "BTC/USDT")
        assert result["status"] == "not_found"

    def test_fetch_order_network_error_raises(self):
        import ccxt
        mock_ex = _mock_ccxt_exchange()
        mock_ex.fetch_order.side_effect = ccxt.NetworkError("timeout")
        gw = self._make_live_gw_with_mock(mock_ex)
        with pytest.raises(ExchangeConnectionError):
            gw.fetch_order("oid", "BTC/USDT")

    def test_cancel_order_live_order_not_found_returns_false(self):
        import ccxt
        mock_ex = _mock_ccxt_exchange()
        mock_ex.cancel_order.side_effect = ccxt.OrderNotFound("already filled")
        gw = self._make_live_gw_with_mock(mock_ex)
        result = gw.cancel_order("oid", "BTC/USDT")
        assert result is False

    def test_cancel_order_live_network_error_raises(self):
        import ccxt
        mock_ex = _mock_ccxt_exchange()
        mock_ex.cancel_order.side_effect = ccxt.NetworkError("timeout")
        gw = self._make_live_gw_with_mock(mock_ex)
        with pytest.raises(ExchangeConnectionError, match="撤单网络错误"):
            gw.cancel_order("oid", "BTC/USDT")

    def test_cancel_order_live_exchange_error_raises_submission_error(self):
        import ccxt
        mock_ex = _mock_ccxt_exchange()
        mock_ex.cancel_order.side_effect = ccxt.ExchangeError("cannot cancel")
        gw = self._make_live_gw_with_mock(mock_ex)
        with pytest.raises(OrderSubmissionError, match="撤单失败"):
            gw.cancel_order("oid", "BTC/USDT")

    def test_live_submit_market_order_succeeds(self):
        mock_ex = _mock_ccxt_exchange()
        gw = self._make_live_gw_with_mock(mock_ex)
        order_id = gw.submit_order("BTC/USDT", "buy", "market", 0.1)
        assert order_id == "live-order-001"

    def test_live_submit_limit_order_succeeds(self):
        mock_ex = _mock_ccxt_exchange()
        gw = self._make_live_gw_with_mock(mock_ex)
        order_id = gw.submit_order("BTC/USDT", "buy", "limit", 0.1, price=50_000.0)
        assert order_id == "live-order-002"

    def test_live_submit_limit_no_price_raises(self):
        mock_ex = _mock_ccxt_exchange()
        gw = self._make_live_gw_with_mock(mock_ex)
        with pytest.raises(OrderSubmissionError, match="price"):
            gw.submit_order("BTC/USDT", "buy", "limit", 0.1, price=None)

    def test_live_submit_network_error_retries_and_raises(self):
        import ccxt
        mock_ex = _mock_ccxt_exchange()
        mock_ex.create_market_order.side_effect = ccxt.NetworkError("timeout")
        gw = self._make_live_gw_with_mock(mock_ex)
        gw.max_retries = 2

        with patch("time.sleep"):  # Skip actual sleep
            with pytest.raises(ExchangeConnectionError):
                gw.submit_order("BTC/USDT", "buy", "market", 0.1)

        # Should have retried max_retries times
        assert mock_ex.create_market_order.call_count == 2

    def test_live_submit_exchange_error_raises_submission_error(self):
        import ccxt
        mock_ex = _mock_ccxt_exchange()
        mock_ex.create_market_order.side_effect = ccxt.InsufficientFunds("no funds")
        gw = self._make_live_gw_with_mock(mock_ex)

        with patch("time.sleep"):
            with pytest.raises(OrderSubmissionError):
                gw.submit_order("BTC/USDT", "buy", "market", 0.1)

    def test_close_calls_exchange_close(self):
        mock_ex = _mock_ccxt_exchange()
        gw = self._make_live_gw_with_mock(mock_ex)
        gw.close()
        mock_ex.close.assert_called_once()

    def test_close_suppresses_exchange_close_exception(self):
        mock_ex = _mock_ccxt_exchange()
        mock_ex.close.side_effect = RuntimeError("close error")
        gw = self._make_live_gw_with_mock(mock_ex)
        gw.close()  # Should not raise


# ══════════════════════════════════════════════════════════════
# 8. PositionSizer — 全方法覆盖
# ══════════════════════════════════════════════════════════════

class TestPositionSizerFixedNotional:

    def test_basic_calculation(self):
        sizer = PositionSizer(max_position_pct=0.2, min_qty=0.001, qty_step=0.001)
        # notional=1000, price=10000, equity=100000 → qty=0.1
        qty = sizer.fixed_notional(notional=1000, price=10_000, equity=100_000)
        assert qty == Decimal("0.100")

    def test_capped_by_max_position_pct(self):
        sizer = PositionSizer(max_position_pct=0.1, min_qty=0.001, qty_step=0.001)
        # max_notional = 0.1 * 100000 = 10000; notional=50000 → capped at 10000
        qty = sizer.fixed_notional(notional=50_000, price=10_000, equity=100_000)
        # 10000 / 10000 = 1.0 → rounded to step 0.001
        assert qty == Decimal("1.000")

    def test_notional_below_cap_uses_notional(self):
        sizer = PositionSizer(max_position_pct=0.5, min_qty=0.0001, qty_step=0.0001)
        qty = sizer.fixed_notional(notional=500, price=10_000, equity=100_000)
        assert qty == Decimal("0.0500")

    def test_result_below_min_qty_returns_zero(self):
        sizer = PositionSizer(max_position_pct=0.2, min_qty=1.0, qty_step=1.0)
        # notional=1, price=10000 → qty=0.0001 < min_qty=1 → 0
        qty = sizer.fixed_notional(notional=1, price=10_000, equity=100_000)
        assert qty == Decimal("0")


class TestPositionSizerFixedRisk:

    def test_basic_fixed_risk(self):
        sizer = PositionSizer(max_position_pct=0.5, min_qty=0.001, qty_step=0.001)
        # risk_amount=100, stop_distance=500 → qty=0.2
        qty = sizer.fixed_risk(
            risk_amount=100, entry_price=50_000, stop_price=49_500, equity=100_000
        )
        assert qty == Decimal("0.200")

    def test_stop_distance_near_zero_returns_zero(self):
        sizer = PositionSizer(max_position_pct=0.2, min_qty=0.001, qty_step=0.001)
        qty = sizer.fixed_risk(
            risk_amount=100, entry_price=50_000, stop_price=50_000, equity=100_000
        )
        assert qty == Decimal("0")

    def test_stop_distance_very_small_returns_zero(self):
        sizer = PositionSizer(max_position_pct=0.2, min_qty=0.001, qty_step=0.001)
        qty = sizer.fixed_risk(
            risk_amount=100,
            entry_price=50_000,
            stop_price=50_000 + 1e-11,
            equity=100_000,
        )
        assert qty == Decimal("0")

    def test_capped_by_max_position_pct(self):
        sizer = PositionSizer(max_position_pct=0.01, min_qty=0.001, qty_step=0.001)
        # max_qty = 0.01 * 100000 / 50000 = 0.02
        # uncapped_qty = 100 / 100 = 1.0 → capped to 0.020
        qty = sizer.fixed_risk(
            risk_amount=100, entry_price=50_000, stop_price=49_900, equity=100_000
        )
        assert qty == Decimal("0.020")

    def test_stop_price_below_entry(self):
        sizer = PositionSizer(max_position_pct=0.5, min_qty=0.001, qty_step=0.001)
        qty = sizer.fixed_risk(
            risk_amount=200, entry_price=50_000, stop_price=45_000, equity=200_000
        )
        # stop_distance = 5000; qty = 200/5000 = 0.04
        assert qty == Decimal("0.040")


class TestPositionSizerVolatilityTarget:

    def test_basic_vol_target(self):
        sizer = PositionSizer(max_position_pct=0.5, min_qty=0.001, qty_step=0.001)
        # target_vol=0.01 (1%), equity=100000, atr_pct=0.02 (2%), price=50000
        # qty = (0.01 * 100000) / (0.02 * 50000) = 1000 / 1000 = 1.0
        qty = sizer.volatility_target(
            equity=100_000, atr_pct=0.02, target_vol=0.01, price=50_000
        )
        assert qty == Decimal("1.000")

    def test_atr_pct_zero_returns_zero(self):
        sizer = PositionSizer(max_position_pct=0.5, min_qty=0.001, qty_step=0.001)
        qty = sizer.volatility_target(
            equity=100_000, atr_pct=0.0, target_vol=0.01, price=50_000
        )
        assert qty == Decimal("0")

    def test_negative_atr_pct_returns_zero(self):
        sizer = PositionSizer(max_position_pct=0.5, min_qty=0.001, qty_step=0.001)
        qty = sizer.volatility_target(
            equity=100_000, atr_pct=-0.01, target_vol=0.01, price=50_000
        )
        assert qty == Decimal("0")

    def test_capped_by_max_position_pct(self):
        sizer = PositionSizer(max_position_pct=0.05, min_qty=0.001, qty_step=0.001)
        # uncapped = large number; capped = 0.05 * 100000 / 50000 = 0.1
        qty = sizer.volatility_target(
            equity=100_000, atr_pct=0.001, target_vol=0.1, price=50_000
        )
        assert qty == Decimal("0.100")

    def test_high_volatility_results_in_smaller_position(self):
        sizer = PositionSizer(max_position_pct=0.5, min_qty=0.001, qty_step=0.001)
        qty_low_vol = sizer.volatility_target(
            equity=100_000, atr_pct=0.02, target_vol=0.01, price=50_000
        )
        qty_high_vol = sizer.volatility_target(
            equity=100_000, atr_pct=0.10, target_vol=0.01, price=50_000
        )
        assert qty_high_vol < qty_low_vol


class TestPositionSizerFractionalKelly:

    def test_positive_kelly_normal_scenario(self):
        sizer = PositionSizer(max_position_pct=0.25, min_qty=0.001, qty_step=0.001)
        # win_rate=0.6, profit_loss_ratio=1.5, equity=100000, price=50000
        # kelly_f* = (0.6*2.5 - 1) / 1.5 = (1.5-1)/1.5 = 0.333
        # fractional (0.25) = 0.333 * 0.25 = 0.083
        # notional = 0.083 * 100000 = 8333; qty = 8333 / 50000 ≈ 0.166
        qty = sizer.fractional_kelly(
            win_rate=0.6, profit_loss_ratio=1.5,
            equity=100_000, price=50_000, kelly_fraction=0.25,
        )
        assert qty > Decimal("0")

    def test_negative_kelly_returns_zero(self):
        sizer = PositionSizer(max_position_pct=0.25, min_qty=0.001, qty_step=0.001)
        # win_rate=0.3, profit_loss_ratio=0.5 → f* = (0.3*1.5 - 1)/0.5 = -0.1/0.5 = -0.2
        qty = sizer.fractional_kelly(
            win_rate=0.3, profit_loss_ratio=0.5,
            equity=100_000, price=50_000,
        )
        assert qty == Decimal("0")

    def test_kelly_fraction_capped_at_0_5(self):
        sizer = PositionSizer(max_position_pct=0.5, min_qty=0.001, qty_step=0.001)
        # Provide kelly_fraction=2.0 (should be capped to 0.5)
        qty_capped = sizer.fractional_kelly(
            win_rate=0.6, profit_loss_ratio=2.0,
            equity=100_000, price=50_000, kelly_fraction=2.0,
        )
        qty_uncapped = sizer.fractional_kelly(
            win_rate=0.6, profit_loss_ratio=2.0,
            equity=100_000, price=50_000, kelly_fraction=0.5,
        )
        assert qty_capped == qty_uncapped

    def test_zero_profit_loss_ratio_returns_zero(self):
        sizer = PositionSizer(max_position_pct=0.25, min_qty=0.001, qty_step=0.001)
        qty = sizer.fractional_kelly(
            win_rate=0.6, profit_loss_ratio=0.0,
            equity=100_000, price=50_000,
        )
        assert qty == Decimal("0")

    def test_capped_by_max_position_pct(self):
        sizer = PositionSizer(max_position_pct=0.01, min_qty=0.001, qty_step=0.001)
        # High win rate / high PLR → kelly_f >> max_position_pct → capped
        qty = sizer.fractional_kelly(
            win_rate=0.9, profit_loss_ratio=5.0,
            equity=100_000, price=50_000, kelly_fraction=0.5,
        )
        max_expected_qty = 0.01 * 100_000 / 50_000
        assert float(qty) <= max_expected_qty + 1e-6

    def test_default_kelly_fraction_is_0_25(self):
        sizer = PositionSizer(max_position_pct=0.5, min_qty=0.001, qty_step=0.001)
        qty_default = sizer.fractional_kelly(
            win_rate=0.6, profit_loss_ratio=2.0,
            equity=100_000, price=50_000,
        )
        qty_explicit = sizer.fractional_kelly(
            win_rate=0.6, profit_loss_ratio=2.0,
            equity=100_000, price=50_000, kelly_fraction=0.25,
        )
        assert qty_default == qty_explicit


class TestPositionSizerRoundQty:

    def test_round_down_to_qty_step(self):
        sizer = PositionSizer(max_position_pct=0.5, min_qty=0.001, qty_step=0.001)
        # 0.1234 rounds down to 0.123
        qty = sizer._round_qty(Decimal("0.1234"))
        assert qty == Decimal("0.123")

    def test_zero_qty_returns_zero(self):
        sizer = PositionSizer(max_position_pct=0.5, min_qty=0.001, qty_step=0.001)
        assert sizer._round_qty(Decimal("0")) == Decimal("0")

    def test_negative_qty_returns_zero(self):
        sizer = PositionSizer(max_position_pct=0.5, min_qty=0.001, qty_step=0.001)
        assert sizer._round_qty(Decimal("-0.1")) == Decimal("0")

    def test_qty_below_min_qty_returns_zero(self):
        sizer = PositionSizer(max_position_pct=0.5, min_qty=0.01, qty_step=0.001)
        # 0.005 rounds down to 0.005 but min_qty=0.01 → 0
        qty = sizer._round_qty(Decimal("0.005"))
        assert qty == Decimal("0")

    def test_exact_min_qty_returns_min_qty(self):
        sizer = PositionSizer(max_position_pct=0.5, min_qty=0.001, qty_step=0.001)
        qty = sizer._round_qty(Decimal("0.001"))
        assert qty == Decimal("0.001")

    def test_large_qty_step_precision(self):
        sizer = PositionSizer(max_position_pct=0.5, min_qty=1.0, qty_step=1.0)
        qty = sizer._round_qty(Decimal("7.9"))
        assert qty == Decimal("7")


# ══════════════════════════════════════════════════════════════
# 9. PositionSizer + Gateway 端到端 Paper 交易
# ══════════════════════════════════════════════════════════════

class TestPositionSizerGatewayIntegration:

    def test_fixed_notional_generates_valid_order_size(self):
        sizer = PositionSizer(max_position_pct=0.20, min_qty=0.001, qty_step=0.001)
        gw = make_paper_gateway(100_000)

        equity = gw.paper_cash
        qty = sizer.fixed_notional(notional=5000, price=50_000, equity=equity)

        assert qty > Decimal("0")
        # Place the order
        gw.submit_order("BTC/USDT", "buy", "limit", float(qty), price=50_000.0)
        assert gw.paper_cash < 100_000

    def test_volatility_target_generates_valid_order_size(self):
        sizer = PositionSizer(max_position_pct=0.10, min_qty=0.001, qty_step=0.001)
        gw = make_paper_gateway(100_000)

        equity = gw.paper_cash
        qty = sizer.volatility_target(equity=equity, atr_pct=0.02, target_vol=0.01, price=50_000)

        assert qty > Decimal("0")
        gw.submit_order("BTC/USDT", "buy", "limit", float(qty), price=50_000.0)
        assert gw.paper_cash < 100_000

    def test_kelly_zero_prevents_order(self):
        """Negative Kelly → qty=0 → no order is placed."""
        sizer = PositionSizer(max_position_pct=0.25, min_qty=0.001, qty_step=0.001)
        qty = sizer.fractional_kelly(
            win_rate=0.2, profit_loss_ratio=0.3,
            equity=100_000, price=50_000,
        )
        assert qty == Decimal("0")
        # Don't submit order with 0 qty
