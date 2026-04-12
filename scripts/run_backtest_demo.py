"""
scripts/run_backtest_demo.py — 端到端回测演示脚本

功能展示：
1. 生成合成历史数据（无需真实 API）
2. 运行 MACrossStrategy + RiskManager 完整回测
3. 打印绩效报告
4. 输出权益曲线到 CSV

运行方式：
    python scripts/run_backtest_demo.py

输出：
    - 控制台绩效报告
    - storage/demo_equity_curve.csv（权益曲线）
    - storage/demo_trade_log.csv（成交记录）

注意：这是演示脚本，使用合成正弦波价格数据
（模拟有趋势的加密市场，含噪声）。
实盘前必须替换为真实历史数据。
"""

from __future__ import annotations

import math
import random
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import List

import pandas as pd

# 确保项目根目录在 Python 路径中
sys.path.insert(0, str(Path(__file__).parent.parent))

from apps.backtest.broker import SimulatedBroker
from apps.backtest.engine import BacktestConfig, BacktestEngine
from core.event import EventBus, EventType, KlineEvent, OrderRequestEvent
from core.logger import setup_logging
from modules.alpha.strategies.ma_cross import MACrossStrategy
from modules.data.feed import DataFeed
from modules.data.storage import ParquetStorage
from modules.risk.manager import RiskConfig, RiskManager


# ──────────────────────────────────────────────────────────────
# 合成数据生成（演示用）
# ──────────────────────────────────────────────────────────────

def generate_synthetic_klines(
    n: int = 500,
    start: str = "2023-01-01",
    symbol: str = "BTC/USDT",
    base_price: float = 25000.0,
    trend: float = 0.0003,       # 每根 K 线的平均涨幅（0.03%）
    noise_pct: float = 0.015,    # 随机噪声幅度（1.5%）
    seed: int = 42,
) -> pd.DataFrame:
    """
    生成模拟 K 线数据（带趋势 + 噪声 + 正弦周期性）。

    这不是真实市场数据，仅用于演示系统功能。
    """
    random.seed(seed)
    timestamps = pd.date_range(start=start, periods=n, freq="1h", tz="UTC")

    closes = []
    price = base_price
    for i in range(n):
        # 趋势 + 正弦波 + 随机噪声
        sinusoid = math.sin(i * 2 * math.pi / 168) * 0.005  # 7 天周期
        change = trend + sinusoid + random.gauss(0, noise_pct / 3)
        price *= (1 + change)
        closes.append(max(price, 1.0))  # 价格不低于 1

    records = []
    for i, (ts, close) in enumerate(zip(timestamps, closes)):
        noise = random.uniform(0.005, 0.02)
        high = close * (1 + noise / 2)
        low = close * (1 - noise / 2)
        open_ = closes[i - 1] if i > 0 else close
        vol = random.uniform(100, 500) * (1 + abs(sinusoid) * 5)
        records.append({
            "timestamp": ts,
            "symbol": symbol,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": vol,
        })

    return pd.DataFrame(records)


# ──────────────────────────────────────────────────────────────
# 风控集成的策略包装器
# ──────────────────────────────────────────────────────────────

def build_risk_aware_strategy(
    strategy: MACrossStrategy,
    risk_manager: RiskManager,
    broker: SimulatedBroker,
):
    """
    构建带风控审核的策略包装函数。

    在策略信号产出和订单提交之间插入 RiskManager.check()。
    被拒绝的订单不进入 Broker，只记录日志。

    Returns:
        符合 BacktestEngine.add_strategy() 接口的包装函数
    """
    def wrapped_strategy(event: KlineEvent) -> List[OrderRequestEvent]:
        # 更新风控状态（权益 + 最高净值追踪）
        equity = float(broker.get_equity({event.symbol: float(event.close)}))
        risk_manager.update_equity(equity)

        # 获取策略信号
        raw_orders = strategy.on_kline(event)

        # 逐个进行风控审核
        approved_orders = []
        for order in raw_orders:
            allowed, reason = risk_manager.check(
                side=order.side,
                symbol=order.symbol,
                quantity=order.quantity,
                price=float(event.close),
                current_equity=equity,
                positions=dict(broker._positions),
            )
            if allowed:
                approved_orders.append(order)
            # 被拒绝的订单已在 RiskManager 内部记录

        return approved_orders

    wrapped_strategy.__name__ = f"{strategy.strategy_id}[risk_managed]"
    return wrapped_strategy


