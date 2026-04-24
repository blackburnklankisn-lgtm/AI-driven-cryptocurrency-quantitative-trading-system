"""
modules/alpha/market_making/inventory_manager.py — 库存管理器

设计说明：
- 维护单个交易对的实时库存状态（base_qty、quote_value、realized_pnl）
- 不直接下单，只输出 InventorySnapshot 供上层决策
- 根据库存偏离目标，建议是否禁用 bid / ask 侧
- 基于 fill 记录更新库存和 PnL（mark-to-market + realized PnL）
- 线程安全（threading.RLock）

库存风险控制逻辑：
    - inventory_pct = base_qty × mid_price / total_value
    - inventory_deviation = inventory_pct - target_inventory_pct
    - 超出 max_inventory_pct 时建议关闭相应侧报价
    - 超出 halt_inventory_pct 时建议全停报价（HALT）

日志标签：[Inventory]
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.alpha.contracts.mm_types import (
    FillRecord,
    InventorySnapshot,
    QuoteSide,
)

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class InventoryConfig:
    """
    库存管理器配置。

    Attributes:
        target_inventory_pct:   目标库存比例（中性点），∈ [0, 1]，默认 0.5
        max_inventory_pct:      允许的最大库存偏离，超出时收紧一侧
        halt_inventory_pct:     超出此偏离时建议全停（比 max 更极端）
        max_inventory_skew_bps: 库存 skew 上限（基点），传递给 AvellanedaModel
        fee_rate:               手续费率（单边，如 0.001 = 0.1%）
        initial_base_qty:       初始 base 持仓（用于回测/paper 初始化）
        initial_quote_value:    初始 quote 价值（USDT 等）
    """

    target_inventory_pct: float = 0.5
    max_inventory_pct: float = 0.20
    halt_inventory_pct: float = 0.40
    max_inventory_skew_bps: float = 30.0
    fee_rate: float = 0.001
    initial_base_qty: float = 0.0
    initial_quote_value: float = 10000.0

    def __post_init__(self):
        if not (0.0 <= self.target_inventory_pct <= 1.0):
            raise ValueError(f"target_inventory_pct 应在 [0, 1]，当前: {self.target_inventory_pct}")
        if not (0.0 < self.max_inventory_pct <= 0.5):
            raise ValueError(f"max_inventory_pct 应在 (0, 0.5]，当前: {self.max_inventory_pct}")
        if not (self.max_inventory_pct < self.halt_inventory_pct <= 1.0):
            raise ValueError("halt_inventory_pct 必须 > max_inventory_pct")


# ══════════════════════════════════════════════════════════════
# 二、InventoryManager 主体
# ══════════════════════════════════════════════════════════════

class InventoryManager:
    """
    单交易对库存管理器。

    负责：
    1. 跟踪 base_qty 和 quote_value（受 fill 更新）
    2. 计算 inventory_pct 和 skew_bps（输入给 AvellanedaModel）
    3. 判断是否建议禁用 bid / ask 侧（超出 max 库存）
    4. 计算 realized PnL（FIFO 成本法）
    5. 输出 InventorySnapshot（只读快照，供上层消费）

    线程安全：所有写操作持 RLock
    """

    def __init__(
        self,
        symbol: str,
        config: Optional[InventoryConfig] = None,
    ) -> None:
        self.symbol = symbol
        self.config = config or InventoryConfig()
        self._lock = threading.RLock()

        # 运行时状态
        self._base_qty: float = self.config.initial_base_qty
        self._quote_value: float = self.config.initial_quote_value
        self._realized_pnl: float = 0.0
        self._total_trades: int = 0
        self._last_mid: float = 0.0   # 最新 mid price（用于 mark-to-market）

        # FIFO 成本队列 [(qty, cost_price), ...]
        self._cost_queue: list[tuple[float, float]] = []

        log.info(
            "[Inventory] 初始化: symbol={} target_pct={} max_pct={} "
            "initial_base={} initial_quote={}",
            symbol,
            self.config.target_inventory_pct,
            self.config.max_inventory_pct,
            self.config.initial_base_qty,
            self.config.initial_quote_value,
        )

    # ──────────────────────────────────────────────────────────
    # 状态更新接口
    # ──────────────────────────────────────────────────────────

    def on_fill(self, fill: FillRecord) -> None:
        """
        处理 Maker fill 记录，更新库存状态。

        BID fill → 买入：base_qty += fill_qty, quote_value -= notional + fee
        ASK fill → 卖出：base_qty -= fill_qty, quote_value += notional - fee
        """
        with self._lock:
            self._total_trades += 1

            if fill.side == QuoteSide.BID:
                # 买入：持仓增加，消耗 quote
                self._base_qty += fill.fill_qty
                cost = fill.fill_price * fill.fill_qty + fill.fee
                self._quote_value -= cost
                # FIFO 入队
                self._cost_queue.append((fill.fill_qty, fill.fill_price))
                log.debug(
                    "[Inventory] BID fill: symbol={} qty={:.6f} price={:.4f} "
                    "fee={:.4f} base_qty_after={:.6f}",
                    self.symbol, fill.fill_qty, fill.fill_price,
                    fill.fee, self._base_qty,
                )

            elif fill.side == QuoteSide.ASK:
                # 卖出：持仓减少，获得 quote
                realized = self._compute_realized_pnl(fill.fill_qty, fill.fill_price, fill.fee)
                self._base_qty -= fill.fill_qty
                revenue = fill.fill_price * fill.fill_qty - fill.fee
                self._quote_value += revenue
                self._realized_pnl += realized
                log.debug(
                    "[Inventory] ASK fill: symbol={} qty={:.6f} price={:.4f} "
                    "fee={:.4f} realized_pnl={:.4f} base_qty_after={:.6f}",
                    self.symbol, fill.fill_qty, fill.fill_price,
                    fill.fee, realized, self._base_qty,
                )

            # 防止浮点误差累积
            self._base_qty = max(0.0, self._base_qty)
            self._quote_value = max(0.0, self._quote_value)

    def update_mid(self, mid_price: float) -> None:
        """更新最新 mid price（用于 mark-to-market 计算）。"""
        with self._lock:
            self._last_mid = mid_price

    # ──────────────────────────────────────────────────────────
    # 快照输出接口
    # ──────────────────────────────────────────────────────────

    def snapshot(self, mid_price: Optional[float] = None) -> InventorySnapshot:
        """
        输出当前库存状态快照。

        Args:
            mid_price: 当前 mid price（None = 使用上次 update_mid 的值）

        Returns:
            InventorySnapshot（只读）
        """
        with self._lock:
            mid = mid_price if mid_price is not None else self._last_mid
            if mid is None or mid <= 0:
                mid = 0.0

            base_value = self._base_qty * mid
            total_value = base_value + self._quote_value

            if total_value > 0:
                inventory_pct = base_value / total_value
            else:
                inventory_pct = 0.0

            # 未实现 PnL（mark-to-market）
            unrealized_pnl = self._compute_unrealized_pnl(mid)

            # 库存偏离 → skew_bps
            deviation = inventory_pct - self.config.target_inventory_pct
            skew_bps = deviation * self.config.max_inventory_skew_bps / max(
                self.config.max_inventory_pct, 0.001
            )
            skew_bps = max(-self.config.max_inventory_skew_bps, min(self.config.max_inventory_skew_bps, skew_bps))

            debug = {
                "base_value": base_value,
                "total_value": total_value,
                "mid": mid,
                "deviation": deviation,
            }

            snap = InventorySnapshot(
                symbol=self.symbol,
                base_qty=self._base_qty,
                quote_value=self._quote_value,
                inventory_pct=inventory_pct,
                target_inventory_pct=self.config.target_inventory_pct,
                max_inventory_pct=self.config.max_inventory_pct,
                skew_bps=skew_bps,
                unrealized_pnl=unrealized_pnl,
                realized_pnl=self._realized_pnl,
                total_trades=self._total_trades,
                debug_payload=debug,
            )

        log.debug(
            "[Inventory] snapshot: symbol={} inventory_pct={:.4f} "
            "skew_bps={:.2f} base={:.6f} quote={:.2f} unrealized_pnl={:.4f}",
            self.symbol, inventory_pct, skew_bps,
            self._base_qty, self._quote_value, unrealized_pnl,
        )

        return snap

    def suggest_quote_sides(self, snap: Optional[InventorySnapshot] = None) -> dict[str, bool]:
        """
        建议是否允许报价某一侧。

        Returns:
            {"allow_bid": bool, "allow_ask": bool, "halt": bool}
        """
        s = snap or self.snapshot()
        dev = abs(s.inventory_deviation())

        halt = dev >= self.config.halt_inventory_pct
        if halt:
            log.warning(
                "[Inventory] 库存极端偏离，建议全停报价: symbol={} deviation={:.4f}",
                self.symbol, s.inventory_deviation(),
            )
            return {"allow_bid": False, "allow_ask": False, "halt": True}

        over_max = dev >= self.config.max_inventory_pct
        if over_max:
            if s.inventory_deviation() > 0:
                # 偏多 → 禁止继续买
                log.debug(
                    "[Inventory] 库存偏多，建议禁 BID: symbol={} deviation={:.4f}",
                    self.symbol, s.inventory_deviation(),
                )
                return {"allow_bid": False, "allow_ask": True, "halt": False}
            else:
                # 偏空 → 禁止继续卖
                log.debug(
                    "[Inventory] 库存偏空，建议禁 ASK: symbol={} deviation={:.4f}",
                    self.symbol, s.inventory_deviation(),
                )
                return {"allow_bid": True, "allow_ask": False, "halt": False}

        return {"allow_bid": True, "allow_ask": True, "halt": False}

    def reset(self) -> None:
        """重置库存状态（用于新策略周期或 paper 重置）。"""
        with self._lock:
            self._base_qty = self.config.initial_base_qty
            self._quote_value = self.config.initial_quote_value
            self._realized_pnl = 0.0
            self._total_trades = 0
            self._last_mid = 0.0
            self._cost_queue = []
        log.info("[Inventory] 库存已重置: symbol={}", self.symbol)

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "symbol": self.symbol,
                "base_qty": self._base_qty,
                "quote_value": self._quote_value,
                "realized_pnl": self._realized_pnl,
                "total_trades": self._total_trades,
                "last_mid": self._last_mid,
                "cost_queue_depth": len(self._cost_queue),
            }

    # ──────────────────────────────────────────────────────────
    # 内部计算
    # ──────────────────────────────────────────────────────────

    def _compute_realized_pnl(
        self,
        sell_qty: float,
        sell_price: float,
        fee: float,
    ) -> float:
        """
        基于 FIFO 成本法计算卖出的 realized PnL。

        Args:
            sell_qty:   卖出数量
            sell_price: 卖出价格
            fee:        手续费（quote）

        Returns:
            realized PnL（quote 单位）
        """
        if not self._cost_queue:
            # 没有买入记录（可能是初始多头），以 sell_price 为成本（保守计 0 盈亏）
            return 0.0

        remaining = sell_qty
        cost_total = 0.0

        while remaining > 1e-10 and self._cost_queue:
            lot_qty, lot_price = self._cost_queue[0]
            consume = min(remaining, lot_qty)
            cost_total += consume * lot_price
            remaining -= consume

            if consume >= lot_qty - 1e-10:
                self._cost_queue.pop(0)
            else:
                self._cost_queue[0] = (lot_qty - consume, lot_price)

        revenue = sell_price * sell_qty - fee
        realized = revenue - cost_total
        return realized

    def _compute_unrealized_pnl(self, mid: float) -> float:
        """基于 FIFO 队列计算未实现 PnL（mark-to-market）。"""
        if not self._cost_queue or mid <= 0:
            return 0.0

        total_cost = sum(qty * price for qty, price in self._cost_queue)
        total_qty = sum(qty for qty, _ in self._cost_queue)
        market_value = total_qty * mid
        return market_value - total_cost
