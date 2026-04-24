"""
modules/alpha/market_making/fill_simulator.py — Maker Fill 仿真器

设计说明：
- 独立于 live 执行，供 replay / backtest / paper 共用
- 根据订单簿快照判断报价是否被"穿越"（即市场价格到达 maker 报价）
- 规则：
    * BID 报价：当 best_ask <= bid_price 时视为成交（被市场卖出方穿越）
    * ASK 报价：当 best_bid >= ask_price 时视为成交（被市场买入方穿越）
- 支持部分成交（partial fill）：使用 fill_pct 随机或基于成交量比例模拟
- 输出 FillRecord（与真实成交回报格式相同）

设计边界：
    - FillSimulator 不维护任何报价状态（那是 QuoteLifecycle 的职责）
    - FillSimulator 只接收"报价 + 快照"，输出"fill 或 None"
    - 所有随机数行为通过 seed 控制，确保 replay 可复现

日志标签：[FillSim]
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.alpha.contracts.mm_types import (
    ActiveQuote,
    FillRecord,
    QuoteSide,
    QuoteState,
)
from modules.data.realtime.orderbook_types import OrderBookSnapshot

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class FillSimulatorConfig:
    """
    Fill 仿真器配置。

    Attributes:
        fee_rate:           单边手续费率（如 0.001 = 0.1%）
        partial_fill_prob:  产生部分成交的概率 ∈ [0, 1]
        partial_fill_min:   部分成交时最小成交比例 ∈ (0, 1)
        partial_fill_max:   部分成交时最大成交比例 ∈ (0, 1)
        slippage_pct:       报价到实际成交价的滑点（相对比例），默认 0（maker 无滑点）
        seed:               随机种子（保证 replay 可复现），None = 不固定
    """

    fee_rate: float = 0.001
    partial_fill_prob: float = 0.2
    partial_fill_min: float = 0.2
    partial_fill_max: float = 0.8
    slippage_pct: float = 0.0
    seed: Optional[int] = 42


# ══════════════════════════════════════════════════════════════
# 二、FillSimulator 主体
# ══════════════════════════════════════════════════════════════

class FillSimulator:
    """
    Maker Fill 仿真器。

    无持久状态，每次 check_fill() 独立判断。
    可复现（通过 seed 控制随机数）。

    Args:
        config: FillSimulatorConfig
    """

    def __init__(self, config: Optional[FillSimulatorConfig] = None) -> None:
        self.config = config or FillSimulatorConfig()
        self._rng = random.Random(self.config.seed)
        log.info(
            "[FillSim] 初始化: fee_rate={} partial_fill_prob={} seed={}",
            self.config.fee_rate,
            self.config.partial_fill_prob,
            self.config.seed,
        )

    def check_fill(
        self,
        quote: ActiveQuote,
        snapshot: OrderBookSnapshot,
    ) -> Optional[FillRecord]:
        """
        检查报价在当前订单簿快照下是否成交。

        填充规则：
        - BID: 若 best_ask <= quote.price → 视为市场价格下穿买价，成交
        - ASK: 若 best_bid >= quote.price → 视为市场价格上穿卖价，成交

        Args:
            quote:    当前活跃报价
            snapshot: 当前订单簿快照

        Returns:
            FillRecord（成交时），None（不成交时）
        """
        if not quote.is_alive():
            return None

        if not snapshot.is_healthy():
            log.debug(
                "[FillSim] 快照不健康，跳过 fill 检查: quote_id={} symbol={}",
                quote.quote_id, quote.symbol,
            )
            return None

        triggered = False
        fill_price = quote.price

        if quote.side == QuoteSide.BID:
            # 买单：市场卖出方穿越买价（best_ask 下穿 bid）
            triggered = snapshot.best_ask <= quote.price
            if triggered:
                # 以 best_ask 成交（或报价 price，取较优者）
                fill_price = min(quote.price, snapshot.best_ask)
        elif quote.side == QuoteSide.ASK:
            # 卖单：市场买入方穿越卖价（best_bid 上穿 ask）
            triggered = snapshot.best_bid >= quote.price
            if triggered:
                # 以 best_bid 成交（或报价 price，取较优者）
                fill_price = max(quote.price, snapshot.best_bid)

        if not triggered:
            return None

        # 应用滑点（maker 通常无滑点，但模拟器支持参数化）
        if self.config.slippage_pct > 0:
            slippage = fill_price * self.config.slippage_pct
            if quote.side == QuoteSide.BID:
                fill_price += slippage
            else:
                fill_price -= slippage

        # 决定成交数量（全量 or 部分）
        fill_qty = quote.remaining_size
        is_partial = False

        if (
            self._rng.random() < self.config.partial_fill_prob
            and quote.remaining_size > 0
        ):
            pct = self._rng.uniform(
                self.config.partial_fill_min,
                self.config.partial_fill_max,
            )
            fill_qty = max(fill_qty * pct, 1e-8)
            is_partial = (fill_qty < quote.remaining_size)

        fill_qty = min(fill_qty, quote.remaining_size)
        fee = fill_price * fill_qty * self.config.fee_rate
        now = datetime.now(tz=timezone.utc)

        fill = FillRecord(
            symbol=quote.symbol,
            side=quote.side,
            fill_price=fill_price,
            fill_qty=fill_qty,
            fee=fee,
            quote_id=quote.quote_id,
            is_partial=is_partial,
            filled_at=now,
            exchange_ts=snapshot.exchange_ts,
            debug_payload={
                "snapshot_best_bid": snapshot.best_bid,
                "snapshot_best_ask": snapshot.best_ask,
                "quote_price": quote.price,
                "quote_remaining": quote.remaining_size,
                "triggered": True,
                "slippage_pct": self.config.slippage_pct,
            },
        )

        log.debug(
            "[FillSim] Fill 触发: quote_id={} symbol={} side={} fill_price={:.4f} "
            "fill_qty={:.6f} is_partial={} fee={:.6f}",
            quote.quote_id, quote.symbol, quote.side.value,
            fill_price, fill_qty, is_partial, fee,
        )

        return fill

    def batch_check(
        self,
        quotes: list[ActiveQuote],
        snapshot: OrderBookSnapshot,
    ) -> list[FillRecord]:
        """
        批量检查多个报价的 fill 情况。

        Args:
            quotes:   活跃报价列表
            snapshot: 当前订单簿快照

        Returns:
            触发成交的 FillRecord 列表（可能为空）
        """
        fills = []
        for quote in quotes:
            fill = self.check_fill(quote, snapshot)
            if fill is not None:
                fills.append(fill)
        return fills

    def reset_rng(self, seed: Optional[int] = None) -> None:
        """重置随机数生成器（用于 replay 重放时恢复种子）。"""
        new_seed = seed if seed is not None else self.config.seed
        self._rng = random.Random(new_seed)
        log.debug("[FillSim] RNG 已重置: seed={}", new_seed)
