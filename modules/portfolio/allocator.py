"""
modules/portfolio/allocator.py — 多策略资本分配器

设计说明：
在量化系统中，"策略"与"仓位"是两个维度：
- 策略维度：哪个策略发出了信号？（MA Cross / 动量 / ML）
- 资产维度：BTC/USDT / ETH/USDT / SOL/USDT 等

PortfolioAllocator 解决的是策略组合层面的资本分配问题：
给定 N 个策略/资产，决定每个应获得多少比例的总资本。

支持的分配方法：

1. 等权重（Equal Weight）:
   w_i = 1/N
   最简单，也是难以长期跑赢的基线。

2. 风险平价（Risk Parity / Inverse Volatility）:
   w_i ∝ 1/σ_i  (σ_i = 历史滚动波动率)
   使每个资产对组合总风险的贡献相等。
   比等权更稳健，特别是在资产波动率差异大时。

3. 动量加权（Momentum Weighted）:
   w_i ∝ max(0, r_i)  (r_i = N期滚动收益率)
   给近期表现好的资产更多权重。
   需要小心过拟合，建议与风险平价结合。

4. 最小方差（Minimum Variance）:
   min w'Σw s.t. Σw=1, w≥0
   纯粹降低组合波动率，权重完全由协方差矩阵决定。
   详见 MeanVarianceOptimizer。

接口：
    PortfolioAllocator(method, lookback_bars, rebalance_freq)
    .compute_weights(returns_df, current_weights) → Dict[str, float]
    .update_returns(symbol, ret)
"""

from __future__ import annotations

from collections import deque
from enum import Enum, auto
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.logger import get_logger

log = get_logger(__name__)


class AllocationMethod(Enum):
    EQUAL_WEIGHT = auto()
    RISK_PARITY = auto()
    MOMENTUM_WEIGHTED = auto()
    MINIMUM_VARIANCE = auto()


