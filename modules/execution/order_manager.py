"""
modules/execution/order_manager.py — 订单生命周期管理器

设计说明：
- 跟踪所有已提交订单从"提交"到"终态"的完整生命周期
- 负责超时检测、状态轮询、自动撤单逻辑
- 维护订单状态机：PENDING → SUBMITTED → PARTIAL_FILLED → FILLED / CANCELLED / FAILED

订单状态机：
    ┌──────────┐   submit    ┌───────────┐   poll fills   ┌────────────────┐
    │ PENDING  │ ──────────> │ SUBMITTED │ ─────────────> │ PARTIAL_FILLED │
    └──────────┘             └───────────┘                └────────────────┘
                                   │                              │
                                   │ timeout / exchange reject    │ fully filled
                                   ▼                              ▼
                              ┌─────────┐                   ┌────────┐
                              │CANCELLED│                   │ FILLED │
                              └─────────┘                   └────────┘

接口：
    OrderManager(gateway, fill_timeout_s, poll_interval_s)
    .submit(request)              → order_id
    .poll_fills()                 → List[FillResult]   （每个心跳周期调用）
    .cancel_timed_out_orders()    → int                （撤销超时订单数量）
    .get_open_orders()            → List[OrderRecord]
    .get_order_history()          → List[OrderRecord]
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum, auto
from typing import Dict, List, Optional

from modules.execution.gateway import CCXTGateway
from core.exceptions import ExchangeConnectionError, OrderSubmissionError
from core.logger import audit_log, get_logger

log = get_logger(__name__)


class OrderStatus(Enum):
    PENDING = auto()          # 尚未提交到交易所
    SUBMITTED = auto()        # 已提交，等待成交
    PARTIAL_FILLED = auto()   # 部分成交
    FILLED = auto()           # 全部成交
    CANCELLED = auto()        # 已撤销
    FAILED = auto()           # 提交失败


@dataclass
class OrderRecord:
    """订单记录，追踪完整生命周期。"""
    local_id: str             # 本地唯一 ID（与 request_id 对应）
    exchange_id: str          # 交易所订单 ID（提交成功后填充）
    symbol: str
    side: str                 # "buy" | "sell"
    order_type: str           # "limit" | "market"
    quantity: Decimal
    price: Optional[Decimal]
    strategy_id: str
    request_id: str
    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    status: OrderStatus = OrderStatus.PENDING
    error_msg: str = ""


@dataclass
class FillResult:
    """单次轮询中检测到的新增成交结果。"""
    order_record: OrderRecord
    new_filled_qty: Decimal
    avg_price: Decimal
    is_complete: bool   # True = 全部成交，False = 部分成交


class OrderManager:
    """
    订单生命周期管理器。

    职责：
    1. 接收订单请求 → 调用 CCXTGateway 提交
    2. 在 poll_fills() 中查询所有挂单状态，检测新增成交
    3. 超时订单自动撤单
    4. 维护完整的订单历史，供审计和绩效分析使用

    Args:
        gateway:         CCXTGateway 实例
        fill_timeout_s:  订单超时时间（秒），超时后自动撤单（默认 300s = 5分钟）
        poll_interval_s: 最小轮询间隔（秒），避免过于频繁查询
    """

    def __init__(
        self,
        gateway: CCXTGateway,
        fill_timeout_s: int = 300,
        poll_interval_s: float = 5.0,
    ) -> None:
        self.gateway = gateway
        self.fill_timeout_s = fill_timeout_s
        self.poll_interval_s = poll_interval_s

        # local_id → OrderRecord
        self._open_orders: Dict[str, OrderRecord] = {}
        self._history: List[OrderRecord] = []

        self._last_poll_ts: Optional[datetime] = None

        log.info(
            "OrderManager 初始化: timeout={}s poll={}s",
            fill_timeout_s,
            poll_interval_s,
        )

    # ────────────────────────────────────────────────────────────
    # 公开接口
    # ────────────────────────────────────────────────────────────

    def submit(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Optional[Decimal],
        strategy_id: str,
        request_id: Optional[str] = None,
    ) -> str:
        """
        提交订单并创建订单记录。

        Args:
            symbol:      交易对
            side:        "buy" | "sell"
            order_type:  "limit" | "market"
            quantity:    数量
            price:       限价（市价单为 None）
            strategy_id: 产生此订单的策略ID（用于审计追溯）
            request_id:  可选，上游 OrderRequestEvent 的 request_id

        Returns:
            本地订单 ID（local_id）

        Raises:
            OrderSubmissionError:    订单被交易所拒绝
            ExchangeConnectionError: 网络异常
        """
        local_id = request_id or str(uuid.uuid4())

        record = OrderRecord(
            local_id=local_id,
            exchange_id="",
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            strategy_id=strategy_id,
            request_id=local_id,
        )

        try:
            exchange_id = self.gateway.submit_order(
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=float(quantity),
                price=float(price) if price is not None else None,
                client_order_id=local_id,
            )
            record.exchange_id = exchange_id
            record.status = OrderStatus.SUBMITTED
            record.submitted_at = datetime.now(tz=timezone.utc)
            self._open_orders[local_id] = record
            log.info(
                "订单已提交: local_id={} exchange_id={} {} {} {}",
                local_id, exchange_id, symbol, side, float(quantity),
            )

        except (OrderSubmissionError, ExchangeConnectionError) as exc:
            record.status = OrderStatus.FAILED
            record.error_msg = str(exc)
            self._history.append(record)
            audit_log(
                "ORDER_FAILED",
                local_id=local_id,
                symbol=symbol,
                side=side,
                error=str(exc),
            )
            log.error("订单提交失败: local_id={} error={}", local_id, exc)
            raise

        return local_id

    def poll_fills(self) -> List[FillResult]:
        """
        轮询所有挂单的最新状态，返回新增的成交结果。

        应在实盘主循环的每个心跳（如 5 秒）中调用一次。

        Returns:
            本次轮询中检测到的所有新增成交的 FillResult 列表
        """
        if not self._open_orders:
            return []

        now = datetime.now(tz=timezone.utc)
        results: List[FillResult] = []
        to_remove: List[str] = []

        for local_id, record in self._open_orders.items():
            if not record.exchange_id:
                continue

            try:
                order_data = self.gateway.fetch_order(record.exchange_id, record.symbol)
            except ExchangeConnectionError as exc:
                log.warning("查询订单状态失败（跳过）: local_id={} error={}", local_id, exc)
                continue

            status_str = order_data.get("status", "open")
            filled = Decimal(str(order_data.get("filled", 0)))
            avg_price = Decimal(str(order_data.get("average", order_data.get("price", 0)) or 0))

            # 检测新增成交量
            new_filled = filled - record.filled_qty
            if new_filled > Decimal("0"):
                record.filled_qty = filled
                record.avg_fill_price = avg_price
                is_complete = (status_str in {"closed", "filled"}) or (filled >= record.quantity)

                if is_complete:
                    record.status = OrderStatus.FILLED
                    record.filled_at = now
                    to_remove.append(local_id)
                    self._history.append(record)
                    audit_log(
                        "ORDER_FILLED",
                        local_id=local_id,
                        exchange_id=record.exchange_id,
                        symbol=record.symbol,
                        side=record.side,
                        filled_qty=float(filled),
                        avg_price=float(avg_price),
                    )
                else:
                    record.status = OrderStatus.PARTIAL_FILLED

                results.append(FillResult(
                    order_record=record,
                    new_filled_qty=new_filled,
                    avg_price=avg_price,
                    is_complete=is_complete,
                ))

        for local_id in to_remove:
            del self._open_orders[local_id]

        return results

    def cancel_timed_out_orders(self) -> int:
        """
        检查并撤销所有超时的挂单。

        应在每个心跳中调用。

        Returns:
            本次撤销的订单数量
        """
        now = datetime.now(tz=timezone.utc)
        to_cancel: List[str] = []

        for local_id, record in self._open_orders.items():
            if record.submitted_at is None:
                continue
            elapsed = (now - record.submitted_at).total_seconds()
            if elapsed > self.fill_timeout_s:
                to_cancel.append(local_id)

        cancelled_count = 0
        for local_id in to_cancel:
            record = self._open_orders[local_id]
            try:
                success = self.gateway.cancel_order(record.exchange_id, record.symbol)
                if success:
                    record.status = OrderStatus.CANCELLED
                    self._history.append(record)
                    del self._open_orders[local_id]
                    cancelled_count += 1
                    log.warning(
                        "超时订单已撤销: local_id={} symbol={} elapsed={}s",
                        local_id, record.symbol,
                        int((now - record.submitted_at).total_seconds()),
                    )
            except ExchangeConnectionError as exc:
                log.error("撤单网络错误: local_id={} error={}", local_id, exc)

        return cancelled_count

    def get_open_orders(self) -> List[OrderRecord]:
        """返回当前所有未完成订单（快照）。"""
        return list(self._open_orders.values())

    def get_order_history(self) -> List[OrderRecord]:
        """返回所有历史订单（已完成/失败/撤单）。"""
        return list(self._history)
