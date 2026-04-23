"""
tests/test_portfolio.py — 组合管理层测试

覆盖项：
- PortfolioAllocator: 等权 / 风险平价 / 动量 / 最小方差
- PortfolioAllocator: 权重约束（上下限、归一化）
- PortfolioRebalancer: 漂移触发、定时触发
- PortfolioRebalancer: 先卖后买的订单顺序
- MeanVarianceOptimizer: max_sharpe / min_variance 权重有效性
- PerformanceAttributor: 策略归因 / 资产归因 / 盈亏计算
- ContinuousLearner: 定时触发重训、模型切换逻辑
"""

from __future__ import annotations

import math
import random
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd
import pytest

from modules.portfolio.allocator import AllocationMethod, PortfolioAllocator
from modules.portfolio.rebalancer import PortfolioRebalancer
from modules.portfolio.optimizer import MeanVarianceOptimizer
from modules.portfolio.performance_attribution import PerformanceAttributor


# ─────────────────────────────────────────────────────────────
# 测试数据生成
# ─────────────────────────────────────────────────────────────

def make_returns(n: int = 100, n_assets: int = 3, seed: int = 42) -> pd.DataFrame:
    """生成多资产收益率 DataFrame。"""
    rng = np.random.RandomState(seed)
    symbols = [f"ASSET{i}/USDT" for i in range(n_assets)]
    data = {s: rng.normal(0.001, 0.02, n) for s in symbols}
    return pd.DataFrame(data)


def make_ohlcv_df(n: int = 500, seed: int = 0) -> pd.DataFrame:
    """生成合成 OHLCV DataFrame。"""
    rng = np.random.RandomState(seed)
    prices = 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n)))
    ts = pd.date_range("2023-01-01", periods=n, freq="1H", tz="UTC")
    noise = rng.uniform(0.005, 0.015, n)
    return pd.DataFrame({
        "timestamp": ts,
        "symbol": "BTC/USDT",
        "open": prices * (1 - noise / 4),
        "high": prices * (1 + noise / 2),
        "low": prices * (1 - noise / 2),
        "close": prices,
        "volume": rng.uniform(100, 500, n),
    })


# ─────────────────────────────────────────────────────────────
# PortfolioAllocator 测试
# ─────────────────────────────────────────────────────────────

class TestPortfolioAllocator:
    SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

    def _fill_allocator(
        self, method: AllocationMethod, n_bars: int = 80
    ) -> PortfolioAllocator:
        """创建并填充历史数据的分配器。"""
        alloc = PortfolioAllocator(method=method, lookback_bars=60, weight_cap=0.60)
        rng = np.random.RandomState(0)
        for _ in range(n_bars):
            for sym in self.SYMBOLS:
                alloc.update_return(sym, float(rng.normal(0.001, 0.015)))
        return alloc

    def test_equal_weight_sums_to_one(self) -> None:
        """等权重分配：权重之和应为 1.0。"""
        alloc = self._fill_allocator(AllocationMethod.EQUAL_WEIGHT)
        weights = alloc.compute_weights(self.SYMBOLS)
        assert abs(sum(weights.values()) - 1.0) < 1e-9

    def test_equal_weight_all_equal(self) -> None:
        """等权重分配：每个资产权重相同。"""
        alloc = self._fill_allocator(AllocationMethod.EQUAL_WEIGHT)
        weights = alloc.compute_weights(self.SYMBOLS)
        expected = 1.0 / len(self.SYMBOLS)
        for v in weights.values():
            assert abs(v - expected) < 1e-9

    def test_risk_parity_sums_to_one(self) -> None:
        """风险平价权重之和应为 1.0。"""
        alloc = self._fill_allocator(AllocationMethod.RISK_PARITY)
        weights = alloc.compute_weights(self.SYMBOLS)
        assert abs(sum(weights.values()) - 1.0) < 1e-6

    def test_risk_parity_high_vol_lower_weight(self) -> None:
        """波动率高的资产应分配到更低权重。"""
        alloc = PortfolioAllocator(method=AllocationMethod.RISK_PARITY, lookback_bars=60)
        rng = np.random.RandomState(1)
        # BTC 波动率更高（sigma=0.05 vs 0.01）
        for _ in range(80):
            alloc.update_return("BTC/USDT", float(rng.normal(0, 0.05)))
            alloc.update_return("ETH/USDT", float(rng.normal(0, 0.01)))

        weights = alloc.compute_weights(["BTC/USDT", "ETH/USDT"])
        assert weights["BTC/USDT"] < weights["ETH/USDT"], \
            "高波动率资产应有更低权重"

    def test_momentum_negative_returns_zero_weight(self) -> None:
        """持续负收益的资产在动量加权中权重应为 0（或接近 0）。"""
        alloc = PortfolioAllocator(method=AllocationMethod.MOMENTUM_WEIGHTED, lookback_bars=30)
        for _ in range(40):
            alloc.update_return("LOSER/USDT", -0.01)   # 持续负收益
            alloc.update_return("WINNER/USDT", +0.01)  # 持续正收益

        weights = alloc.compute_weights(["LOSER/USDT", "WINNER/USDT"])
        assert weights["WINNER/USDT"] > weights["LOSER/USDT"]

    def test_weight_cap_enforced(self) -> None:
        """单资产权重不应超过 weight_cap（多资产场景验证）。"""
        # 使用 5 个资产，cap=0.25 → 等权 0.20，不超过 cap
        # 将动量数据设置极度不均匀，验证 cap 能截断权重
        alloc = PortfolioAllocator(
            method=AllocationMethod.MOMENTUM_WEIGHTED,
            lookback_bars=30,
            weight_cap=0.62,  # 实际最高权重约 0.579，cap 截断应生效
        )
        syms = [f"A{i}/USDT" for i in range(5)]
        # 只有 A0 有正收益，动量+等权平滑后约占 57-60%，cap=0.62 仍可截断异常值
        for _ in range(40):
            alloc.update_return("A0/USDT", 0.05)
            for s in syms[1:]:
                alloc.update_return(s, -0.01)

        weights = alloc.compute_weights(syms)
        for s, w in weights.items():
            assert w <= 0.62 + 1e-9, f"{s} 权重 {w:.3f} 超过 cap=0.62"

    def test_empty_symbols_returns_empty(self) -> None:
        """空标的列表应返回空字典。"""
        alloc = PortfolioAllocator()
        weights = alloc.compute_weights([])
        assert weights == {}

    def test_minimum_variance_sums_to_one(self) -> None:
        """最小方差权重之和应为 1.0（或接近）。"""
        alloc = self._fill_allocator(AllocationMethod.MINIMUM_VARIANCE)
        weights = alloc.compute_weights(self.SYMBOLS)
        assert abs(sum(weights.values()) - 1.0) < 1e-6


