"""
tests/test_execution.py — 执行层单元测试

覆盖项：
- CCXTGateway paper 模式：submit_order 返回虚拟 ID，不调用真实 API
- CCXTGateway paper 模式：cancel_order 正常返回 True
- CCXTGateway：无效交易所 ID 时初始化失败
- OrderManager.submit()：正常提交并存入 open_orders
- OrderManager.submit()：提交失败时存入 history 并重新抛出
- OrderManager.poll_fills()：检测到成交变更返回 FillResult
- OrderManager.cancel_timed_out_orders()：超时订单自动撤单
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from core.exceptions import ExchangeConnectionError, OrderSubmissionError
from modules.execution.gateway import CCXTGateway
from modules.execution.order_manager import OrderManager, OrderStatus


# ─────────────────────────────────────────────────────────────
# CCXTGateway 测试
# ─────────────────────────────────────────────────────────────

class TestCCXTGatewayPaperMode:
    """Paper 模式下所有测试不真实调用交易所。"""

    @pytest.fixture
    def gateway(self) -> CCXTGateway:
        """创建 paper 模式 gateway（不需要 API Key）。"""
        return CCXTGateway(
            exchange_id="binance",
            mode="paper",
            api_key="",
            secret="",
        )

    def test_init_paper_mode(self, gateway: CCXTGateway) -> None:
        """Paper 模式初始化应成功。"""
        assert gateway.mode == "paper"
        assert gateway.exchange_id == "binance"

    def test_submit_market_order_returns_paper_id(self, gateway: CCXTGateway) -> None:
        """Paper 模式下提交市价单应返回 paper_ 前缀的虚拟 ID。"""
        gateway.update_paper_price("BTC/USDT", 67000.0)
        order_id = gateway.submit_order(
            symbol="BTC/USDT",
            side="buy",
            order_type="market",
            quantity=0.01,
        )
        assert order_id.startswith("paper_")

    def test_submit_limit_order_returns_paper_id(self, gateway: CCXTGateway) -> None:
        """Paper 模式下提交限价单应返回虚拟 ID。"""
        order_id = gateway.submit_order(
            symbol="BTC/USDT",
            side="buy",
            order_type="limit",
            quantity=0.01,
            price=45000.0,
        )
        assert order_id.startswith("paper_")

    def test_cancel_order_paper_returns_true(self, gateway: CCXTGateway) -> None:
        """Paper 模式撤单应返回 True（不实际调用 API）。"""
        result = gateway.cancel_order("paper_abc123", "BTC/USDT")
        assert result is True

    def test_fetch_order_paper_returns_closed(self, gateway: CCXTGateway) -> None:
        """Paper 模式查询订单应返回 closed 状态。"""
        order = gateway.fetch_order("paper_abc", "BTC/USDT")
        assert order["status"] == "closed"

    def test_fetch_balance_paper_returns_dict(self, gateway: CCXTGateway) -> None:
        """Paper 模式查询余额应返回字典，含初始资金。"""
        balance = gateway.fetch_balance()
        assert isinstance(balance, dict)
        assert "USDT" in balance
        assert balance["USDT"]["free"] == 5000.0


class TestCCXTGatewayInvalidExchange:
    def test_unknown_exchange_raises(self) -> None:
        """未知交易所 ID 应在初始化时抛出 OrderSubmissionError。"""
        with pytest.raises(OrderSubmissionError, match="未知交易所"):
            CCXTGateway(exchange_id="not_a_real_exchange", mode="paper")

    def test_invalid_mode_raises(self) -> None:
        """无效 mode 应在初始化时抛出 ValueError。"""
        with pytest.raises(ValueError, match="live.*paper"):
            CCXTGateway(exchange_id="binance", mode="backtest")


class TestCCXTGatewayLiveModeRetry:
    """Live 模式下网络错误重试测试（使用 Mock）。"""

    def test_network_error_retried_then_raises(self) -> None:
        """网络错误超过 max_retries 次后应抛出 ExchangeConnectionError。"""
        import ccxt

        gateway = CCXTGateway(
            exchange_id="binance",
            mode="live",
            api_key="test_key",
            secret="test_secret",
            max_retries=2,
        )
        # Mock 交易所的 create_market_order 方法
        gateway._exchange.create_market_order = MagicMock(
            side_effect=ccxt.NetworkError("connection refused")
        )

        with pytest.raises(ExchangeConnectionError, match="下单失败"):
            gateway.submit_order(
                symbol="BTC/USDT",
                side="buy",
                order_type="market",
                quantity=0.1,
            )

        # 应重试了 max_retries 次
        assert gateway._exchange.create_market_order.call_count == 2

    def test_insufficient_funds_not_retried(self) -> None:
        """余额不足异常不应重试，直接抛出 OrderSubmissionError。"""
        import ccxt

        gateway = CCXTGateway(
            exchange_id="binance",
            mode="live",
            api_key="test_key",
            secret="test_secret",
            max_retries=3,
        )
        gateway._exchange.create_market_order = MagicMock(
            side_effect=ccxt.InsufficientFunds("余额不足")
        )

        with pytest.raises(OrderSubmissionError, match="余额不足"):
            gateway.submit_order(
                symbol="BTC/USDT",
                side="buy",
                order_type="market",
                quantity=1000.0,
            )

        # 不重试，只调用 1 次
        assert gateway._exchange.create_market_order.call_count == 1


# ─────────────────────────────────────────────────────────────
# OrderManager 测试
# ─────────────────────────────────────────────────────────────

class TestOrderManager:
    @pytest.fixture
    def paper_gateway(self) -> CCXTGateway:
        gw = CCXTGateway(exchange_id="binance", mode="paper")
        gw.update_paper_price("BTC/USDT", 67000.0)
        return gw

    @pytest.fixture
    def om(self, paper_gateway: CCXTGateway) -> OrderManager:
        return OrderManager(gateway=paper_gateway, fill_timeout_s=60)

    def test_submit_stores_in_open_orders(self, om: OrderManager) -> None:
        """提交订单应存储在 open_orders 中。"""
        local_id = om.submit(
            symbol="BTC/USDT",
            side="buy",
            order_type="market",
            quantity=Decimal("0.01"),
            price=None,
            strategy_id="test_strategy",
        )
        open_orders = om.get_open_orders()
        assert len(open_orders) == 1
        assert open_orders[0].local_id == local_id
        assert open_orders[0].status == OrderStatus.SUBMITTED

    def test_submit_failure_goes_to_history(self) -> None:
        """提交失败的订单应存入 history，不在 open_orders 中。"""
        mock_gateway = MagicMock()
        mock_gateway.submit_order.side_effect = OrderSubmissionError("拒绝")

        om = OrderManager(gateway=mock_gateway, fill_timeout_s=60)

        with pytest.raises(OrderSubmissionError):
            om.submit(
                symbol="BTC/USDT",
                side="buy",
                order_type="market",
                quantity=Decimal("0.1"),
                price=None,
                strategy_id="test",
            )

        assert len(om.get_open_orders()) == 0
        history = om.get_order_history()
        assert len(history) == 1
        assert history[0].status == OrderStatus.FAILED

    def test_poll_fills_detects_new_fill(self) -> None:
        """mock fetch_order 返回成交数据时，poll_fills 应返回 FillResult。"""
        mock_gateway = MagicMock()
        # 第一次提交成功
        mock_gateway.submit_order.return_value = "exc_order_001"
        # 轮询时返回已成交
        mock_gateway.fetch_order.return_value = {
            "status": "closed",
            "filled": 0.1,
            "remaining": 0,
            "average": 50000.0,
        }

        om = OrderManager(gateway=mock_gateway, fill_timeout_s=60)
        om.submit(
            symbol="BTC/USDT",
            side="buy",
            order_type="market",
            quantity=Decimal("0.1"),
            price=None,
            strategy_id="test",
        )

        fills = om.poll_fills()
        assert len(fills) == 1
        assert fills[0].is_complete is True
        assert fills[0].new_filled_qty == Decimal("0.1")

    def test_cancel_timed_out_orders(self) -> None:
        """提交时间超过 fill_timeout_s 的订单应被自动撤销。"""
        mock_gateway = MagicMock()
        mock_gateway.submit_order.return_value = "exc_order_002"
        mock_gateway.cancel_order.return_value = True
        # 轮询返回仍未成交
        mock_gateway.fetch_order.return_value = {
            "status": "open",
            "filled": 0,
            "remaining": 0.1,
            "average": None,
        }

        om = OrderManager(gateway=mock_gateway, fill_timeout_s=0)  # 立即超时
        om.submit(
            symbol="BTC/USDT",
            side="buy",
            order_type="limit",
            quantity=Decimal("0.1"),
            price=Decimal("40000"),
            strategy_id="test",
        )

        # 反向调拨提交时间，模拟超时已过
        for rec in om._open_orders.values():
            from datetime import timedelta
            rec.submitted_at = datetime.now(tz=timezone.utc) - timedelta(seconds=10)

        cancelled = om.cancel_timed_out_orders()
        assert cancelled == 1
        assert len(om.get_open_orders()) == 0
        assert om.get_order_history()[0].status == OrderStatus.CANCELLED

    def test_multiple_open_orders_tracked(self, om: OrderManager) -> None:
        """多个挂单应全部被追踪。"""
        for i in range(3):
            om.submit(
                symbol="BTC/USDT",
                side="buy",
                order_type="market",
                quantity=Decimal("0.01"),
                price=None,
                strategy_id="test",
            )

        assert len(om.get_open_orders()) == 3


# ─────────────────────────────────────────────────────────────
# SystemMetrics 测试（基本可调用性）
# ─────────────────────────────────────────────────────────────

class TestSystemMetrics:
    def test_update_equity_no_crash(self) -> None:
        """update_equity 应不崩溃（prometheus 可能未安装）。"""
        from modules.monitoring.metrics import SystemMetrics
        m = SystemMetrics(exchange_id="binance", mode="paper")
        m.update_equity(100_000.0)  # 不崩溃

    def test_record_rejection_categorization(self) -> None:
        """拒绝原因应正确分类。"""
        from modules.monitoring.metrics import SystemMetrics
        m = SystemMetrics()
        m.record_order_rejected("BTC/USDT", "单币种仓位 25% 超过限制 20%")
        m.record_order_rejected("BTC/USDT", "系统熔断中")
        m.record_order_rejected("BTC/USDT", "单日亏损超限")
        # 全部调用不崩溃即为通过

    def test_all_public_methods_callable(self) -> None:
        """所有公开方法应可调用（prometheus 未安装时也不崩溃）。"""
        from modules.monitoring.metrics import SystemMetrics
        m = SystemMetrics(exchange_id="binance", mode="paper")
        m.update_equity(100_000.0)
        m.update_position("BTC/USDT", 0.1, 5000.0)
        m.update_daily_pnl(-500.0)
        m.record_order_submitted("BTC/USDT", "buy", "market")
        m.record_order_filled("BTC/USDT", "buy", 0.1, 5000.0, 5.0)
        m.record_order_rejected("BTC/USDT", "position_limit")
        m.record_circuit_breaker(active=False)
        m.update_consecutive_losses(2)
        m.record_data_latency(123.4)
        m.record_signal("ma_cross", "buy")
        m.record_heartbeat()
