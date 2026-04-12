"""
modules/alpha/strategies/ma_cross.py — 双均线穿越策略（MA Cross）

设计说明：
- 这是系统的"标准策略模板"，展示如何正确实现一个完整策略
- 策略逻辑：快线（短期 SMA）上穿慢线（长期 SMA）时买入，下穿时卖出
- 只有当策略维护的全仓（full position）不在持仓中时才买入

策略细节（防过拟合处理）：
- 需要至少 slow_window 根已收线 K 线才发出信号（预热期保护）
- 成交量过滤：信号产生当根 K 线的成交量必须高于 N 日均量（确认信号有效性）
- 不允许同根 K 线内反复开平仓

仓位管理（保守原则）：
- 固定名义金额下单（fraction of equity），由风控层最终决定仓位大小
- 策略只发出"方向 + 建议数量"，最终数量由 RiskManager 可削减

参数：
    fast_window:   快线窗口（默认 10）
    slow_window:   慢线窗口（默认 30）
    order_pct:     建议下单占当前账户估值的比例（默认 0.95，近满仓）
    volume_filter: 是否开启成交量过滤（默认 True）
    vol_ma_window: 成交量均线窗口（默认 20）
    vol_multiplier:成交量过滤倍数（默认 0.8，允许略低于均量）
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Deque, List, Optional

from modules.alpha.base import BaseAlpha
from modules.alpha.features import FeatureEngine
from core.event import KlineEvent, OrderRequestEvent
from core.logger import get_logger

log = get_logger(__name__)


class MACrossStrategy(BaseAlpha):
    """
    双均线穿越（Golden Cross / Death Cross）策略。

    信号规则：
    - 金叉（fast SMA > slow SMA，前一根 fast SMA <= slow SMA）→ 买入
    - 死叉（fast SMA < slow SMA，前一根 fast SMA >= slow SMA）→ 卖出

    仓位状态机（简单版）：
        FLAT → (金叉信号) → LONG → (死叉信号) → FLAT

    Args:
        symbol:         目标交易对
        fast_window:    快线窗口
        slow_window:    慢线窗口
        order_qty:      每次建仓的基础数量（基础币，如 BTC）。
                        建议由 RiskManager 在运行时动态调整。
        volume_filter:  是否开启成交量过滤
        vol_ma_window:  成交量均线窗口
        vol_multiplier: 过滤阈值倍数（当前成交量 >= vol_multiplier * 均量才确认信号）
        timeframe:      K 线周期
    """

    def __init__(
        self,
        symbol: str,
        fast_window: int = 10,
        slow_window: int = 30,
        order_qty: float = 0.01,
        volume_filter: bool = True,
        vol_ma_window: int = 20,
        vol_multiplier: float = 0.8,
        timeframe: str = "1h",
    ) -> None:
        super().__init__(
            strategy_id=f"ma_cross_{fast_window}_{slow_window}_{symbol.replace('/', '_')}",
            symbol=symbol,
            timeframe=timeframe,
        )
        if fast_window >= slow_window:
            raise ValueError(
                f"fast_window({fast_window}) 必须小于 slow_window({slow_window})"
            )

        self.fast_window = fast_window
        self.slow_window = slow_window
        self.order_qty = Decimal(str(order_qty))
        self.volume_filter = volume_filter
        self.vol_ma_window = vol_ma_window
        self.vol_multiplier = vol_multiplier

        # 内部状态：滑动窗口缓存最近 N 根 K 线
        max_buf = max(slow_window, vol_ma_window) + 2  # 多留 2 根用于穿越判断
        self._closes: Deque[float] = deque(maxlen=max_buf)
        self._volumes: Deque[float] = deque(maxlen=max_buf)

        # 持仓状态：True = 持有多头
        self._in_position: bool = False

        # 记录上一根 K 线的均线状态（用于判断穿越）
        self._prev_fast: Optional[float] = None
        self._prev_slow: Optional[float] = None

        log.info(
            "{} 初始化: fast={} slow={} qty={} vol_filter={}",
            self.strategy_id,
            fast_window,
            slow_window,
            order_qty,
            volume_filter,
        )

    def on_kline(self, event: KlineEvent) -> List[OrderRequestEvent]:
        """
        处理新 K 线：
        1. 更新滑动窗口缓存
        2. 检查是否在预热期
        3. 计算快慢均线
        4. 判断穿越信号（金叉/死叉）
        5. 可选：成交量过滤确认
        6. 产出订单请求（或返回空列表）
        """
        # 只处理已收线 K 线（实盘中也适用，避免在 K 线未收盘时产出噪声信号）
        if not event.is_closed or event.symbol != self.symbol:
            return []

        self._increment_bar(event)
        self._closes.append(float(event.close))
        self._volumes.append(float(event.volume))

        # 预热期检查：至少需要 slow_window 根 K 线
        if self._is_warming_up(self.slow_window):
            log.debug(
                "{} 预热中 ({}/{})",
                self.strategy_id,
                self._bar_count,
                self.slow_window,
            )
            return []

        # 计算当前快慢均线
        import statistics
        closes_list = list(self._closes)
        curr_fast = statistics.mean(closes_list[-self.fast_window:])
        curr_slow = statistics.mean(closes_list[-self.slow_window:])

        orders: List[OrderRequestEvent] = []

        # 判断穿越（需要上一根均线值）
        if self._prev_fast is not None and self._prev_slow is not None:
            golden_cross = (
                self._prev_fast <= self._prev_slow and curr_fast > curr_slow
            )
            death_cross = (
                self._prev_fast >= self._prev_slow and curr_fast < curr_slow
            )

            # 成交量确认（可选）
            vol_confirmed = True
            if self.volume_filter:
                vol_confirmed = self._check_volume()

            if golden_cross and not self._in_position and vol_confirmed:
                log.info(
                    "{} 金叉信号: close={} fast={:.4f} slow={:.4f}",
                    self.strategy_id,
                    float(event.close),
                    curr_fast,
                    curr_slow,
                )
                orders.append(
                    self._make_market_order(event, "buy", self.order_qty)
                )
                self._in_position = True

            elif death_cross and self._in_position:
                log.info(
                    "{} 死叉信号: close={} fast={:.4f} slow={:.4f}",
                    self.strategy_id,
                    float(event.close),
                    curr_fast,
                    curr_slow,
                )
                orders.append(
                    self._make_market_order(event, "sell", self.order_qty)
                )
                self._in_position = False

        # 更新均线历史状态
        self._prev_fast = curr_fast
        self._prev_slow = curr_slow

        return orders

    def _check_volume(self) -> bool:
        """
        成交量过滤：当前成交量是否高于 N 日均量的 vol_multiplier 倍。
        """
        if len(self._volumes) < self.vol_ma_window:
            return True  # 样本不足时不过滤

        volumes_list = list(self._volumes)
        import statistics
        avg_vol = statistics.mean(volumes_list[-self.vol_ma_window:])
        curr_vol = volumes_list[-1]

        confirmed = curr_vol >= avg_vol * self.vol_multiplier
        if not confirmed:
            log.debug(
                "{} 成交量不足，信号被过滤: curr_vol={:.2f} avg_vol={:.2f}",
                self.strategy_id,
                curr_vol,
                avg_vol,
            )
        return confirmed