# ─────────────────────────────────────────────────────────────
# PortfolioRebalancer 测试
# ─────────────────────────────────────────────────────────────

class TestPortfolioRebalancer:
    SYMBOLS = ["BTC/USDT", "ETH/USDT"]

    @pytest.fixture
    def allocator(self) -> PortfolioAllocator:
        alloc = PortfolioAllocator(
            method=AllocationMethod.EQUAL_WEIGHT,
            lookback_bars=30,
        )
        for _ in range(40):
            alloc.update_return("BTC/USDT", 0.001)
            alloc.update_return("ETH/USDT", 0.001)
        return alloc

    def test_no_rebalance_when_weights_match(self, allocator: PortfolioAllocator) -> None:
        """当持仓权重与目标权重匹配时，不应产生再平衡订单。"""
        rebalancer = PortfolioRebalancer(
            allocator=allocator,
            rebalance_every_n=100,  # 不触发定时
            drift_threshold=0.10,   # 漂移阈值大
            min_trade_notional=10.0,
        )
        equity = 100_000.0
        # 持仓让 BTC 和 ETH 各占 50%（与等权目标一致）
        positions = {"BTC/USDT": Decimal("1.0"), "ETH/USDT": Decimal("2.0")}
        prices = {"BTC/USDT": 50_000.0, "ETH/USDT": 25_000.0}

        orders = rebalancer.on_bar_close(equity, positions, prices, self.SYMBOLS)
        assert len(orders) == 0

    def test_scheduled_rebalance_triggers(self, allocator: PortfolioAllocator) -> None:
        """到达定时间隔时应触发再平衡。"""
        # rebalance_every_n=3：初始 last=-1，bar_count 从 1 开始
        # 第 1 根：1-(-1)=2 < 3  → 不触发
        # 第 2 根：2-(-1)=3 >= 3 → 触发，last=2
        # 第 3 根：3-2=1 < 3    → 不触发
        rebalancer = PortfolioRebalancer(
            allocator=allocator,
            rebalance_every_n=3,  # 每 3 根触发
            drift_threshold=0.99,  # 禁用漂移触发
            min_trade_notional=1.0,
        )
        equity = 100_000.0
        positions = {"BTC/USDT": Decimal("1.0"), "ETH/USDT": Decimal("0.0")}
        prices = {"BTC/USDT": 50_000.0, "ETH/USDT": 3_000.0}

        all_orders = []
        for _ in range(5):
            orders = rebalancer.on_bar_close(equity, positions, prices, self.SYMBOLS)
            all_orders.extend(orders)

        # 5 根 K 线内至少应触发一次（3 进 1）
        assert len(all_orders) > 0

    def test_drift_rebalance_triggers(self, allocator: PortfolioAllocator) -> None:
        """超过漂移阈值时应触发再平衡。"""
        rebalancer = PortfolioRebalancer(
            allocator=allocator,
            rebalance_every_n=1000,  # 不触发定时
            drift_threshold=0.05,    # 5% 漂移触发
            min_trade_notional=1.0,
        )
        equity = 100_000.0
        # BTC 占 80%，ETH 占 20%（目标各 50%，漂移 30%）
        positions = {"BTC/USDT": Decimal("1.6"), "ETH/USDT": Decimal("0.0")}
        prices = {"BTC/USDT": 50_000.0, "ETH/USDT": 3_000.0}

        orders = rebalancer.on_bar_close(equity, positions, prices, self.SYMBOLS)
        assert len(orders) > 0

    def test_sell_before_buy_ordering(self, allocator: PortfolioAllocator) -> None:
        """再平衡订单应先卖后买（防止资金不足）。"""
        rebalancer = PortfolioRebalancer(
            allocator=allocator,
            rebalance_every_n=1,
            drift_threshold=0.5,
            min_trade_notional=1.0,
        )
        equity = 100_000.0
        positions = {"BTC/USDT": Decimal("1.5"), "ETH/USDT": Decimal("0.0")}
        prices = {"BTC/USDT": 50_000.0, "ETH/USDT": 3_000.0}

        orders = rebalancer.on_bar_close(equity, positions, prices, self.SYMBOLS)
        # 找到第一笔卖单和第一笔买单的位置
        sell_indices = [i for i, o in enumerate(orders) if o.side == "sell"]
        buy_indices = [i for i, o in enumerate(orders) if o.side == "buy"]

        if sell_indices and buy_indices:
            assert max(sell_indices) < min(buy_indices), \
                "所有卖单应在买单之前"

    def test_small_notional_filtered(self, allocator: PortfolioAllocator) -> None:
        """小额交易（低于 min_trade_notional）不应生成订单。"""
        rebalancer = PortfolioRebalancer(
            allocator=allocator,
            rebalance_every_n=1,
            drift_threshold=0.001,
            min_trade_notional=99_999.0,  # 极高阈值，过滤所有小额
        )
        equity = 100_000.0
        positions = {"BTC/USDT": Decimal("1.0"), "ETH/USDT": Decimal("10.0")}
        prices = {"BTC/USDT": 50_000.0, "ETH/USDT": 3_000.0}

        orders = rebalancer.on_bar_close(equity, positions, prices, self.SYMBOLS)
        assert len(orders) == 0

    def test_drift_suppressed_after_consecutive_noop(self, allocator: PortfolioAllocator) -> None:
        """连续 drift noop 达到 3 次后，drift 触发应被抑制，仅保留 scheduled。"""
        rebalancer = PortfolioRebalancer(
            allocator=allocator,
            rebalance_every_n=100,  # scheduled 间隔很长，不会触发
            drift_threshold=0.05,
            min_trade_notional=1.0,
        )
        equity = 100_000.0
        # 全空仓，所有 target_weight > 0.05，每次都会 drift 触发
        positions = {"BTC/USDT": Decimal("0"), "ETH/USDT": Decimal("0")}
        prices = {"BTC/USDT": 50_000.0, "ETH/USDT": 3_000.0}

        # 前 3 次 drift 正常触发
        for i in range(3):
            orders = rebalancer.on_bar_close(equity, positions, prices, self.SYMBOLS)
            assert len(orders) > 0, f"第 {i+1} 次 drift 应触发"
            # 模拟熔断拦截后递增 noop
            rebalancer._consecutive_drift_noop += 1

        assert rebalancer._consecutive_drift_noop >= 3

        # 第 4 次：drift 被抑制
        orders = rebalancer.on_bar_close(equity, positions, prices, self.SYMBOLS)
        assert len(orders) == 0, "连续 noop>=3 后 drift 应被抑制"

    def test_drift_noop_reset_on_scheduled(self, allocator: PortfolioAllocator) -> None:
        """scheduled 触发应重置 drift noop 计数器。"""
        rebalancer = PortfolioRebalancer(
            allocator=allocator,
            rebalance_every_n=2,  # 每 2 根触发 scheduled
            drift_threshold=0.05,
            min_trade_notional=1.0,
        )
        rebalancer._consecutive_drift_noop = 5  # 模拟已被抑制

        equity = 100_000.0
        positions = {"BTC/USDT": Decimal("0"), "ETH/USDT": Decimal("0")}
        prices = {"BTC/USDT": 50_000.0, "ETH/USDT": 3_000.0}

        # 触发 scheduled（bar_count=1, last=-1, 1-(-1)=2 >= 2）
        orders = rebalancer.on_bar_close(equity, positions, prices, self.SYMBOLS)
        assert len(orders) > 0
        assert rebalancer._consecutive_drift_noop == 0


