"""
modules/portfolio/optimizer.py — 均值-方差（Markowitz）优化器

设计说明：
均值-方差优化（MVO）是经典的组合优化理论，但在实践中有严重缺陷：
1. 对预期收益率极度敏感（微小估计误差 → 极端权重）
2. 历史协方差矩阵的估计本身就带有巨大的统计噪声

工业级改进：
- 使用 Ledoit-Wolf 收缩估计量（shrinkage estimator）代替样本协方差
- 使用 1/T 滚动收益率均值而非外部预测作为预期收益（保守）
- 加入权重约束（上下限、最小仓位）
- 最大化夏普比率而非单纯的期望收益（更实用）

本模块提供：
1. MeanVarianceOptimizer.max_sharpe(): 最大化夏普比率的权重
2. MeanVarianceOptimizer.min_variance(): 全局最小方差权重
3. MeanVarianceOptimizer.efficient_return(target): 目标收益率下的最小方差

注意：
- 本模块属于事前分析工具，周期性（如每周/每月）重新计算权重
- 不适用于每根 K 线都重新优化（计算代价高 + 过拟合）

接口：
    MeanVarianceOptimizer(use_shrinkage, risk_free_rate, n_portfolios)
    .fit(returns_df)               → self（拟合历史收益率）
    .max_sharpe()                  → Dict[str, float]（最大夏普权重）
    .min_variance()                → Dict[str, float]（最小方差权重）
    .efficient_frontier(n_points)  → pd.DataFrame（有效前沿数据点）
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.logger import get_logger

log = get_logger(__name__)


class MeanVarianceOptimizer:
    """
    均值-方差组合优化器。

    Args:
        use_shrinkage:  是否使用 Ledoit-Wolf 协方差收缩（默认 True）
        risk_free_rate: 年化无风险利率（用于夏普比率计算，默认 0.05）
        weight_cap:     单资产最大权重（默认 0.4）
        min_weight:     单资产最小权重（默认 0.0，允许退出）
        n_montecarlo:   蒙特卡洛模拟组合数（用于近似最优解，默认 5000）
    """

    def __init__(
        self,
        use_shrinkage: bool = True,
        risk_free_rate: float = 0.05,
        weight_cap: float = 0.40,
        min_weight: float = 0.0,
        n_montecarlo: int = 5000,
    ) -> None:
        self.use_shrinkage = use_shrinkage
        self.risk_free_rate = risk_free_rate
        self.weight_cap = weight_cap
        self.min_weight = min_weight
        self.n_montecarlo = n_montecarlo

        self._mu: Optional[np.ndarray] = None       # 预期收益率向量
        self._cov: Optional[np.ndarray] = None      # 协方差矩阵
        self._symbols: Optional[List[str]] = None
        self._n_bars: int = 0
        self._bars_per_year: int = 8760             # 1h K 线每年约 8760 根

    # ────────────────────────────────────────────────────────────
    # 拟合
    # ────────────────────────────────────────────────────────────

    def fit(
        self,
        returns_df: pd.DataFrame,
        bars_per_year: int = 8760,
    ) -> "MeanVarianceOptimizer":
        """
        拟合历史收益率数据。

        Args:
            returns_df:    列为各资产、行为各期收益率的 DataFrame
            bars_per_year: K 线频率对应的年化因子（1h=8760，1d=365）

        Returns:
            self（链式调用）
        """
        self._symbols = list(returns_df.columns)
        self._n_bars = len(returns_df)
        self._bars_per_year = bars_per_year

        # 预期收益率（使用简单历史均值 × 年化因子，保守估计）
        self._mu = returns_df.mean().values * bars_per_year

        # 协方差矩阵
        if self.use_shrinkage:
            self._cov = self._ledoit_wolf_shrinkage(returns_df) * bars_per_year
        else:
            self._cov = returns_df.cov().values * bars_per_year

        log.info(
            "MVO 拟合完成: {} 资产 / {} 期数据 / 年化因子={}",
            len(self._symbols), self._n_bars, bars_per_year,
        )
        return self

    # ────────────────────────────────────────────────────────────
    # 优化求解
    # ────────────────────────────────────────────────────────────

    def max_sharpe(self) -> Dict[str, float]:
        """
        求最大化夏普比率的组合权重。

        使用蒙特卡洛模拟 + 约束筛选近似求解（避免引入 scipy.optimize 硬依赖）。

        Returns:
            {symbol: 权重} 字典
        """
        self._check_fitted()
        return self._montecarlo_optimize(objective="sharpe")

    def min_variance(self) -> Dict[str, float]:
        """
        求全局最小方差组合权重。

        Returns:
            {symbol: 权重} 字典
        """
        self._check_fitted()
        return self._montecarlo_optimize(objective="variance")

    def efficient_frontier(
        self, n_points: int = 50
    ) -> pd.DataFrame:
        """
        生成有效前沿（风险-收益边界）数据点。

        Returns:
            DataFrame，含 columns=['symbol1', ..., 'exp_return', 'volatility', 'sharpe']
            用于可视化或进一步分析
        """
        self._check_fitted()
        n = len(self._symbols)
        rng = np.random.default_rng(42)

        records = []
        for _ in range(self.n_montecarlo):
            w = self._random_weights(rng, n)
            ret, vol, sharpe = self._portfolio_stats(w)
            record = {s: w[i] for i, s in enumerate(self._symbols)}
            record["exp_return"] = ret
            record["volatility"] = vol
            record["sharpe"] = sharpe
            records.append(record)

        df = pd.DataFrame(records)

        # 选出代表有效前沿的 n_points 个点（按波动率分层）
        df_sorted = df.sort_values("volatility")
        step = max(1, len(df_sorted) // n_points)
        return df_sorted.iloc[::step].reset_index(drop=True)

    def summary(self) -> pd.DataFrame:
        """
        返回各资产的年化收益/波动率/夏普比率摘要。
        """
        self._check_fitted()
        rows = []
        for i, s in enumerate(self._symbols):
            rows.append({
                "symbol": s,
                "exp_return_annual": float(self._mu[i]),
                "volatility_annual": float(np.sqrt(self._cov[i, i])),
                "sharpe": float(
                    (self._mu[i] - self.risk_free_rate)
                    / max(np.sqrt(self._cov[i, i]), 1e-8)
                ),
            })
        return pd.DataFrame(rows).set_index("symbol")

    # ────────────────────────────────────────────────────────────
    # 私有方法
    # ────────────────────────────────────────────────────────────

    def _montecarlo_optimize(self, objective: str = "sharpe") -> Dict[str, float]:
        """蒙特卡洛模拟 + 约束筛选求最优权重。"""
        n = len(self._symbols)
        rng = np.random.default_rng(seed=42)

        best_val = -np.inf if objective == "sharpe" else np.inf
        best_w = np.ones(n) / n

        for _ in range(self.n_montecarlo):
            w = self._random_weights(rng, n)
            ret, vol, sharpe = self._portfolio_stats(w)

            if objective == "sharpe":
                score = sharpe
                if score > best_val:
                    best_val = score
                    best_w = w
            else:  # min variance
                score = vol
                if score < best_val:
                    best_val = score
                    best_w = w

        log.info(
            "MVO {}: 最优{} = {:.4f}",
            objective,
            "夏普" if objective == "sharpe" else "波动率",
            best_val,
        )
        return {s: float(best_w[i]) for i, s in enumerate(self._symbols)}

    def _random_weights(self, rng: np.random.Generator, n: int) -> np.ndarray:
        """生成满足约束的随机权重。"""
        max_tries = 100
        for _ in range(max_tries):
            w = rng.dirichlet(np.ones(n))  # 和为 1 的随机权重
            # 应用约束
            w = np.clip(w, self.min_weight, self.weight_cap)
            total = w.sum()
            if total > 0:
                w /= total
            if np.all(w >= self.min_weight) and np.all(w <= self.weight_cap + 1e-8):
                return w
        # 退出时返回等权
        return np.ones(n) / n

    def _portfolio_stats(
        self, w: np.ndarray
    ) -> Tuple[float, float, float]:
        """计算给定权重下的组合期望收益/波动率/夏普比率（年化）。"""
        ret = float(w @ self._mu)
        vol = float(np.sqrt(w @ self._cov @ w))
        sharpe = (ret - self.risk_free_rate) / max(vol, 1e-8)
        return ret, vol, sharpe

    def _ledoit_wolf_shrinkage(self, returns_df: pd.DataFrame) -> np.ndarray:
        """
        Ledoit-Wolf 线性收缩估计量（简化版）。

        将样本协方差矩阵向均值方差矩阵（μI）收缩：
        Σ_shrunk = (1 - δ) * Σ_sample + δ * μ * I

        其中 δ 为最优收缩强度（此处使用 sklearn 实现，若不可用则退化）。
        """
        try:
            from sklearn.covariance import LedoitWolf
            lw = LedoitWolf()
            lw.fit(returns_df.values)
            return lw.covariance_
        except Exception:
            # 退化为样本协方差 + 对角正则化
            S = returns_df.cov().values
            S += np.eye(len(S)) * 1e-6
            return S

    def _check_fitted(self) -> None:
        if self._mu is None or self._cov is None:
            raise RuntimeError("请先调用 fit() 拟合历史收益率数据")
