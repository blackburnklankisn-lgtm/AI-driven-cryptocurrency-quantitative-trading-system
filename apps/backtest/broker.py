"""
apps/backtest/broker.py — 模拟券商（SimulatedBroker）

设计说明：
- 负责在回测中模拟真实的订单撮合过程
- 维护虚拟账户：可用余额、持仓、成交历史
- 在 KlineEvent 到来时，对所有挂单进行撮合判断
- 手续费、滑点显式建模，不忽略不估高

撮合规则（保守估计）：
- 市价单：在下一根 K 线的 open 价格成交（避免当根 K 线未来函数）
- 限价单：K 线 high/low 穿越成交价时触发撮合，成交价 = 限价
- 滑点：市价单在 open 基础上加/减滑点比例（buy 加价，sell 减价）
- 手续费：按成交名义金额的固定比例扣除（默认 0.1%，Maker）

接口：
    SimulatedBroker(initial_cash, fee_rate, slippage_rate)
    .on_kline(kline_event)          → 触发撮合，返回 OrderFilledEvent 列表
    .submit_order(order_request)    → 提交订单，返回本地 order_id
    .get_position(symbol)           → 持仓数量
    .get_cash()                     → 可用现金
    .get_equity(prices)             → 当前净值（含持仓市值）
    .get_trade_log()                → 所有成交记录 DataFrame
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional

import pandas as pd

from core.event import EventType, KlineEvent, OrderFilledEvent, OrderRequestEvent
from core.logger import audit_log, get_logger

log = get_logger(__name__)


@dataclass
class _PendingOrder:
    """内部挂单结构（不对外暴露）。"""
    order_id: str
    symbol: str
    side: str           # "buy" | "sell"
    order_type: str     # "limit" | "market"
    quantity: Decimal
    price: Optional[Decimal]  # 限价单价格，市价单为 None
    strategy_id: str
    request_id: str
    created_at: datetime
    # 市价单需等到下一根 K 线才能成交（防未来函数）
    submitted_at_ts: Optional[datetime] = None  # 提交时的 K 线时间戳


@dataclass
class _TradeRecord:
    """单笔成交记录。"""
    order_id: str
    symbol: str
    side: str
    filled_qty: Decimal
    avg_price: Decimal
    fee: Decimal
    fee_currency: str
    timestamp: datetime
    strategy_id: str


class SimulatedBroker:
    """
    回测模拟券商。

    维护：
    - 现金账户（quote currency，默认 USDT）
    - 持仓（各 symbol 的持仓数量）
    - 挂单队列（等待撮合的订单）
    - 成交日志

    Args:
        initial_cash:   初始资金（USDT）
        fee_rate:       手续费率（默认 0.001 = 0.1%）
        slippage_rate:  市价单滑点（默认 0.001 = 0.1%）
        min_order_size: 最小下单数量（防止碎片化）
    """

    def __init__(
        self,
        initial_cash: float = 100_000.0,
        fee_rate: float = 0.001,
        slippage_rate: float = 0.001,
        min_order_size: float = 1e-8,
    ) -> None:
        self._cash = Decimal(str(initial_cash))
        self._fee_rate = Decimal(str(fee_rate))
        self._slippage_rate = Decimal(str(slippage_rate))
        self._min_order_size = Decimal(str(min_order_size))

        self._positions: Dict[str, Decimal] = {}   # symbol -> qty
        self._pending_orders: List[_PendingOrder] = []
        self._trade_log: List[_TradeRecord] = []

        # 当前时间（由 on_kline 驱动，用于防未来函数判断）
        self._current_ts: Optional[datetime] = None

        log.info(
            "SimulatedBroker 初始化: cash={} fee_rate={} slippage={}",
            initial_cash,
            fee_rate,
            slippage_rate,
        )

    # ────────────────────────────────────────────────────────────
    # 公开接口
    # ────────────────────────────────────────────────────────────

    def on_kline(self, event: KlineEvent) -> List[OrderFilledEvent]:
        """
        处理 K 线事件：推进时间，尝试撮合挂单。

        市价单：只在 submitted_at_ts < event.timestamp 时才撮合
        （即提交订单时所在的 K 线已过，防止当根 K 线内成交）。

        Returns:
            本次 K 线触发的所有成交回报事件
        """
        self._current_ts = event.timestamp
        filled_events: List[OrderFilledEvent] = []
        remaining_orders: List[_PendingOrder] = []

        for order in self._pending_orders:
            if order.symbol != event.symbol:
                remaining_orders.append(order)
                continue

            filled = self._try_fill(order, event)
            if filled is not None:
                filled_events.append(filled)
            else:
                remaining_orders.append(order)

        self._pending_orders = remaining_orders
        return filled_events

    def submit_order(self, request: OrderRequestEvent) -> str:
        """
        提交订单到挂单队列。

        Returns:
            本地 order_id（UUID）
        """
        order_id = str(uuid.uuid4())
        pending = _PendingOrder(
            order_id=order_id,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            price=request.price,
            strategy_id=request.strategy_id,
            request_id=request.request_id,
            created_at=request.timestamp,
            submitted_at_ts=self._current_ts,  # 记录提交时所在 K 线时间
        )
        self._pending_orders.append(pending)
        log.debug(
            "订单入队: order_id={} symbol={} side={} type={} qty={} price={}",
            order_id,
            request.symbol,
            request.side,
            request.order_type,
            request.quantity,
            request.price,
        )
        return order_id

    def get_position(self, symbol: str) -> Decimal:
        """返回指定 symbol 的当前持仓数量（0 表示无持仓）。"""
        return self._positions.get(symbol, Decimal("0"))

    def get_cash(self) -> Decimal:
        """返回当前可用现金（USDT）。"""
        return self._cash

    def get_equity(self, prices: Dict[str, float]) -> Decimal:
        """
        计算当前总净值（现金 + 持仓市值）。

        Args:
            prices: {symbol: 当前价格} 的字典（由策略层提供最新 close）
        """
        equity = self._cash
        for symbol, qty in self._positions.items():
            if qty > 0 and symbol in prices:
                equity += qty * Decimal(str(prices[symbol]))
        return equity

    def get_trade_log(self) -> pd.DataFrame:
        """返回完整的成交日志 DataFrame。"""
        if not self._trade_log:
            return pd.DataFrame(
                columns=[
                    "order_id", "symbol", "side", "filled_qty",
                    "avg_price", "fee", "fee_currency", "timestamp", "strategy_id",
                ]
            )
        return pd.DataFrame([
            {
                "order_id": t.order_id,
                "symbol": t.symbol,
                "side": t.side,
                "filled_qty": float(t.filled_qty),
                "avg_price": float(t.avg_price),
                "fee": float(t.fee),
                "fee_currency": t.fee_currency,
                "timestamp": t.timestamp,
                "strategy_id": t.strategy_id,
            }
            for t in self._trade_log
        ])

    # ────────────────────────────────────────────────────────────
    # 内部撮合逻辑
    # ────────────────────────────────────────────────────────────

    def _try_fill(
        self,
        order: _PendingOrder,
        event: KlineEvent,
    ) -> Optional[OrderFilledEvent]:
        """
        尝试撮合单个订单。

        Returns:
            OrderFilledEvent（成交）或 None（未触发）
        """
        if order.order_type == "market":
            return self._fill_market(order, event)
        elif order.order_type == "limit":
            return self._fill_limit(order, event)
        return None

    def _fill_market(
        self,
        order: _PendingOrder,
        event: KlineEvent,
    ) -> Optional[OrderFilledEvent]:
        """
        市价单撮合。

        防未来函数规则：
        - 只在提交订单时的 K 线时间戳 < 当前 K 线时间戳时才撮合
        - 成交价 = 当前 K 线 open（非同根 K 线内收盘价）
        """
        # 防止在同一根 K 线内成交
        if order.submitted_at_ts is not None and order.submitted_at_ts >= event.timestamp:
            return None

        # 带滑点的市价成交价
        open_price = Decimal(str(event.open))
        if order.side == "buy":
            fill_price = open_price * (1 + self._slippage_rate)
        else:
            fill_price = open_price * (1 - self._slippage_rate)

        return self._execute_fill(order, fill_price, event.timestamp)

    def _fill_limit(
        self,
        order: _PendingOrder,
        event: KlineEvent,
    ) -> Optional[OrderFilledEvent]:
        """
        限价单撮合。

        规则：
        - buy 限价单：当前 K 线 low <= 限价时触发，成交价 = 限价
        - sell 限价单：当前 K 线 high >= 限价时触发，成交价 = 限价
        """
        if order.price is None:
            return None

        low = Decimal(str(event.low))
        high = Decimal(str(event.high))

        triggered = False
        if order.side == "buy" and low <= order.price:
            triggered = True
        elif order.side == "sell" and high >= order.price:
            triggered = True

        if not triggered:
            return None

        return self._execute_fill(order, order.price, event.timestamp)

    def _execute_fill(
        self,
        order: _PendingOrder,
        fill_price: Decimal,
        fill_ts: datetime,
    ) -> Optional[OrderFilledEvent]:
        """
        执行成交：更新账户余额 + 持仓，记录审计日志。

        Returns:
            OrderFilledEvent 或 None（余额不足时）
        """
        qty = order.quantity
        notional = qty * fill_price
        fee = notional * self._fee_rate
        fee_currency = "USDT"

        if order.side == "buy":
            total_cost = notional + fee
            if total_cost > self._cash:
                log.warning(
                    "余额不足，撤销买单: order_id={} 需要={:.4f} 可用={:.4f}",
                    order.order_id,
                    float(total_cost),
                    float(self._cash),
                )
                return None
            self._cash -= total_cost
            self._positions[order.symbol] = (
                self._positions.get(order.symbol, Decimal("0")) + qty
            )
        else:  # sell
            current_pos = self._positions.get(order.symbol, Decimal("0"))
            actual_qty = min(qty, current_pos)
            if actual_qty < self._min_order_size:
                log.warning(
                    "持仓不足，撤销卖单: order_id={} 持仓={} 需卖={}",
                    order.order_id,
                    float(current_pos),
                    float(qty),
                )
                return None
            qty = actual_qty
            notional = qty * fill_price
            fee = notional * self._fee_rate
            self._cash += notional - fee
            self._positions[order.symbol] = current_pos - qty

        # 记录成交
        trade = _TradeRecord(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            filled_qty=qty,
            avg_price=fill_price,
            fee=fee,
            fee_currency=fee_currency,
            timestamp=fill_ts,
            strategy_id=order.strategy_id,
        )
        self._trade_log.append(trade)

        # 审计日志（不可屏蔽）
        audit_log(
            "ORDER_FILLED",
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            qty=float(qty),
            price=float(fill_price),
            fee=float(fee),
            cash_after=float(self._cash),
        )

        return OrderFilledEvent(
            event_type=EventType.ORDER_FILLED,
            timestamp=fill_ts,
            source="simulated_broker",
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            filled_qty=qty,
            avg_price=fill_price,
            fee=fee,
            fee_currency=fee_currency,
        )
