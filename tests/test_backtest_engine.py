"""
tests/test_backtest_engine.py — 回测引擎集成测试

覆盖项：
- 完整回测流程（DataFeed → Engine → Broker → Reporter）
- 市价单防未来函数（同根 K 线不成交）
- 限价单正确触发
- 余额不足时订单被拒（不崩溃）
- 权益曲线长度 = K 线数量
- 绩效指标包含必要字段
- 空策略（无订单）时回测正常完成
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import List
from unittest.mock import MagicMock

import pandas as pd
import pytest

from apps.backtest.broker import SimulatedBroker
from apps.backtest.engine import BacktestConfig, BacktestEngine
from core.event import EventBus, EventType, KlineEvent, OrderRequestEvent
from modules.data.feed import DataFeed
from modules.data.storage import ParquetStorage


# ─────────────────────────────────────────────────────────────
# 测试辅助工具
# ─────────────────────────────────────────────────────────────

def make_ohlcv_df(n: int = 20, start: str = "2024-01-01", freq: str = "1H") -> pd.DataFrame:
    """生成 n 条合法 OHLCV K 线（UTC）。"""
    ts = pd.date_range(start=start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({
        "timestamp": ts,
        "symbol": "BTC/USDT",
        "open":   [100.0 + i * 0.5 for i in range(n)],
        "high":   [105.0 + i * 0.5 for i in range(n)],
        "low":    [95.0 + i * 0.5 for i in range(n)],
        "close":  [102.0 + i * 0.5 for i in range(n)],
        "volume": [1000.0] * n,
    })


def make_request(
    symbol: str = "BTC/USDT",
    side: str = "buy",
    qty: float = 0.1,
    price: float | None = None,
    ts: datetime | None = None,
) -> OrderRequestEvent:
    """构造一个 OrderRequestEvent。"""
    return OrderRequestEvent(
        event_type=EventType.ORDER_REQUESTED,
        timestamp=ts or datetime.now(tz=timezone.utc),
        source="test_strategy",
        symbol=symbol,
        side=side,
        order_type="market" if price is None else "limit",
        quantity=Decimal(str(qty)),
        price=Decimal(str(price)) if price is not None else None,
        strategy_id="test",
        request_id=str(uuid.uuid4()),
    )


@pytest.fixture
def storage(tmp_path) -> ParquetStorage:
    st = ParquetStorage(root_dir=tmp_path / "storage", exchange_id="binance")
    df = make_ohlcv_df(n=20)
    st.write(df, symbol="BTC/USDT", timeframe="1h")
    return st


@pytest.fixture
def feed(storage: ParquetStorage) -> DataFeed:
    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    until = datetime(2024, 1, 2, tzinfo=timezone.utc)
    bus = EventBus()
    return DataFeed(
        storage=storage,
        symbols=["BTC/USDT"],
        timeframe="1h",
        since=since,
        until=until,
        bus=bus,
    )


@pytest.fixture
def broker() -> SimulatedBroker:
    return SimulatedBroker(initial_cash=100_000.0, fee_rate=0.001, slippage_rate=0.001)


# ─────────────────────────────────────────────────────────────
# SimulatedBroker 单元测试
# ─────────────────────────────────────────────────────────────

class TestSimulatedBroker:
    def _make_kline(self, ts_offset: int = 0) -> KlineEvent:
        ts = datetime(2024, 1, 1, ts_offset, tzinfo=timezone.utc)
        return KlineEvent(
            event_type=EventType.KLINE_UPDATED,
            timestamp=ts,
            source="test",
            symbol="BTC/USDT",
            timeframe="1h",
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("95"),
            close=Decimal("102"),
            volume=Decimal("1000"),
        )

    def test_market_buy_executes_on_next_bar(self) -> None:
        """市价买单应在提交后的下一根 K 线成交，不在同一根 K 线内成交。"""
        broker = SimulatedBroker(initial_cash=10_000.0)

        bar0 = self._make_kline(ts_offset=0)  # 第 0 根 K 线
        bar1 = self._make_kline(ts_offset=1)  # 第 1 根 K 线

        # 在 bar0 通知 broker 时推进当前时间
        broker.on_kline(bar0)

        # 在 bar0 的时间上提交买单
        req = make_request(side="buy", qty=0.1, ts=bar0.timestamp)
        broker.submit_order(req)

        # bar0 上：不应成交（防未来函数）
        fills_bar0 = broker.on_kline(bar0)
        assert fills_bar0 == []

        # bar1 上：应该成交
        fills_bar1 = broker.on_kline(bar1)
        assert len(fills_bar1) == 1
        assert fills_bar1[0].side == "buy"

    def test_limit_buy_triggers_on_low(self) -> None:
        """限价买单在 K 线 low <= 限价时触发。"""
        broker = SimulatedBroker(initial_cash=10_000.0)
        bar = self._make_kline(ts_offset=0)

        req = make_request(side="buy", qty=0.1, price=96.0)  # low=95，应触发
        broker.submit_order(req)
        fills = broker.on_kline(bar)
        assert len(fills) == 1
        assert fills[0].avg_price == Decimal("96.0")

    def test_limit_buy_no_trigger_if_price_too_high(self) -> None:
        """限价买单价格高于 low 但不在范围内时不触发。"""
        broker = SimulatedBroker(initial_cash=10_000.0)
        bar = self._make_kline(ts_offset=0)

        req = make_request(side="buy", qty=0.1, price=90.0)  # low=95，不触发
        broker.submit_order(req)
        fills = broker.on_kline(bar)
        assert len(fills) == 0

    def test_sell_without_position_rejected(self) -> None:
        """无持仓时卖单应被拒绝（不崩溃）。"""
        broker = SimulatedBroker(initial_cash=10_000.0)
        bar = self._make_kline(ts_offset=0)
        req = make_request(side="sell", qty=1.0, price=103.0)
        broker.submit_order(req)
        fills = broker.on_kline(bar)
        assert fills == []

    def test_buy_reduces_cash(self) -> None:
        """买入后现金应减少。"""
        broker = SimulatedBroker(initial_cash=10_000.0)
        bar = self._make_kline(ts_offset=1)
        broker._current_ts = datetime(2024, 1, 1, 0, tzinfo=timezone.utc)

        req = make_request(side="buy", qty=1.0, ts=datetime(2024, 1, 1, 0, tzinfo=timezone.utc))
        broker.submit_order(req)
        broker.on_kline(bar)

        assert broker.get_cash() < Decimal("10000")
        assert broker.get_position("BTC/USDT") == Decimal("1.0")

    def test_insufficient_cash_rejected(self) -> None:
        """现金不足时，买单应被拒绝（不崩溃）。"""
        broker = SimulatedBroker(initial_cash=1.0)  # 只有 $1
        bar = self._make_kline(ts_offset=1)
        broker._current_ts = datetime(2024, 1, 1, 0, tzinfo=timezone.utc)

        req = make_request(side="buy", qty=10.0, ts=datetime(2024, 1, 1, 0, tzinfo=timezone.utc))
        broker.submit_order(req)
        fills = broker.on_kline(bar)
        assert fills == []
        assert broker.get_cash() == Decimal("1.0")  # 资金未变


# ─────────────────────────────────────────────────────────────
# BacktestEngine 集成测试
# ─────────────────────────────────────────────────────────────

class TestBacktestEngine:
    def test_empty_strategy_completes(
        self, feed: DataFeed, broker: SimulatedBroker
    ) -> None:
        """无策略时回测应正常完成，不产生任何成交。"""
        config = BacktestConfig(initial_cash=100_000.0)
        engine = BacktestEngine(feed=feed, broker=broker, config=config, bus=feed.bus)
        result = engine.run()

        assert result.metrics is not None
        assert result.trade_log.empty
        # 初始资金未变
        assert abs(result.metrics["final_equity"] - 100_000.0) < 0.01

    def test_buy_and_hold_strategy(
        self, storage: ParquetStorage, broker: SimulatedBroker
    ) -> None:
        """简单多头策略（第 0 根 K 线买入）应产生成交记录。"""
        since = datetime(2024, 1, 1, tzinfo=timezone.utc)
        until = datetime(2024, 1, 2, tzinfo=timezone.utc)
        bus = EventBus()
        feed = DataFeed(storage=storage, symbols=["BTC/USDT"], timeframe="1h",
                        since=since, until=until, bus=bus)

        config = BacktestConfig(initial_cash=100_000.0)
        engine = BacktestEngine(feed=feed, broker=broker, config=config, bus=bus)

        order_count = 0

        def buy_on_first_bar(event: KlineEvent) -> List[OrderRequestEvent]:
            nonlocal order_count
            if order_count == 0:
                order_count += 1
                return [make_request(side="buy", qty=1.0, ts=event.timestamp)]
            return []

        engine.add_strategy(buy_on_first_bar)
        result = engine.run()

        # 应有至少一条买入成交
        assert not result.trade_log.empty
        assert "buy" in result.trade_log["side"].values

    def test_equity_curve_length_matches_bars(
        self, feed: DataFeed, broker: SimulatedBroker
    ) -> None:
        """权益曲线的记录数应等于 K 线总数。"""
        config = BacktestConfig(initial_cash=100_000.0)
        engine = BacktestEngine(feed=feed, broker=broker, config=config, bus=feed.bus)
        result = engine.run()

        # DataFeed 加载的 K 线数量
        df_raw = make_ohlcv_df(n=20)
        since = datetime(2024, 1, 1, tzinfo=timezone.utc)
        until = datetime(2024, 1, 2, tzinfo=timezone.utc)
        expected = len(df_raw[
            (df_raw["timestamp"] >= pd.Timestamp(since)) &
            (df_raw["timestamp"] <= pd.Timestamp(until))
        ])
        assert len(result.equity_df) == expected

    def test_metrics_contain_required_keys(
        self, feed: DataFeed, broker: SimulatedBroker
    ) -> None:
        """绩效指标应包含所有必要字段。"""
        config = BacktestConfig(initial_cash=100_000.0)
        engine = BacktestEngine(feed=feed, broker=broker, config=config, bus=feed.bus)
        result = engine.run()

        required_keys = {
            "total_return", "cagr", "max_drawdown",
            "sharpe_ratio", "sortino_ratio", "calmar_ratio", "total_trades",
        }
        for key in required_keys:
            assert key in result.metrics, f"缺少指标: {key}"