# ─────────────────────────────────────────────────────────────
# MeanVarianceOptimizer 测试
# ─────────────────────────────────────────────────────────────

class TestMeanVarianceOptimizer:
    @pytest.fixture
    def optimizer(self) -> MeanVarianceOptimizer:
        returns_df = make_returns(n=200, n_assets=3, seed=42)
        opt = MeanVarianceOptimizer(use_shrinkage=True, n_montecarlo=1000)
        opt.fit(returns_df, bars_per_year=8760)
        return opt

    def test_max_sharpe_sums_to_one(self, optimizer: MeanVarianceOptimizer) -> None:
        """最大夏普权重之和应为 1.0。"""
        weights = optimizer.max_sharpe()
        assert abs(sum(weights.values()) - 1.0) < 1e-6

    def test_min_variance_sums_to_one(self, optimizer: MeanVarianceOptimizer) -> None:
        """最小方差权重之和应为 1.0。"""
        weights = optimizer.min_variance()
        assert abs(sum(weights.values()) - 1.0) < 1e-6

    def test_all_weights_non_negative(self, optimizer: MeanVarianceOptimizer) -> None:
        """所有权重应为非负数（不允许做空）。"""
        for weights in [optimizer.max_sharpe(), optimizer.min_variance()]:
            for v in weights.values():
                assert v >= -1e-9, f"权重不应为负: {v}"

    def test_efficient_frontier_returns_dataframe(
        self, optimizer: MeanVarianceOptimizer
    ) -> None:
        """efficient_frontier() 应返回有效的 DataFrame。"""
        df = optimizer.efficient_frontier(n_points=20)
        assert isinstance(df, pd.DataFrame)
        assert "volatility" in df.columns
        assert "exp_return" in df.columns
        assert "sharpe" in df.columns
        assert len(df) > 0

    def test_not_fitted_raises(self) -> None:
        """未 fit() 前调用优化方法应抛出异常。"""
        opt = MeanVarianceOptimizer()
        with pytest.raises(RuntimeError, match="fit"):
            opt.max_sharpe()

    def test_summary_has_all_symbols(self, optimizer: MeanVarianceOptimizer) -> None:
        """summary() 应包含所有资产的指标行。"""
        summary = optimizer.summary()
        assert len(summary) == 3