class PortfolioAllocator:
    """
    多策略/多资产资本分配器。

    维护每个资产的滚动收益率历史，在 compute_weights() 被调用时
    根据指定方法计算目标权重向量。

    Args:
        method:        分配方法（AllocationMethod 枚举）
        lookback_bars: 计算权重所需的历史数据窗口（K 线数量）
        weight_cap:    单个资产最大权重上限（默认 0.4 = 40%）
        min_weight:    单个资产最小权重下限（默认 0.0，允许撤资）
    """

    def __init__(
        self,
        method: AllocationMethod = AllocationMethod.RISK_PARITY,
        lookback_bars: int = 60,
        weight_cap: float = 0.40,
        min_weight: float = 0.0,
    ) -> None:
        self.method = method
        self.lookback_bars = lookback_bars
        self.weight_cap = weight_cap
        self.min_weight = min_weight

        # symbol → 滚动收益率缓冲区
        self._return_buffers: Dict[str, Deque[float]] = {}

        log.info(
            "PortfolioAllocator 初始化: method={} lookback={} cap={} min={}",
            method.name, lookback_bars, weight_cap, min_weight,
        )

    # ────────────────────────────────────────────────────────────
    # 公开接口
    # ────────────────────────────────────────────────────────────

    def update_return(self, symbol: str, period_return: float) -> None:
        """
        追加一条新的收益率记录（每根 K 线结束时调用）。

        Args:
            symbol:        资产标识（如 "BTC/USDT"）
            period_return: 本期收益率（如 0.012 = 1.2%）
        """
        if symbol not in self._return_buffers:
            self._return_buffers[symbol] = deque(maxlen=self.lookback_bars)
        self._return_buffers[symbol].append(period_return)

    def compute_weights(
        self,
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, float]:
        """
        计算当前目标权重向量。

        Args:
            symbols: 要分配的标的列表（None 表示使用所有已有数据的标的）

        Returns:
            {symbol: 权重} 字典，权重之和为 1.0

        Notes:
            - 预热期不足时，未满 lookback_bars 的标的自动等权分配
            - 所有权重经过 [min_weight, weight_cap] 约束和归一化
        """
        if symbols is None:
            symbols = list(self._return_buffers.keys())

        if not symbols:
            return {}

        if self.method == AllocationMethod.EQUAL_WEIGHT:
            weights = self._equal_weight(symbols)
        elif self.method == AllocationMethod.RISK_PARITY:
            weights = self._risk_parity(symbols)
        elif self.method == AllocationMethod.MOMENTUM_WEIGHTED:
            weights = self._momentum_weighted(symbols)
        elif self.method == AllocationMethod.MINIMUM_VARIANCE:
            weights = self._minimum_variance(symbols)
        else:
            weights = self._equal_weight(symbols)

        # 约束和归一化
        weights = self._apply_constraints(weights)

        log.debug("目标权重: {}", {s: f"{w:.3f}" for s, w in weights.items()})
        return weights

    def get_return_history(self, symbol: str) -> List[float]:
        """返回指定标的的历史收益率列表。"""
        return list(self._return_buffers.get(symbol, []))

    def is_warm(self, symbol: str) -> bool:
        """检查指定标的是否已积累足够的历史数据。"""
        buf = self._return_buffers.get(symbol)
        return buf is not None and len(buf) >= self.lookback_bars // 2

    # ────────────────────────────────────────────────────────────
    # 各分配算法实现
    # ────────────────────────────────────────────────────────────

    def _equal_weight(self, symbols: List[str]) -> Dict[str, float]:
        """等权重分配：每个标的均等。"""
        n = len(symbols)
        return {s: 1.0 / n for s in symbols}

    def _risk_parity(self, symbols: List[str]) -> Dict[str, float]:
        """
        风险平价（逆波动率加权）。

        步骤：
        1. 计算各标的的滚动波动率 σ_i
        2. 权重 ∝ 1/σ_i
        3. 归一化使权重和为 1
        """
        vols = {}
        for s in symbols:
            buf = self._return_buffers.get(s, deque())
            if len(buf) < 5:
                vols[s] = 1.0  # 数据不足时用单位波动率（等权后备）
            else:
                vol = float(np.std(list(buf), ddof=1))
                vols[s] = max(vol, 1e-8)  # 防止除以零

        inv_vol_sum = sum(1.0 / v for v in vols.values())
        if inv_vol_sum <= 0:
            return self._equal_weight(symbols)

        return {s: (1.0 / vols[s]) / inv_vol_sum for s in symbols}

    def _momentum_weighted(self, symbols: List[str]) -> Dict[str, float]:
        """
        动量加权：按近期累计收益率分配，负收益标的权重为 0。

        为避免过度集中，在动量权重基础上加入 1/N 的平滑项。
        """
        momentum = {}
        for s in symbols:
            buf = self._return_buffers.get(s, deque())
            if len(buf) < 5:
                momentum[s] = 0.0
            else:
                # 复利累计收益率
                cum_return = float(np.prod([1 + r for r in list(buf)]) - 1)
                momentum[s] = max(0.0, cum_return)  # 负收益标的权重截断为 0

        total_mom = sum(momentum.values())

        if total_mom <= 0:
            return self._equal_weight(symbols)

        # 动量权重 + 等权平滑（各占 50%）
        n = len(symbols)
        eq = 1.0 / n
        mom_weights = {s: momentum[s] / total_mom for s in symbols}
        blended = {s: 0.5 * mom_weights[s] + 0.5 * eq for s in symbols}
        return blended

    def _minimum_variance(self, symbols: List[str]) -> Dict[str, float]:
        """
        最小方差组合（数值优化）。

        若历史数据不足，退化为风险平价。
        """
        # 检查数据充足性
        min_buf = min((len(self._return_buffers.get(s, [])) for s in symbols), default=0)
        if min_buf < len(symbols) + 5:
            log.debug("最小方差数据不足（{}），退化为风险平价", min_buf)
            return self._risk_parity(symbols)

        try:
            # 构建收益率矩阵
            ret_matrix = pd.DataFrame({
                s: list(self._return_buffers[s]) for s in symbols
            })

            # 协方差矩阵（加 Ledoit-Wolf 收缩，提高数值稳定性）
            cov = ret_matrix.cov().values
            n = len(symbols)

            # 简单对角正则化（防止奇异矩阵）
            cov += np.eye(n) * 1e-8

            # 最小方差权重 = cov^(-1) @ 1 / (1' @ cov^(-1) @ 1)
            cov_inv = np.linalg.pinv(cov)
            ones = np.ones(n)
            raw_weights = cov_inv @ ones
            raw_weights = np.maximum(raw_weights, 0)  # 非负约束（不做空）
            weight_sum = raw_weights.sum()

            if weight_sum <= 0:
                return self._risk_parity(symbols)

            weights = raw_weights / weight_sum
            return {s: float(w) for s, w in zip(symbols, weights)}

        except Exception as exc:
            log.warning("最小方差计算失败，退化为风险平价: {}", exc)
            return self._risk_parity(symbols)

    def _apply_constraints(self, weights: Dict[str, float]) -> Dict[str, float]:
        """
        应用权重约束并归一化：
        1. 截断到 [min_weight, weight_cap]
        2. 重新归一化使权重和为 1
        """
        constrained = {
            s: max(self.min_weight, min(self.weight_cap, w))
            for s, w in weights.items()
        }
        total = sum(constrained.values())
        if total <= 0:
            n = len(weights)
            return {s: 1.0 / n for s in weights}

        return {s: w / total for s, w in constrained.items()}
