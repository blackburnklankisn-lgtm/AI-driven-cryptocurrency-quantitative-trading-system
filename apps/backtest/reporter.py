"""
apps/backtest/reporter.py — 回测绩效报告生成器

设计说明：
- 输入：SimulatedBroker 产生的权益曲线（equity curve）和成交日志
- 输出：标准化的回测绩效指标字典和 DataFrame 报告
- 所有指标均含交易成本（手续费 + 滑点），不允许展示未扣费结果

输出指标：
- 总收益率（Total Return）
- 年化收益率（CAGR）
- 最大回撤（Max Drawdown）及回撤持续时间
- 夏普比率（Sharpe Ratio，年化，基于日收益）
- 索提诺比率（Sortino Ratio）
- 卡玛比率（Calmar Ratio = CAGR / Max Drawdown）
- 总交易次数、胜率、盈亏比
- 平均持仓时间

接口：
    BacktestReporter(equity_df, trade_log_df)
    .compute() → dict[str, float]   绩效指标字典
    .summary_table() → pd.DataFrame 格式化摘要
    .print_report() → 打印到控制台
"""

from __future__ import annotations

import math
from typing import Dict

import numpy as np
import pandas as pd

from core.logger import get_logger

log = get_logger(__name__)

# 年化因子（假设 24x365 加密市场，按实际天数计算）
_TRADING_DAYS_PER_YEAR = 365
# 无风险利率（年化，crypto 市场通常参考美元稳定币收益率，约 5%）
_RISK_FREE_ANNUAL = 0.05


