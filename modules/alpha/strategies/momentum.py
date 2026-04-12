"""
modules/alpha/strategies/momentum.py — 价格动量策略

设计说明：
- 基于 N 期价格变化率（ROC）与 RSI 组合判断动量方向
- ROC > 阈值 且 RSI 不过热（< 70）→ 做多信号
- ROC < -阈值 且 RSI 不过冷（> 30）→ 单纯平仓（现货不做空）
- 使用 ATR 动态计算止损距离（ATR 止损）

参数：
    roc_window:     ROC 计算窗口（默认 10）
    roc_entry_pct:  ROC 触发阈值（默认 2.0，即 2%）
    rsi_window:     RSI 计算窗口（默认 14）
    rsi_upper:      RSI 过热阈值（默认 70，买入信号过滤）
    rsi_lower:      RSI 过冷阈值（默认 30，卖出信号过滤）
    atr_window:     ATR 窗口（默认 14）
    order_qty:      基础下单数量
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Deque, List, Optional

import pandas as pd

from core.event import KlineEvent, OrderRequestEvent
from core.logger import get_logger
from modules.alpha.base import BaseAlpha
from modules.alpha.features import FeatureEngine

log = get_logger(__name__)


class MomentumStrategy(BaseAlpha):
    """
    ROC + RSI 组合动量策略。

    信号规则（纯现货，无做空）：
    - 入场：ROC_{N} > roc_entry_pct 且 RSI < rsi_upper → 市价买入
    - 出场：ROC_{N} < -roc_entry_pct 且 RSI > rsi_lower → 市价卖出
    """

    def __init__(
        self,
        symbol: str,
        roc_window: int = 10,
        roc_entry_pct: float = 2.0,
        rsi_window: int = 14,
        rsi_upper: float = 70.0,
        rsi_lower: float = 30.0,
        atr_window: int = 14,
        order_qty: float = 0.01,
        timeframe: str = "1h",
    ) -> None:
        super().__init__(
            strategy_id=f"momentum_{roc_window}_{symbol.replace('/', '_')}",
            symbol=symbol,
            timeframe=timeframe,
        )
        self.roc_window = roc_window
        self.roc_entry_pct = roc_entry_pct
        self.rsi_window = rsi_window
        self.rsi_upper = rsi_upper
        self.rsi_lower = rsi_lower
        self.atr_window = atr_window
        self.order_qty = Decimal(str(order_qty))

        # 需要的最大历史窗口
        self._min_bars = max(roc_window, rsi_window, atr_window) + 5
        max_buf = self._min_bars + 10

        # 滑动缓冲区
        self._closes: Deque[float] = deque(maxlen=max_buf)
        self._highs: Deque[float] = deque(maxlen=max_buf)
        self._lows: Deque[float] = deque(maxlen=max_buf)
        self._volumes: Deque[float] = deque(maxlen=max_buf)

        self._in_position: bool = False

    def on_kline(self, event: KlineEvent) -> List[OrderRequestEvent]:
        if not event.is_closed or event.symbol != self.symbol:
            return []

        self._increment_bar(event)
        self._closes.append(float(event.close))
        self._highs.append(float(event.high))
        self._lows.append(float(event.low))
        self._volumes.append(float(event.volume))

        if self._is_warming_up(self._min_bars):
            return []

        # 构建临时 DataFrame 计算指标
        df = pd.DataFrame({
            "close": list(self._closes),
            "high":  list(self._highs),
            "low":   list(self._lows),
            "volume": list(self._volumes),
        })

        roc = FeatureEngine.roc(df["close"], self.roc_window)
        rsi = FeatureEngine.rsi(df["close"], self.rsi_window)

        curr_roc = roc.iloc[-1]
        curr_rsi = rsi.iloc[-1]

        if pd.isna(curr_roc) or pd.isna(curr_rsi):
            return []

        orders: List[OrderRequestEvent] = []

        # 买入信号
        if (
            not self._in_position
            and curr_roc > self.roc_entry_pct
            and curr_rsi < self.rsi_upper
        ):
            log.info(
                "{} 动量买入: ROC={:.2f}% RSI={:.1f}",
                self.strategy_id, curr_roc, curr_rsi,
            )
            orders.append(self._make_market_order(event, "buy", self.order_qty))
            self._in_position = True

        # 卖出信号（平多）
        elif (
            self._in_position
            and curr_roc < -self.roc_entry_pct
            and curr_rsi > self.rsi_lower
        ):
            log.info(
                "{} 动量卖出: ROC={:.2f}% RSI={:.1f}",
                self.strategy_id, curr_roc, curr_rsi,
            )
            orders.append(self._make_market_order(event, "sell", self.order_qty))
            self._in_position = False

        return orders
