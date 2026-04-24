"""
modules/alpha/contracts/mm_types.py — 做市策略核心数据契约

设计说明：
- 定义做市层所有模块共享的不可变数据结构
- InventorySnapshot：当前库存状态（仓位、偏差、PnL）
- QuoteIntent：报价引擎的中间意图（reservation_price + optimal_spread + skew）
- QuoteDecision：最终双边报价决策（含价格/数量/允许标志/原因码）
- QuoteSide：报价侧枚举（BID / ASK）
- QuoteAction：报价生命周期动作枚举（POST / CANCEL / REFRESH / SKIP）
- FillRecord：单笔 maker fill 记录（来自 fill_simulator 或真实成交回报）

日志标签：[QuoteEngine] [Inventory] [FillSim]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional


# ══════════════════════════════════════════════════════════════
# 一、枚举
# ══════════════════════════════════════════════════════════════

class QuoteSide(str, Enum):
    """报价侧。"""
    BID = "bid"   # 买单（Maker 买）
    ASK = "ask"   # 卖单（Maker 卖）


class QuoteAction(str, Enum):
    """报价动作，由 QuoteLifecycle 状态机输出。"""
    POST = "post"         # 挂出新报价
    CANCEL = "cancel"     # 撤销当前报价
    REFRESH = "refresh"   # 撤旧挂新（cancel + post）
    SKIP = "skip"         # 本轮无操作（报价仍有效）
    HALT = "halt"         # 风控/库存原因暂停报价


class QuoteState(str, Enum):
    """报价生命周期状态机状态。"""
    IDLE = "idle"             # 空闲（尚未挂单）
    PENDING = "pending"       # 已提交到交易所，等待确认
    ACTIVE = "active"         # 有效在册
    PARTIALLY_FILLED = "partially_filled"  # 部分成交
    FILLED = "filled"         # 全部成交
    CANCELLED = "cancelled"   # 已撤销
    EXPIRED = "expired"       # 超时自动过期
    ERROR = "error"           # 提交/撤销出错


# ══════════════════════════════════════════════════════════════
# 二、库存快照
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class InventorySnapshot:
    """
    当前库存状态快照（由 InventoryManager 产出，策略层消费）。

    Attributes:
        symbol:              交易对
        base_qty:            当前持有的 base 资产数量（如 BTC）
        quote_value:         当前持有的 quote 资产价值（如 USDT）
        inventory_pct:       base 资产占总价值的比例 ∈ [0, 1]
        target_inventory_pct: 目标库存比例（中性点，默认 0.5）
        max_inventory_pct:   允许的最大库存偏离 ∈ [0, 1]
        skew_bps:            库存偏斜量（bps），正值 = 偏多，负值 = 偏空
                             用于调整 reservation_price
        unrealized_pnl:      未实现 PnL（quote 单位）
        realized_pnl:        已实现 PnL（quote 单位），本周期累计
        total_trades:        累计成交笔数
        last_updated_at:     快照生成时间（UTC）
    """

    symbol: str
    base_qty: float
    quote_value: float
    inventory_pct: float
    target_inventory_pct: float
    max_inventory_pct: float
    skew_bps: float
    unrealized_pnl: float
    realized_pnl: float
    total_trades: int
    last_updated_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    debug_payload: dict[str, Any] = field(default_factory=dict)

    def is_overweight(self) -> bool:
        """当前 base 持仓是否超过最大库存比例。"""
        return self.inventory_pct > self.target_inventory_pct + self.max_inventory_pct

    def is_underweight(self) -> bool:
        """当前 base 持仓是否低于最小库存比例。"""
        return self.inventory_pct < self.target_inventory_pct - self.max_inventory_pct

    def inventory_deviation(self) -> float:
        """当前库存偏离目标的距离（正 = 偏多，负 = 偏空）。"""
        return self.inventory_pct - self.target_inventory_pct

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "base_qty": self.base_qty,
            "quote_value": self.quote_value,
            "inventory_pct": self.inventory_pct,
            "target_inventory_pct": self.target_inventory_pct,
            "skew_bps": self.skew_bps,
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "total_trades": self.total_trades,
        }


# ══════════════════════════════════════════════════════════════
# 三、报价意图（QuoteEngine 中间产物）
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class QuoteIntent:
    """
    报价引擎的中间意图（不含最终价格，仅包含计算参数）。

    由 AvellanedaModel 产出，传入 QuoteEngine 进一步生成 QuoteDecision。

    Attributes:
        symbol:              交易对
        mid_price:           当前 mid price（来自 OrderBookSnapshot）
        reservation_price:   调整后的 mid（含库存 skew）
        optimal_spread_bps:  最优总点差（基点）
        sigma:               当前波动率估计（用于 spread 计算）
        gamma:               风险厌恶系数
        inventory_deviation: 库存偏离（-1 到 1）
        allow_bid:           是否允许挂买盘
        allow_ask:           是否允许挂卖盘
        reason_codes:        决策原因码列表（调试用）
        debug_payload:       原始计算中间值
    """

    symbol: str
    mid_price: float
    reservation_price: float
    optimal_spread_bps: float
    sigma: float
    gamma: float
    inventory_deviation: float
    allow_bid: bool = True
    allow_ask: bool = True
    reason_codes: list[str] = field(default_factory=list)
    debug_payload: dict[str, Any] = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════
# 四、报价决策
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class QuoteDecision:
    """
    做市策略的最终双边报价决策。

    由 QuoteEngine 产出，传入 QuoteLifecycle 执行。

    Attributes:
        symbol:              交易对
        bid_price:           买单价格（None = 本轮不挂买单）
        ask_price:           卖单价格（None = 本轮不挂卖单）
        bid_size:            买单数量（base 单位）
        ask_size:            卖单数量（base 单位）
        reservation_price:   计算用 reservation price（调试）
        optimal_spread_bps:  最优总点差（基点）
        skew_bps:            库存 skew 调整（bps）
        allow_post_bid:      是否允许提交买单
        allow_post_ask:      是否允许提交卖单
        reason_codes:        决策理由码列表
        generated_at:        决策生成时间
        debug_payload:       完整调试信息
    """

    symbol: str
    bid_price: Optional[float]
    ask_price: Optional[float]
    bid_size: Optional[float]
    ask_size: Optional[float]
    reservation_price: float
    optimal_spread_bps: float
    skew_bps: float
    allow_post_bid: bool
    allow_post_ask: bool
    reason_codes: list[str] = field(default_factory=list)
    generated_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    debug_payload: dict[str, Any] = field(default_factory=dict)

    def is_actionable(self) -> bool:
        """是否有至少一侧可以挂单。"""
        return (self.allow_post_bid and self.bid_price is not None) or \
               (self.allow_post_ask and self.ask_price is not None)

    def effective_spread_bps(self) -> Optional[float]:
        """实际挂单价差（基点），仅双边报价时有意义。"""
        if self.bid_price is not None and self.ask_price is not None and self.bid_price > 0:
            mid = (self.bid_price + self.ask_price) / 2
            return (self.ask_price - self.bid_price) / mid * 10000
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bid_price": self.bid_price,
            "ask_price": self.ask_price,
            "bid_size": self.bid_size,
            "ask_size": self.ask_size,
            "reservation_price": self.reservation_price,
            "optimal_spread_bps": self.optimal_spread_bps,
            "skew_bps": self.skew_bps,
            "allow_post_bid": self.allow_post_bid,
            "allow_post_ask": self.allow_post_ask,
            "reason_codes": self.reason_codes,
        }


# ══════════════════════════════════════════════════════════════
# 五、Fill 记录
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FillRecord:
    """
    单笔 Maker fill 记录（来自 fill_simulator 或真实成交回报）。

    用于更新 InventoryManager 状态、计算 PnL、触发 quote refresh。

    Attributes:
        symbol:         交易对
        side:           报价侧（BID = 买入成交，ASK = 卖出成交）
        fill_price:     成交价格
        fill_qty:       成交数量（base 单位）
        fee:            手续费（quote 单位）
        quote_id:       对应的报价 ID
        is_partial:     是否为部分成交
        filled_at:      成交时间（UTC）
        exchange_ts:    交易所确认时间（UTC），可能为 None
        debug_payload:  调试信息
    """

    symbol: str
    side: QuoteSide
    fill_price: float
    fill_qty: float
    fee: float
    quote_id: str
    is_partial: bool = False
    filled_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    exchange_ts: Optional[datetime] = None
    debug_payload: dict[str, Any] = field(default_factory=dict)

    def notional(self) -> float:
        return self.fill_price * self.fill_qty

    def net_notional(self) -> float:
        """扣除手续费后的净成交金额。"""
        return self.notional() - self.fee


# ══════════════════════════════════════════════════════════════
# 六、活跃报价记录
# ══════════════════════════════════════════════════════════════

@dataclass
class ActiveQuote:
    """
    单边活跃报价状态（由 QuoteLifecycle 维护）。

    Mutable dataclass（非 frozen），因为会在生命周期内更新状态。

    Attributes:
        quote_id:       唯一报价 ID
        symbol:         交易对
        side:           报价侧
        price:          挂单价格
        original_size:  原始挂单数量
        remaining_size: 剩余未成交数量
        state:          当前状态机状态
        posted_at:      挂单时间
        last_updated_at: 最后更新时间
        fills:          已产生的 fill 记录列表
        cancel_reason:  撤单原因（仅 CANCELLED/EXPIRED 状态有效）
    """

    quote_id: str
    symbol: str
    side: QuoteSide
    price: float
    original_size: float
    remaining_size: float
    state: QuoteState
    posted_at: datetime
    last_updated_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    fills: list[FillRecord] = field(default_factory=list)
    cancel_reason: str = ""

    def is_alive(self) -> bool:
        return self.state in (QuoteState.PENDING, QuoteState.ACTIVE, QuoteState.PARTIALLY_FILLED)

    def filled_pct(self) -> float:
        if self.original_size <= 0:
            return 0.0
        return 1.0 - (self.remaining_size / self.original_size)

    def age_sec(self, now: Optional[datetime] = None) -> float:
        ts = now or datetime.now(tz=timezone.utc)
        return (ts - self.posted_at).total_seconds()