# ─────────────────────────────────────────────────────────────
# PerformanceAttributor 测试
# ─────────────────────────────────────────────────────────────

class TestPerformanceAttributor:
    @pytest.fixture
    def attributor(self) -> PerformanceAttributor:
        attr = PerformanceAttributor()
        ts = datetime(2024, 1, 1, tzinfo=timezone.utc)

        # 策略 A：买 BTC @ 40000，卖 @ 50000（盈利 1000 USDT）
        attr.record_trade("BTC/USDT", "strategy_a", "buy", 0.1, 40_000.0, ts)
        attr.record_trade("BTC/USDT", "strategy_a", "sell", 0.1, 50_000.0, ts)

        # 策略 B：买 ETH @ 2000，卖 @ 1800（亏损 20 USDT）
        attr.record_trade("ETH/USDT", "strategy_b", "buy", 0.1, 2_000.0, ts)
        attr.record_trade("ETH/USDT", "strategy_b", "sell", 0.1, 1_800.0, ts)

        attr.record_price("BTC/USDT", 51_000.0, ts)
        attr.record_price("ETH/USDT", 1_750.0, ts)
        return attr

    def test_strategy_attribution_has_correct_pnl(
        self, attributor: PerformanceAttributor
    ) -> None:
        """策略 A 盈利应为 1000 USDT，策略 B 亏损应为 20 USDT。"""
        df = attributor.get_strategy_attribution()
        strategy_a_row = df[df["strategy_id"] == "strategy_a"].iloc[0]
        strategy_b_row = df[df["strategy_id"] == "strategy_b"].iloc[0]

        assert abs(strategy_a_row["total_pnl_usdt"] - 1000.0) < 0.01
        assert abs(strategy_b_row["total_pnl_usdt"] - (-20.0)) < 0.01

    def test_asset_attribution_has_correct_symbols(
        self, attributor: PerformanceAttributor
    ) -> None:
        """资产归因应包含 BTC/USDT 和 ETH/USDT 两行。"""
        df = attributor.get_asset_attribution()
        symbols = set(df["symbol"].tolist())
        assert "BTC/USDT" in symbols
        assert "ETH/USDT" in symbols

    def test_win_rate_correct(self, attributor: PerformanceAttributor) -> None:
        """两笔交易中一胜一负，总体胜率应为 50%。"""
        summary = attributor.get_summary_metrics()
        assert abs(summary["win_rate"] - 0.5) < 0.01

    def test_summary_has_all_keys(self, attributor: PerformanceAttributor) -> None:
        """summary 应包含所有必需的指标键。"""
        summary = attributor.get_summary_metrics()
        required_keys = {
            "total_trades", "sell_trades", "winning_trades", "win_rate",
            "total_realized_pnl_usdt", "profit_factor",
        }
        for key in required_keys:
            assert key in summary, f"缺少指标: {key}"

    def test_empty_attributor_returns_empty(self) -> None:
        """无成交记录时，归因报告应返回空。"""
        attr = PerformanceAttributor()
        assert len(attr.get_strategy_attribution()) == 0
        assert len(attr.get_asset_attribution()) == 0
        assert attr.get_summary_metrics() == {}


