"""
modules/alpha/market_making/quote_engine.py — 双边报价生成器

设计说明：
- 消费 QuoteIntent（来自 AvellanedaModel）+ InventorySnapshot，产出 QuoteDecision
- 核心职责：将 reservation_price 和 optimal_spread_bps 转换为具体的 bid/ask 价格
- 结合库存 skew 调整单边价格：
    * bid = reservation_price - half_spread_bps * bps_factor * (1 + skew_adjust_bid)
    * ask = reservation_price + half_spread_bps * bps_factor * (1 + skew_adjust_ask)
- 根据 InventorySnapshot 的 suggest_quote_sides 决定是否允许挂单
- 做市数量根据配置的 base_order_size 和库存偏离度动态调整

设计边界：
    - QuoteEngine 不知道当前已有哪些未成交报价（那是 QuoteLifecycle 的职责）
    - QuoteEngine 不直接下单（那是 MarketMakingStrategy → Gateway 的职责）
    - QuoteEngine 只做"输入 → 决策"的纯函数转换

日志标签：[QuoteEngine]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.alpha.contracts.mm_types import (
    InventorySnapshot,
    QuoteDecision,
    QuoteIntent,
)

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class QuoteEngineConfig:
    """
    QuoteEngine 配置。

    Attributes:
        base_order_size:        基础每笔报价数量（base 单位，如 BTC）
        min_order_size:         最小报价数量（防止过小的单）
        size_scale_with_inventory: 是否根据库存偏离调整报价数量
                                True = 偏多时 ask 更大、bid 更小（加速去库存）
        min_spread_bps:         报价最小点差下限（防止买卖价交叉）
        price_precision:        价格精度（小数位数），-1 = 不截断
        size_precision:         数量精度（小数位数），-1 = 不截断
        spread_buffer_bps:      在 spread 两端额外加入的安全 buffer（基点）
                                防止因价格精度截断导致 bid > ask
    """

    base_order_size: float = 0.01
    min_order_size: float = 0.001
    size_scale_with_inventory: bool = True
    min_spread_bps: float = 0.5
    price_precision: int = 2
    size_precision: int = 6
    spread_buffer_bps: float = 0.1

    def __post_init__(self):
        if self.base_order_size <= 0:
            raise ValueError(f"base_order_size 必须 > 0，当前: {self.base_order_size}")
        if self.min_order_size <= 0 or self.min_order_size > self.base_order_size:
            raise ValueError("min_order_size 必须在 (0, base_order_size]")


# ══════════════════════════════════════════════════════════════
# 二、QuoteEngine 主体
# ══════════════════════════════════════════════════════════════

class QuoteEngine:
    """
    双边报价生成器。

    无状态，每次 generate() 调用独立计算。

    Args:
        config: QuoteEngineConfig

    接口：
        generate(intent, inventory_snap) → QuoteDecision
    """

    def __init__(self, config: Optional[QuoteEngineConfig] = None) -> None:
        self.config = config or QuoteEngineConfig()
        log.info(
            "[QuoteEngine] 初始化: base_size={} min_spread={}bps "
            "price_precision={} size_precision={}",
            self.config.base_order_size,
            self.config.min_spread_bps,
            self.config.price_precision,
            self.config.size_precision,
        )

    def generate(
        self,
        intent: QuoteIntent,
        inventory_snap: InventorySnapshot,
    ) -> QuoteDecision:
        """
        基于 QuoteIntent 和 InventorySnapshot 生成最终双边报价决策。

        流程：
        1. 从 intent 读取 reservation_price 和 optimal_spread_bps
        2. 结合 inventory skew_bps 调整两侧半价差
        3. 计算 bid/ask 原始价格
        4. 应用价格精度截断
        5. 检查库存允许侧
        6. 根据库存偏离调整报价数量
        7. 输出 QuoteDecision

        Args:
            intent:          AvellanedaModel 产出的报价意图
            inventory_snap:  InventoryManager 产出的库存快照

        Returns:
            QuoteDecision（含完整调试信息）
        """
        reason_codes: list[str] = list(intent.reason_codes)

        # ── 1. 计算半价差（基点 → 价格）
        r = intent.reservation_price
        bps_factor = r / 10000.0  # 1 bps 对应的价格单位

        # 保证最小 spread
        spread_bps = max(
            self.config.min_spread_bps + self.config.spread_buffer_bps,
            intent.optimal_spread_bps,
        )
        half_spread_price = spread_bps / 2.0 * bps_factor

        # ── 2. 库存 skew 调整（skew_bps → 价格调整量）
        # 正 skew（偏多）→ bid 降低 + ask 降低（希望多卖）
        # 负 skew（偏空）→ bid 抬高 + ask 抬高（希望多买）
        skew_adj = inventory_snap.skew_bps * bps_factor

        bid_raw = r - half_spread_price - skew_adj / 2.0
        ask_raw = r + half_spread_price - skew_adj / 2.0

        # ── 3. 价格精度截断
        bid_price = self._round_price(bid_raw)
        ask_price = self._round_price(ask_raw)

        # ── 4. 买卖价合理性检查（bid < ask）
        if bid_price >= ask_price:
            # 价格精度截断导致交叉，强制拉开
            reason_codes.append("PRICE_PRECISION_CROSS_FIX")
            mid_p = self._round_price(r)
            min_tick = 10 ** (-self.config.price_precision) if self.config.price_precision >= 0 else 0.01
            bid_price = mid_p - min_tick
            ask_price = mid_p + min_tick
            log.debug(
                "[QuoteEngine] 价格截断后交叉，强制拉开: symbol={} bid={} ask={}",
                intent.symbol, bid_price, ask_price,
            )

        # ── 5. 库存允许侧
        allow_bid = intent.allow_bid
        allow_ask = intent.allow_ask

        # 叠加库存管理器的建议
        dev = inventory_snap.inventory_deviation()
        if dev >= inventory_snap.max_inventory_pct:
            allow_bid = False
            reason_codes.append("INVENTORY_OVERWEIGHT_BID_DISABLED")
        elif dev <= -inventory_snap.max_inventory_pct:
            allow_ask = False
            reason_codes.append("INVENTORY_OVERWEIGHT_ASK_DISABLED")

        # ── 6. 报价数量（根据库存偏离调整）
        bid_size, ask_size = self._compute_sizes(inventory_snap, allow_bid, allow_ask)

        # ── 7. 过滤最小订单
        if bid_size < self.config.min_order_size:
            allow_bid = False
            bid_size = None
            reason_codes.append("BID_SIZE_TOO_SMALL")

        if ask_size < self.config.min_order_size:
            allow_ask = False
            ask_size = None
            reason_codes.append("ASK_SIZE_TOO_SMALL")

        final_bid_price = bid_price if allow_bid else None
        final_ask_price = ask_price if allow_ask else None
        final_bid_size = bid_size if allow_bid else None
        final_ask_size = ask_size if allow_ask else None

        effective_spread = None
        if final_bid_price and final_ask_price:
            mid_e = (final_bid_price + final_ask_price) / 2
            effective_spread = (final_ask_price - final_bid_price) / mid_e * 10000

        log.debug(
            "[QuoteEngine] 报价生成: symbol={} bid={} ask={} bid_size={} ask_size={} "
            "r_price={:.4f} spread={}bps effective={}bps skew={:.2f}bps reasons={}",
            intent.symbol,
            final_bid_price, final_ask_price,
            final_bid_size, final_ask_size,
            intent.reservation_price,
            round(spread_bps, 2),
            round(effective_spread, 2) if effective_spread else "N/A",
            inventory_snap.skew_bps,
            reason_codes,
        )

        return QuoteDecision(
            symbol=intent.symbol,
            bid_price=final_bid_price,
            ask_price=final_ask_price,
            bid_size=final_bid_size,
            ask_size=final_ask_size,
            reservation_price=intent.reservation_price,
            optimal_spread_bps=intent.optimal_spread_bps,
            skew_bps=inventory_snap.skew_bps,
            allow_post_bid=allow_bid,
            allow_post_ask=allow_ask,
            reason_codes=reason_codes,
            debug_payload={
                "bid_raw": bid_raw,
                "ask_raw": ask_raw,
                "spread_bps_used": spread_bps,
                "half_spread_price": half_spread_price,
                "skew_adj": skew_adj,
                "inventory_deviation": dev,
                "effective_spread_bps": effective_spread,
            },
        )

    # ──────────────────────────────────────────────────────────
    # 内部工具
    # ──────────────────────────────────────────────────────────

    def _round_price(self, price: float) -> float:
        if self.config.price_precision < 0:
            return price
        return round(price, self.config.price_precision)

    def _round_size(self, size: float) -> float:
        if self.config.size_precision < 0:
            return size
        return round(size, self.config.size_precision)

    def _compute_sizes(
        self,
        snap: InventorySnapshot,
        allow_bid: bool,
        allow_ask: bool,
    ) -> tuple[float, float]:
        """
        根据库存偏离动态调整报价数量。

        偏多（inventory_deviation > 0）→ ask_size 增大，bid_size 减小（加速减仓）
        偏空（inventory_deviation < 0）→ bid_size 增大，ask_size 减小（加速补仓）
        """
        dev = snap.inventory_deviation()
        base_size = self.config.base_order_size

        if not self.config.size_scale_with_inventory:
            bid_size = base_size if allow_bid else 0.0
            ask_size = base_size if allow_ask else 0.0
            return self._round_size(bid_size), self._round_size(ask_size)

        # deviation ∈ [-max, max]，归一化到 [-1, 1]
        max_dev = max(snap.max_inventory_pct, 0.001)
        dev_norm = max(-1.0, min(1.0, dev / max_dev))

        # 偏多：减少 bid，增加 ask
        bid_scale = max(0.1, 1.0 - dev_norm * 0.5)   # 偏多时最低减到 50%
        ask_scale = max(0.1, 1.0 + dev_norm * 0.5)   # 偏多时最高增到 150%

        bid_size = base_size * bid_scale if allow_bid else 0.0
        ask_size = base_size * ask_scale if allow_ask else 0.0

        return self._round_size(bid_size), self._round_size(ask_size)
