"""
modules/data/realtime/orderbook_types.py — 订单簿与成交流的核心数据契约

设计说明：
- 定义实时数据层所有模块共享的不可变数据结构
- OrderBookSnapshot：完整的订单簿快照（序列一致性保证后才发布）
- TradeTick：单笔成交记录（原始 + 规范化）
- OrderBookDelta：增量更新包（ws_client 层解析产出）
- DepthLevel：单档量价（(price, size) 命名元组）
- GapStatus：订单簿序列缺口状态枚举

所有字段命名遵循 CCXT 标准，便于后续接入真实交易所适配层。

日志标签：[OrderBookTypes]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Literal, Optional


# ══════════════════════════════════════════════════════════════
# 一、基础类型
# ══════════════════════════════════════════════════════════════

class GapStatus(str, Enum):
    """订单簿序列号缺口状态。"""

    OK = "ok"                     # 序列连续，无缺口
    GAP_DETECTED = "gap_detected" # 检测到缺口，当前快照不可信
    RECOVERING = "recovering"     # 正在通过 REST 快照回补
    RECOVERED = "recovered"       # 回补完成，序列重新对齐
    FATAL = "fatal"               # 无法恢复，需重新建立 WebSocket 连接

    def is_healthy(self) -> bool:
        """当前序列状态是否可以向策略层发布快照。"""
        return self in (GapStatus.OK, GapStatus.RECOVERED)


class TradeSide(str, Enum):
    """成交方向。"""

    BUY = "buy"
    SELL = "sell"
    UNKNOWN = "unknown"


# ══════════════════════════════════════════════════════════════
# 二、核心数据结构
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class DepthLevel:
    """单档量价（订单簿中的一档报价）。"""

    price: float
    size: float

    def is_empty(self) -> bool:
        return self.size <= 0.0

    def notional(self) -> float:
        return self.price * self.size


@dataclass(frozen=True)
class TradeTick:
    """
    单笔成交记录（规范化后的成交流数据）。

    Attributes:
        symbol:       交易对，如 "BTC/USDT"
        exchange:     交易所名称，如 "binance"
        trade_id:     交易所成交 ID（唯一）
        side:         成交方向（taker 方向）
        price:        成交价格
        size:         成交数量（base 单位）
        notional:     成交金额（quote 单位）= price × size
        received_at:  本地接收时间戳（UTC）
        exchange_ts:  交易所报告的成交时间（UTC），可能为 None（数据源未提供）
        is_liquidation: 是否为强平单
        debug_payload: 原始交易所数据（用于审计）
    """

    symbol: str
    exchange: str
    trade_id: str
    side: TradeSide
    price: float
    size: float
    notional: float
    received_at: datetime
    exchange_ts: Optional[datetime] = None
    is_liquidation: bool = False
    debug_payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create_mock(
        cls,
        symbol: str = "BTC/USDT",
        exchange: str = "mock",
        price: float = 50000.0,
        size: float = 0.01,
        side: TradeSide = TradeSide.BUY,
        trade_id: str = "mock-trade-001",
        received_at: Optional[datetime] = None,
    ) -> "TradeTick":
        """创建测试用 mock 成交记录。"""
        ts = received_at or datetime.now(tz=timezone.utc)
        return cls(
            symbol=symbol,
            exchange=exchange,
            trade_id=trade_id,
            side=side,
            price=price,
            size=size,
            notional=price * size,
            received_at=ts,
            exchange_ts=ts,
            debug_payload={"mock": True},
        )


@dataclass(frozen=True)
class OrderBookSnapshot:
    """
    完整的订单簿快照（序列一致性保证后才允许发布到策略层）。

    设计约束：
    - 只有 GapStatus.is_healthy() 为 True 时，下游才应消费本快照
    - bids 按价格从高到低排列，asks 按价格从低到高排列
    - spread_bps / mid_price / imbalance 是预计算好的派生字段，节省下游计算

    Attributes:
        symbol:            交易对
        exchange:          交易所名称
        sequence_id:       WebSocket 消息序列号（用于检测缺口）
        best_bid:          最优买一价
        best_ask:          最优卖一价
        bids:              买盘档位列表（price 降序，最多 top-N）
        asks:              卖盘档位列表（price 升序，最多 top-N）
        spread_bps:        买卖价差（基点），= (ask - bid) / mid × 10000
        mid_price:         中间价 = (best_bid + best_ask) / 2
        imbalance:         订单簿不平衡度 ∈ [-1, 1]，正值偏买，负值偏卖
                           = (bid_qty - ask_qty) / (bid_qty + ask_qty)，取 top-N
        received_at:       本地接收时间戳（UTC）
        exchange_ts:       交易所报告的快照时间（UTC），可能为 None
        gap_status:        序列号状态（策略层应检查此值）
        is_gap_recovered:  是否通过 REST 快照回补恢复序列
        depth_levels:      本次快照的有效档位数量（bid + ask 合计）
        debug_payload:     调试信息（sequence_id 追踪、解析耗时等）
    """

    symbol: str
    exchange: str
    sequence_id: int
    best_bid: float
    best_ask: float
    bids: list[DepthLevel]
    asks: list[DepthLevel]
    spread_bps: float
    mid_price: float
    imbalance: float
    received_at: datetime
    exchange_ts: Optional[datetime] = None
    gap_status: GapStatus = GapStatus.OK
    is_gap_recovered: bool = False
    depth_levels: int = 0
    debug_payload: dict[str, Any] = field(default_factory=dict)

    def is_healthy(self) -> bool:
        """快照是否健康（序列连续、价差合理）。"""
        return (
            self.gap_status.is_healthy()
            and self.best_bid > 0
            and self.best_ask > 0
            and self.best_ask > self.best_bid
        )

    def latency_ms(self) -> Optional[float]:
        """本地接收与交易所时间戳之间的延迟（毫秒），None 表示 exchange_ts 不可用。"""
        if self.exchange_ts is None:
            return None
        delta = self.received_at - self.exchange_ts
        return delta.total_seconds() * 1000

    @classmethod
    def create_mock(
        cls,
        symbol: str = "BTC/USDT",
        exchange: str = "mock",
        sequence_id: int = 1,
        mid_price: float = 50000.0,
        spread_bps: float = 2.0,
        depth: int = 5,
        received_at: Optional[datetime] = None,
    ) -> "OrderBookSnapshot":
        """创建测试用 mock 订单簿快照。"""
        ts = received_at or datetime.now(tz=timezone.utc)
        half_spread = mid_price * spread_bps / 20000
        best_bid = mid_price - half_spread
        best_ask = mid_price + half_spread

        bids = [
            DepthLevel(price=best_bid - i * 0.5, size=1.0 + i * 0.1)
            for i in range(depth)
        ]
        asks = [
            DepthLevel(price=best_ask + i * 0.5, size=1.0 + i * 0.1)
            for i in range(depth)
        ]

        bid_qty = sum(d.size for d in bids)
        ask_qty = sum(d.size for d in asks)
        imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty) if (bid_qty + ask_qty) > 0 else 0.0

        return cls(
            symbol=symbol,
            exchange=exchange,
            sequence_id=sequence_id,
            best_bid=best_bid,
            best_ask=best_ask,
            bids=bids,
            asks=asks,
            spread_bps=spread_bps,
            mid_price=mid_price,
            imbalance=imbalance,
            received_at=ts,
            exchange_ts=ts,
            gap_status=GapStatus.OK,
            depth_levels=depth * 2,
            debug_payload={"mock": True},
        )


@dataclass(frozen=True)
class OrderBookDelta:
    """
    订单簿增量更新包（WebSocket 推送，未经序列校验）。

    DepthCache 消费此结构合并到当前快照。

    Attributes:
        symbol:      交易对
        exchange:    交易所
        sequence_id: 本次增量的序列号
        prev_sequence_id: 期望的上一个序列号（用于 gap 检测）
        bid_updates: 买盘更新档位（size=0 表示删除该档位）
        ask_updates: 卖盘更新档位（size=0 表示删除该档位）
        received_at: 本地接收时间
        exchange_ts: 交易所时间（可选）
        is_snapshot: True 表示这是一个全量快照包，而非增量包
    """

    symbol: str
    exchange: str
    sequence_id: int
    prev_sequence_id: int
    bid_updates: list[DepthLevel]
    ask_updates: list[DepthLevel]
    received_at: datetime
    exchange_ts: Optional[datetime] = None
    is_snapshot: bool = False
    debug_payload: dict[str, Any] = field(default_factory=dict)

    def expected_sequence(self) -> int:
        """期望的下一个序列号（= 本次 sequence_id + 1）。"""
        return self.sequence_id + 1
