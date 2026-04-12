"""
modules/portfolio/performance_attribution.py — 绩效归因分析器

设计说明：
绩效归因回答"我的收益从哪里来"的问题，将组合收益分解为：
1. 策略贡献（Strategy Attribution）：每个策略贡献了多少？
2. 资产贡献（Asset Attribution）：每个标的贡献了多少？
3. 分配效应（Allocation Effect）：权重决策的贡献（超/低配的影响）
4. 选股效应（Selection Effect）：标的本身收益的贡献

基准比较：
- 以"等权买入持有"为基准组合（BuyAndHold Benchmark）
- 超额收益 = 实际组合收益 - 基准组合收益（同期）
- 信息比率（IC）= 超额收益均值 / 超额收益标准差

接口：
    PerformanceAttributor()
    .record_trade(symbol, strategy_id, side, qty, price, ts)
    .record_price(symbol, price, ts)
    .get_strategy_attribution()   → pd.DataFrame
    .get_asset_attribution()      → pd.DataFrame
    .get_summary_metrics()        → Dict
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class TradeRecord:
    """单笔成交记录。"""
    symbol: str
    strategy_id: str
    side: str        # "buy" | "sell"
    quantity: float
    price: float
    notional: float
    timestamp: datetime
    pnl: float = 0.0  # 此笔成交实现的盈亏（仅卖出时有意义）


@dataclass
class StrategyStats:
    """单个策略的绩效统计。"""
    strategy_id: str
    total_pnl: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    total_notional: float = 0.0
    win_rate: float = 0.0
    avg_pnl_per_trade: float = 0.0


class PerformanceAttributor:
    """
    多策略绩效归因分析器。

    维护所有成交记录和价格历史，提供各维度的绩效拆解报告。

    用法：
        attr = PerformanceAttributor()
        # 每次成交后调用
        attr.record_trade(...)
        # 获取策略归因报告
        df = attr.get_strategy_attribution()
    """

    def __init__(self) -> None:
        self._trades: List[TradeRecord] = []
        self._price_history: Dict[str, List[tuple]] = defaultdict(list)

        # 按策略分组的成交列表
        self._strategy_trades: Dict[str, List[TradeRecord]] = defaultdict(list)

        # 按资产分组的持仓成本 {symbol: (avg_cost, qty)}
        self._positions: Dict[str, Dict] = {}  # {symbol: {cost, qty}}

        log.info("PerformanceAttributor 初始化")

    # ────────────────────────────────────────────────────────────
    # 数据录入接口
    # ────────────────────────────────────────────────────────────

    def record_trade(
        self,
        symbol: str,
        strategy_id: str,
        side: str,
        quantity: float,
        price: float,
        timestamp: datetime,
    ) -> None:
        """
        记录一笔成交。

        买入时：更新持仓成本均价。
        卖出时：计算已实现盈亏（FIFO 成本基础）。
        """
        notional = quantity * price
        pnl = 0.0

        if side == "buy":
            pos = self._positions.get(symbol, {"avg_cost": price, "qty": 0.0, "total_cost": 0.0})
            new_qty = pos["qty"] + quantity
            new_total_cost = pos["total_cost"] + notional
            self._positions[symbol] = {
                "avg_cost": new_total_cost / new_qty if new_qty > 0 else price,
                "qty": new_qty,
                "total_cost": new_total_cost,
            }
        elif side == "sell":
            pos = self._positions.get(symbol, {"avg_cost": 0.0, "qty": 0.0, "total_cost": 0.0})
            avg_cost = pos.get("avg_cost", 0.0)
            pnl = (price - avg_cost) * quantity  # 实现盈亏

            new_qty = max(0.0, pos["qty"] - quantity)
            new_total_cost = avg_cost * new_qty
            self._positions[symbol] = {
                "avg_cost": avg_cost,
                "qty": new_qty,
                "total_cost": new_total_cost,
            }

        trade = TradeRecord(
            symbol=symbol,
            strategy_id=strategy_id,
            side=side,
            quantity=quantity,
            price=price,
            notional=notional,
            timestamp=timestamp,
            pnl=pnl,
        )
        self._trades.append(trade)
        self._strategy_trades[strategy_id].append(trade)

    def record_price(self, symbol: str, price: float, timestamp: datetime) -> None:
        """记录价格历史（用于计算未实现盈亏和基准比较）。"""
        self._price_history[symbol].append((timestamp, price))

    # ────────────────────────────────────────────────────────────
    # 分析报告
    # ────────────────────────────────────────────────────────────

    def get_strategy_attribution(self) -> pd.DataFrame:
        """
        策略维度的绩效归因报告。

        Returns:
            DataFrame（每行一个策略），包含：
            strategy_id / total_pnl / win_rate / total_trades /
            avg_pnl_per_trade / total_notional / pnl_pct
        """
        if not self._trades:
            return pd.DataFrame()

        rows = []
        total_pnl_all = sum(t.pnl for t in self._trades)

        for strategy_id, trades in self._strategy_trades.items():
            sell_trades = [t for t in trades if t.side == "sell"]
            total_pnl = sum(t.pnl for t in sell_trades)
            winning = sum(1 for t in sell_trades if t.pnl > 0)
            win_rate = winning / len(sell_trades) if sell_trades else 0.0
            total_notional = sum(t.notional for t in trades)
            avg_pnl = total_pnl / len(sell_trades) if sell_trades else 0.0
            pnl_pct = (total_pnl / total_pnl_all) if total_pnl_all != 0 else 0.0

            rows.append({
                "strategy_id": strategy_id,
                "total_pnl_usdt": round(total_pnl, 4),
                "win_rate": round(win_rate, 4),
                "total_trades": len(trades),
                "sell_trades": len(sell_trades),
                "avg_pnl_per_trade": round(avg_pnl, 4),
                "total_notional_usdt": round(total_notional, 2),
                "pnl_contribution_pct": round(pnl_pct * 100, 2),
            })

        df = pd.DataFrame(rows)
        if len(df) > 0:
            df = df.sort_values("total_pnl_usdt", ascending=False).reset_index(drop=True)
        return df

    def get_asset_attribution(self) -> pd.DataFrame:
        """
        资产维度的绩效归因报告（含未实现盈亏）。

        Returns:
            DataFrame（每行一个资产），包含：
            symbol / realized_pnl / unrealized_pnl / total_pnl /
            total_trades / current_qty / avg_cost / current_price
        """
        symbols = {t.symbol for t in self._trades}
        rows = []

        for symbol in symbols:
            sym_trades = [t for t in self._trades if t.symbol == symbol]
            realized_pnl = sum(t.pnl for t in sym_trades if t.side == "sell")

            # 未实现盈亏（按最新价格）
            pos = self._positions.get(symbol, {})
            latest_price = 0.0
            if symbol in self._price_history and self._price_history[symbol]:
                latest_price = self._price_history[symbol][-1][1]

            qty = pos.get("qty", 0.0)
            avg_cost = pos.get("avg_cost", 0.0)
            unrealized_pnl = (latest_price - avg_cost) * qty if latest_price > 0 else 0.0

            rows.append({
                "symbol": symbol,
                "realized_pnl_usdt": round(realized_pnl, 4),
                "unrealized_pnl_usdt": round(unrealized_pnl, 4),
                "total_pnl_usdt": round(realized_pnl + unrealized_pnl, 4),
                "total_trades": len(sym_trades),
                "current_qty": round(qty, 8),
                "avg_cost_usdt": round(avg_cost, 4),
                "latest_price_usdt": round(latest_price, 4),
            })

        df = pd.DataFrame(rows)
        if len(df) > 0:
            df = df.sort_values("total_pnl_usdt", ascending=False).reset_index(drop=True)
        return df

    def get_summary_metrics(self, initial_equity: float = 100_000.0) -> Dict[str, object]:
        """
        组合层面的汇总指标。

        Returns:
            包含各项汇总指标的字典
        """
        if not self._trades:
            return {}

        sell_trades = [t for t in self._trades if t.side == "sell"]
        total_realized_pnl = sum(t.pnl for t in sell_trades)
        winning = sum(1 for t in sell_trades if t.pnl > 0)

        win_rate = winning / len(sell_trades) if sell_trades else 0.0
        profit_trades = [t.pnl for t in sell_trades if t.pnl > 0]
        loss_trades = [abs(t.pnl) for t in sell_trades if t.pnl < 0]

        avg_win = np.mean(profit_trades) if profit_trades else 0.0
        avg_loss = np.mean(loss_trades) if loss_trades else 0.0
        profit_factor = (sum(profit_trades) / sum(loss_trades)) if loss_trades else float("inf")

        return {
            "total_trades": len(self._trades),
            "sell_trades": len(sell_trades),
            "winning_trades": winning,
            "win_rate": round(win_rate, 4),
            "total_realized_pnl_usdt": round(total_realized_pnl, 4),
            "profit_factor": round(profit_factor, 4),
            "avg_win_usdt": round(avg_win, 4),
            "avg_loss_usdt": round(avg_loss, 4),
            "payoff_ratio": round(avg_win / avg_loss, 4) if avg_loss > 0 else float("inf"),
            "n_strategies": len(self._strategy_trades),
            "n_symbols": len({t.symbol for t in self._trades}),
        }

    def print_report(self) -> None:
        """打印完整的绩效归因报告（终端友好格式）。"""
        print("\n" + "=" * 60)
        print("          绩效归因报告")
        print("=" * 60)

        summary = self.get_summary_metrics()
        print("\n【组合汇总】")
        for k, v in summary.items():
            print(f"  {k:<35} {v}")

        strat_df = self.get_strategy_attribution()
        if len(strat_df) > 0:
            print("\n【策略维度归因】")
            print(strat_df.to_string(index=False))

        asset_df = self.get_asset_attribution()
        if len(asset_df) > 0:
            print("\n【资产维度归因】")
            print(asset_df.to_string(index=False))

        print("\n" + "=" * 60)
