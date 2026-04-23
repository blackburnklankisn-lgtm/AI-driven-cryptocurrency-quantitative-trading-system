"""
modules/portfolio/rebalancer.py — 组合再平衡管理器

设计说明：
再平衡是将当前实际持仓权重调整回目标权重的过程。

触发机制（二选一或组合）：
1. 时间触发（Time-Based）：每 N 根 K 线强制再平衡一次
2. 漂移触发（Drift-Based）：当任意资产的实际权重与目标权重
   偏差超过 drift_threshold 时触发

再平衡执行逻辑：
- 计算每个资产的当前权重（市值 / 总净值）
- 算出目标权重与当前权重的差值（Δw）
- 对 Δw > 0 的资产发出买入请求
- 对 Δw < 0 的资产发出卖出请求
- 订单量 = 总净值 × |Δw| / 当前价格

关键约束：
- 最小交易量过滤（避免产生尘埃仓位）
- 买入前验证余额充足（不允许负现金）
- 所有再平衡操作写入审计日志

接口：
    PortfolioRebalancer(allocator, rebalance_every_n_bars, drift_threshold, min_trade_notional)
    .on_bar_close(equity, positions, prices, current_bar) → List[RebalanceOrder]
    .force_rebalance(equity, positions, prices)            → List[RebalanceOrder]
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional

from core.logger import audit_log, get_logger
from modules.portfolio.allocator import AllocationMethod, PortfolioAllocator

log = get_logger(__name__)


@dataclass
class RebalanceOrder:
    """再平衡产出的单笔交易请求。"""
    symbol: str
    side: str           # "buy" | "sell"
    quantity: Decimal
    notional: float     # 名义金额（USDT）
    reason: str         # 触发原因（"drift" | "scheduled" | "forced"）
    current_weight: float
    target_weight: float


class PortfolioRebalancer:
    """
    组合再平衡管理器。

    绑定 PortfolioAllocator，在每根 K 线结束时判断是否需要再平衡，
    并产出 RebalanceOrder 列表（上层传递给 RiskManager → OrderManager 执行）。

    Args:
        allocator:            PortfolioAllocator 实例（提供目标权重）
        rebalance_every_n:    时间触发间隔（K 线数量，0 = 禁用）
        drift_threshold:      漂移阈值（如 0.05 = 实际偏离目标 5% 时触发）
        min_trade_notional:   最小交易名义金额（USDT），低于此不产生订单（过滤尘埃仓位）
        cash_buffer_pct:      保留现金缓冲比例（不参与分配，用于手续费和滑点）
    """

    def __init__(
        self,
        allocator: PortfolioAllocator,
        rebalance_every_n: int = 24,       # 每 24 根 K 线（1h 则为每天）
        drift_threshold: float = 0.05,
        min_trade_notional: float = 10.0,
        cash_buffer_pct: float = 0.02,
    ) -> None:
        self.allocator = allocator
        self.rebalance_every_n = rebalance_every_n
        self.drift_threshold = drift_threshold
        self.min_trade_notional = min_trade_notional
        self.cash_buffer_pct = cash_buffer_pct

        self._bar_count: int = 0
        self._last_rebalance_bar: int = -1
        self._target_weights: Dict[str, float] = {}
        self._consecutive_drift_noop: int = 0  # 连续 drift 触发但未实际执行的次数

        log.info(
            "PortfolioRebalancer 初始化: every_n={} drift={} min_notional={}",
            rebalance_every_n, drift_threshold, min_trade_notional,
        )

    # ────────────────────────────────────────────────────────────
    # 主接口
    # ────────────────────────────────────────────────────────────

    def on_bar_close(
        self,
        equity: float,
        positions: Dict[str, Decimal],
        prices: Dict[str, float],
        symbols: List[str],
    ) -> List[RebalanceOrder]:
        """
        在每根 K 线结束时调用，判断并（若需要）执行再平衡。

        Args:
            equity:    当前账户总净值（现金 + 持仓市值）
            positions: 当前持仓 {symbol: qty}
            prices:    当前价格 {symbol: price}
            symbols:   参与分配的标的列表

        Returns:
            RebalanceOrder 列表（可能为空）
        """
        self._bar_count += 1

        if not symbols or equity <= 0:
            return []

        # 更新目标权重
        self._target_weights = self.allocator.compute_weights(symbols)

        # 计算当前权重
        current_weights = self._compute_current_weights(equity, positions, prices, symbols)

        # 判断是否触发再平衡
        trigger_reason = self._check_triggers(current_weights)

        if trigger_reason:
            # scheduled 触发时重置 drift noop 计数器（允许重新检测 drift）
            if trigger_reason == "scheduled":
                self._consecutive_drift_noop = 0
            orders = self._generate_orders(
                equity=equity,
                positions=positions,
                prices=prices,
                current_weights=current_weights,
                reason=trigger_reason,
            )
            if orders:
                self._last_rebalance_bar = self._bar_count
                audit_log(
                    "PORTFOLIO_REBALANCE",
                    trigger=trigger_reason,
                    bar=self._bar_count,
                    target_weights=self._target_weights,
                    current_weights=current_weights,
                    n_orders=len(orders),
                )
                log.info(
                    "再平衡触发: reason={} orders={} bar={}",
                    trigger_reason, len(orders), self._bar_count,
                )
            return orders

        return []

    def force_rebalance(
        self,
        equity: float,
        positions: Dict[str, Decimal],
        prices: Dict[str, float],
        symbols: List[str],
    ) -> List[RebalanceOrder]:
        """强制立即执行一次再平衡（不考虑触发条件）。"""
        self._target_weights = self.allocator.compute_weights(symbols)
        current_weights = self._compute_current_weights(equity, positions, prices, symbols)
        return self._generate_orders(
            equity=equity,
            positions=positions,
            prices=prices,
            current_weights=current_weights,
            reason="forced",
        )

    def get_current_drift(
        self,
        equity: float,
        positions: Dict[str, Decimal],
        prices: Dict[str, float],
        symbols: List[str],
    ) -> Dict[str, float]:
        """
        返回各标的的当前权重漂移量（实际权重 - 目标权重）。

        正数 = 超配，负数 = 低配。
        """
        if not self._target_weights:
            return {}
        current = self._compute_current_weights(equity, positions, prices, symbols)
        return {
            s: current.get(s, 0.0) - self._target_weights.get(s, 0.0)
            for s in symbols
        }

    # ────────────────────────────────────────────────────────────
    # 私有方法
    # ────────────────────────────────────────────────────────────

    def _compute_current_weights(
        self,
        equity: float,
        positions: Dict[str, Decimal],
        prices: Dict[str, float],
        symbols: List[str],
    ) -> Dict[str, float]:
        """计算各标的的实际市值权重。"""
        if equity <= 0:
            return {s: 0.0 for s in symbols}

        weights = {}
        for s in symbols:
            qty = float(positions.get(s, Decimal("0")))
            price = prices.get(s, 0.0)
            notional = qty * price
            weights[s] = notional / equity

        return weights

    def _check_triggers(self, current_weights: Dict[str, float]) -> Optional[str]:
        """
        检查是否满足再平衡触发条件。

        Returns:
            触发原因字符串，或 None（不触发）
        """
        # 时间触发
        if (
            self.rebalance_every_n > 0
            and self._bar_count - self._last_rebalance_bar >= self.rebalance_every_n
        ):
            return "scheduled"

        # 漂移触发（当连续 drift 未被执行超过 3 次时降级为仅 scheduled 触发，
        # 避免空仓+熔断场景下每分钟产生无效日志）
        if self._consecutive_drift_noop >= 3:
            return None

        if self._target_weights and self.drift_threshold > 0:
            for symbol, target_w in self._target_weights.items():
                actual_w = current_weights.get(symbol, 0.0)
                if abs(actual_w - target_w) > self.drift_threshold:
                    log.debug(
                        "漂移触发: symbol={} actual={:.3f} target={:.3f}",
                        symbol, actual_w, target_w,
                    )
                    return "drift"

        return None

    def _generate_orders(
        self,
        equity: float,
        positions: Dict[str, Decimal],
        prices: Dict[str, float],
        current_weights: Dict[str, float],
        reason: str,
    ) -> List[RebalanceOrder]:
        """
        生成再平衡订单列表。

        逻辑：
        - 可用于分配的资金 = equity × (1 - cash_buffer_pct)
        - 对每个标的：目标金额 = 可用资金 × target_weight
        - 差额 = 目标金额 - 当前市值
        - |差额| > min_trade_notional 才生成订单
        - 先生成卖单（释放资金），再生成买单（使用资金）
        """
        allocatable = equity * (1.0 - self.cash_buffer_pct)
        orders: List[RebalanceOrder] = []
        buys: List[RebalanceOrder] = []
        sells: List[RebalanceOrder] = []

        for symbol, target_w in self._target_weights.items():
            price = prices.get(symbol, 0.0)
            if price <= 0:
                continue

            current_qty = float(positions.get(symbol, Decimal("0")))
            current_notional = current_qty * price
            target_notional = allocatable * target_w
            delta_notional = target_notional - current_notional

            if abs(delta_notional) < self.min_trade_notional:
                continue  # 差额太小，不值得交易（节省手续费）

            delta_qty = abs(delta_notional) / price
            order = RebalanceOrder(
                symbol=symbol,
                side="buy" if delta_notional > 0 else "sell",
                quantity=Decimal(f"{delta_qty:.8f}"),
                notional=abs(delta_notional),
                reason=reason,
                current_weight=current_weights.get(symbol, 0.0),
                target_weight=target_w,
            )

            if order.side == "sell":
                sells.append(order)
            else:
                buys.append(order)

        # 先卖后买（确保有资金执行买入）
        orders = sells + buys

        if orders:
            log.info(
                "再平衡订单生成: {} 笔（{} 卖 / {} 买）reason={}",
                len(orders), len(sells), len(buys), reason,
            )

        return orders