# ──────────────────────────────────────────────────────────────
# 主函数
# ──────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging(log_level="WARNING")  # 演示时只显示警告以上
    print("\n" + "=" * 60)
    print("  AI 驱动加密货币量化交易系统 — 端到端回测演示")
    print("=" * 60)

    # ── 1. 准备合成数据 ──────────────────────────────────────
    print("\n[1/5] 生成合成历史数据...")
    symbol = "BTC/USDT"
    df = generate_synthetic_klines(n=1000, symbol=symbol)
    print(f"      生成 {len(df)} 根 1h K 线，时间范围: {df['timestamp'].iloc[0]} ~ {df['timestamp'].iloc[-1]}")

    # 存入 Parquet（演示用 ./storage/demo 目录）
    storage_dir = Path("./storage/demo")
    storage = ParquetStorage(root_dir=storage_dir, exchange_id="synthetic")
    storage.write(df, symbol=symbol, timeframe="1h")
    print(f"      数据已落盘: {storage_dir}/synthetic/BTC_USDT/1h.parquet")

    # ── 2. 初始化组件 ────────────────────────────────────────
    print("\n[2/5] 初始化回测组件...")
    initial_cash = 100_000.0  # USDT

    since = df["timestamp"].iloc[0].to_pydatetime()
    until = df["timestamp"].iloc[-1].to_pydatetime()

    bus = EventBus()
    feed = DataFeed(
        storage=storage,
        symbols=[symbol],
        timeframe="1h",
        since=since,
        until=until,
        bus=bus,
    )

    broker = SimulatedBroker(
        initial_cash=initial_cash,
        fee_rate=0.001,
        slippage_rate=0.001,
    )

    # ── 3. 配置策略与风控 ────────────────────────────────────
    print("\n[3/5] 配置策略与风控...")
    strategy = MACrossStrategy(
        symbol=symbol,
        fast_window=10,
        slow_window=30,
        order_qty=0.1,     # 每次 0.1 BTC
        volume_filter=True,
        timeframe="1h",
    )

    risk_config = RiskConfig(
        max_position_pct=0.20,
        max_portfolio_drawdown=0.15,
        max_daily_loss=0.05,
        max_consecutive_losses=5,
    )
    risk_manager = RiskManager(risk_config)

    print(f"      策略: {strategy.strategy_id}")
    print(f"      风控: max_pos={risk_config.max_position_pct*100:.0f}%"
          f" max_dd={risk_config.max_portfolio_drawdown*100:.0f}%"
          f" max_daily_loss={risk_config.max_daily_loss*100:.0f}%")

    # ── 4. 运行回测 ──────────────────────────────────────────
    print("\n[4/5] 运行回测...")
    config = BacktestConfig(
        initial_cash=initial_cash,
        fee_rate=0.001,
        slippage_rate=0.001,
    )
    engine = BacktestEngine(feed=feed, broker=broker, config=config, bus=bus)

    # 使用风控包装器注册策略
    engine.add_strategy(
        build_risk_aware_strategy(strategy, risk_manager, broker)
    )

    result = engine.run()
    print(f"      回测完成，共 {len(result.equity_df)} 个时间步")

    # ── 5. 输出报告 ──────────────────────────────────────────
    print("\n[5/5] 绩效报告:")
    result.reporter.print_report()

    # 风控状态摘要
    risk_state = risk_manager.get_state_summary()
    print("\n风控状态:")
    print(f"  熔断状态: {'🔴 已熔断' if risk_state['circuit_broken'] else '🟢 正常'}")
    print(f"  连续亏损: {risk_state['consecutive_losses']} 次")

    # 保存 CSV
    output_dir = Path("./storage")
    output_dir.mkdir(parents=True, exist_ok=True)

    equity_path = output_dir / "demo_equity_curve.csv"
    trade_path = output_dir / "demo_trade_log.csv"

    result.equity_df.to_csv(equity_path, index=False)
    result.trade_log.to_csv(trade_path, index=False)

    print(f"\n📊 权益曲线已保存: {equity_path}")
    print(f"📋 成交记录已保存: {trade_path}")
    print(f"   共 {len(result.trade_log)} 笔成交记录\n")


if __name__ == "__main__":
    main()