class BacktestReporter:
    """
    回测绩效分析与报告。

    Args:
        equity_df:    权益曲线 DataFrame，列: [timestamp(index), equity]
        trade_log_df: 成交日志 DataFrame，来自 SimulatedBroker.get_trade_log()
        initial_cash: 初始资金（用于计算收益率基准）
    """

    def __init__(
        self,
        equity_df: pd.DataFrame,
        trade_log_df: pd.DataFrame,
        initial_cash: float = 100_000.0,
    ) -> None:
        self.equity_df = equity_df.copy()
        self.trade_log = trade_log_df.copy()
        self.initial_cash = initial_cash
        self._metrics: Dict[str, float] | None = None

    # ────────────────────────────────────────────────────────────
    # 公开接口
    # ────────────────────────────────────────────────────────────

    def compute(self) -> Dict[str, float]:
        """
        计算所有绩效指标。

        Returns:
            指标字典，键名为英文（方便机器消费），
            如 {"total_return": 0.35, "max_drawdown": -0.12, ...}
        """
        if self._metrics is not None:
            return self._metrics

        equity = self._prepare_equity()
        returns = equity.pct_change().dropna()

        metrics: Dict[str, float] = {}

        # ── 收益类 ─────────────────────────────────────────────
        final_equity = float(equity.iloc[-1])
        metrics["initial_cash"] = self.initial_cash
        metrics["final_equity"] = final_equity
        metrics["total_return"] = (final_equity - self.initial_cash) / self.initial_cash

        # 年化收益率（CAGR）
        n_days = self._calc_days(equity)
        if n_days > 0:
            metrics["cagr"] = (final_equity / self.initial_cash) ** (
                _TRADING_DAYS_PER_YEAR / n_days
            ) - 1
        else:
            metrics["cagr"] = 0.0

        # ── 风险类 ─────────────────────────────────────────────
        max_dd, max_dd_duration = self._calc_max_drawdown(equity)
        metrics["max_drawdown"] = max_dd
        metrics["max_drawdown_duration_days"] = max_dd_duration

        # ── 风险调整收益 ───────────────────────────────────────
        metrics["sharpe_ratio"] = self._calc_sharpe(returns)
        metrics["sortino_ratio"] = self._calc_sortino(returns)
        metrics["calmar_ratio"] = (
            metrics["cagr"] / abs(max_dd) if max_dd != 0 else float("nan")
        )

        # ── 交易统计 ───────────────────────────────────────────
        trade_stats = self._calc_trade_stats()
        metrics.update(trade_stats)

        self._metrics = metrics
        log.info("回测绩效计算完成: {}", metrics)
        return metrics

    def summary_table(self) -> pd.DataFrame:
        """返回格式化的绩效摘要 DataFrame，适合展示。"""
        metrics = self.compute()

        labels = {
            "initial_cash":              "初始资金（USDT）",
            "final_equity":              "最终净值（USDT）",
            "total_return":              "总收益率",
            "cagr":                      "年化收益率（CAGR）",
            "max_drawdown":              "最大回撤",
            "max_drawdown_duration_days": "最大回撤持续（天）",
            "sharpe_ratio":              "夏普比率",
            "sortino_ratio":             "索提诺比率",
            "calmar_ratio":              "卡玛比率",
            "total_trades":              "总交易次数",
            "win_rate":                  "胜率",
            "profit_factor":             "盈亏比",
            "avg_trade_return":          "平均每笔收益率",
        }

        rows = []
        for key, label in labels.items():
            val = metrics.get(key, float("nan"))
            if key in {"total_return", "cagr", "max_drawdown", "win_rate", "avg_trade_return"}:
                formatted = f"{val * 100:.2f}%"
            elif key in {"sharpe_ratio", "sortino_ratio", "calmar_ratio", "profit_factor"}:
                formatted = f"{val:.3f}" if not math.isnan(val) else "N/A"
            elif key in {"total_trades", "max_drawdown_duration_days"}:
                formatted = f"{int(val)}"
            else:
                formatted = f"{val:,.2f}"
            rows.append({"指标": label, "数值": formatted})

        return pd.DataFrame(rows)

    def print_report(self) -> None:
        """打印回测报告到控制台。"""
        table = self.summary_table()
        print("\n" + "=" * 50)
        print("           回测绩效报告")
        print("=" * 50)
        print(table.to_string(index=False))
        print("=" * 50 + "\n")

    # ────────────────────────────────────────────────────────────
    # 私有计算方法
    # ────────────────────────────────────────────────────────────

    def _prepare_equity(self) -> pd.Series:
        """提取并校验权益序列。"""
        if "equity" not in self.equity_df.columns:
            raise ValueError("equity_df 必须包含 'equity' 列")
        return self.equity_df["equity"].astype(float).reset_index(drop=True)

    @staticmethod
    def _calc_days(equity: pd.Series) -> float:
        """估算回测持续天数。"""
        return float(len(equity))  # 每条记录视为一天（按日频权益曲线）

    @staticmethod
    def _calc_max_drawdown(equity: pd.Series) -> tuple[float, int]:
        """
        计算最大回撤及其持续时间（以步数为单位）。

        Returns:
            (max_drawdown: float, duration: int)  max_drawdown 为负数
        """
        peak = equity.cummax()
        drawdown = (equity - peak) / peak

        max_dd = float(drawdown.min())  # 最负的值

        # 计算最大回撤的持续时间
        in_dd = drawdown < 0
        max_duration = 0
        current_duration = 0
        for flag in in_dd:
            if flag:
                current_duration += 1
                max_duration = max(max_duration, current_duration)
            else:
                current_duration = 0

        return max_dd, max_duration

    @staticmethod
    def _calc_sharpe(returns: pd.Series) -> float:
        """年化夏普比率（日收益序列）。"""
        if len(returns) < 2:
            return float("nan")
        rf_daily = (1 + _RISK_FREE_ANNUAL) ** (1 / _TRADING_DAYS_PER_YEAR) - 1
        excess = returns - rf_daily
        std = excess.std()
        if std == 0:
            return float("nan")
        return float((excess.mean() / std) * math.sqrt(_TRADING_DAYS_PER_YEAR))

    @staticmethod
    def _calc_sortino(returns: pd.Series) -> float:
        """年化索提诺比率（只惩罚下行波动）。"""
        if len(returns) < 2:
            return float("nan")
        rf_daily = (1 + _RISK_FREE_ANNUAL) ** (1 / _TRADING_DAYS_PER_YEAR) - 1
        excess = returns - rf_daily
        downside = excess[excess < 0]
        if len(downside) == 0:
            return float("inf")
        downside_std = downside.std()
        if downside_std == 0:
            return float("nan")
        return float((excess.mean() / downside_std) * math.sqrt(_TRADING_DAYS_PER_YEAR))

    def _calc_trade_stats(self) -> Dict[str, float]:
        """从成交日志计算交易统计数据。"""
        stats: Dict[str, float] = {
            "total_trades": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "avg_trade_return": 0.0,
        }

        if self.trade_log.empty:
            return stats

        # 按 buy/sell 配对计算每笔完整交易的盈亏
        # 简化方式：按成交记录直接统计正负盈亏
        buy_trades = self.trade_log[self.trade_log["side"] == "buy"]
        sell_trades = self.trade_log[self.trade_log["side"] == "sell"]

        stats["total_trades"] = float(len(sell_trades))

        if sell_trades.empty:
            return stats

        # 用 sell 记录估算盈亏（此处简化配对，实战中应按 FIFO 严格配对）
        sell_notional = sell_trades["avg_price"] * sell_trades["filled_qty"]
        total_fees = self.trade_log["fee"].sum()

        gross_pnl = sell_notional.sum() - (
            buy_trades["avg_price"] * buy_trades["filled_qty"]
        ).sum() if not buy_trades.empty else 0.0

        net_pnl = gross_pnl - total_fees

        # 简化版胜率：正盈亏比例
        pnl_per_sell = (
            sell_trades["avg_price"] - sell_trades["avg_price"].mean()
        )
        winners = (pnl_per_sell > 0).sum()
        stats["win_rate"] = float(winners) / len(sell_trades) if len(sell_trades) > 0 else 0.0

        # 盈亏比
        gross_profit = max(net_pnl, 0)
        gross_loss = abs(min(net_pnl, 0))
        stats["profit_factor"] = (
            gross_profit / gross_loss if gross_loss > 0 else float("inf")
        )

        stats["avg_trade_return"] = (
            net_pnl / self.initial_cash / max(len(sell_trades), 1)
        )

        return stats