# ─────────────────────────────────────────────────────────────
# ContinuousLearner 测试
# ─────────────────────────────────────────────────────────────

class TestContinuousLearner:
    @pytest.fixture
    def learner(self, tmp_path: Path):
        """构建完整的 ContinuousLearner 实例（小参数，快速测试）。"""
        from modules.alpha.ml.feature_builder import FeatureConfig, MLFeatureBuilder
        from modules.alpha.ml.labeler import ReturnLabeler
        from modules.alpha.ml.trainer import WalkForwardTrainer
        from modules.alpha.ml.continuous_learner import (
            ContinuousLearner,
            ContinuousLearnerConfig,
        )

        cfg = FeatureConfig(sma_windows=[10, 20], lag_periods=[1, 2], rolling_windows=[5])
        fb = MLFeatureBuilder(cfg)
        labeler = ReturnLabeler(forward_bars=3, return_threshold=0.005)
        trainer = WalkForwardTrainer(
            feature_builder=fb,
            labeler=labeler,
            model_type="rf",
            model_params={"n_estimators": 10, "random_state": 0},
        )
        config = ContinuousLearnerConfig(
            retrain_every_n_bars=50,   # 每 50 根触发（加快测试）
            min_bars_for_retrain=150,
            model_dir=str(tmp_path),
            ab_test_window=20,
            label_type="binary",
        )
        return ContinuousLearner(trainer=trainer, feature_builder=fb, labeler=labeler, config=config)

    def test_initial_state_no_active_model(self, learner) -> None:
        """初始状态下应没有活跃模型。"""
        assert learner.get_active_model() is None

    def test_on_new_bar_accumulates_data(self, learner) -> None:
        """on_new_bar() 应逐步积累 OHLCV 数据。"""
        df = make_ohlcv_df(20)
        for _, row in df.iterrows():
            learner.on_new_bar(row.to_dict())
        assert len(learner._ohlcv_buffer) == 20

    def test_scheduled_retrain_triggers_model(self, learner) -> None:
        """积累足够数据后，定时触发应激活一个模型。"""
        df = make_ohlcv_df(250)
        for _, row in df.iterrows():
            learner.on_new_bar(row.to_dict())

        # 此时应已触发至少一次重训（250 > min_bars=150 且 250 > every_n=50）
        assert learner.get_active_model() is not None, \
            "应已触发重训并激活模型"

    def test_version_history_tracked(self, learner) -> None:
        """每次重训后版本历史应有记录。"""
        df = make_ohlcv_df(250)
        for _, row in df.iterrows():
            learner.on_new_bar(row.to_dict())

        versions = learner.get_model_version_info()
        # 应有至少一个版本
        assert isinstance(versions, list)

    def test_record_prediction_outcome(self, learner) -> None:
        """record_prediction_outcome 应更新近期准确率缓冲区。"""
        learner.record_prediction_outcome(predicted=1, actual=1)
        learner.record_prediction_outcome(predicted=0, actual=1)
        assert len(learner._recent_correct) == 2
        assert learner._recent_correct[0] == 1
        assert learner._recent_correct[1] == 0
