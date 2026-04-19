"""
modules/alpha/strategies/ma_cross.py — 双均线穿越策略（MA Cross）v2

v2 优化记录：
- EMA 替代 SMA：降低信号滞后，更快识别趋势启动
- ADX 趋势过滤：ADX > 25 才允许开仓，ADX < 20 关闭新开仓信号
- 成交量过滤保留

设计说明：
- 策略逻辑：快线（短期 EMA）上穿慢线（长期 EMA）时买入，下穿时卖出
- 只有当策略维护的全仓（full position）不在持仓中时才买入

策略细节（防过拟合处理）：
- 需要至少 slow_window 根已收线 K 线才发出信号（预热期保护）
- ADX 过滤：趋势强度不够时不开仓，避免震荡市锯齿亏损
- 成交量过滤：信号产生当根 K 线的成交量必须高于 N 日均量（确认信号有效性）
- 不允许同根 K 线内反复开平仓

参数：
    fast_window:     快线窗口（默认 10）
    slow_window:     慢线窗口（默认 30）
    order_pct:       建议下单占当前账户估值的比例
    use_ema:         是否使用 EMA（默认 True，False 退化为 SMA）
    adx_filter:      是否开启 ADX 趋势过滤（默认 True）
    adx_entry_threshold:  ADX 开仓阈值（默认 25）
    adx_close_threshold:  ADX 休眠阈值（默认 20）
    volume_filter:   是否开启成交量过滤（默认 True）
    vol_ma_window:   成交量均线窗口（默认 20）
    vol_multiplier:  成交量过滤倍数（默认 0.8）
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Deque, List, Optional

import pandas as pd

from modules.alpha.base import BaseAlpha
from modules.alpha.features import FeatureEngine
from core.event import KlineEvent, OrderRequestEvent
from core.logger import get_logger

log = get_logger(__name__)


class MACrossStrategy(BaseAlpha):
    """
    双均线穿越（Golden Cross / Death Cross）策略 v2。

    v2 改进：
    - 默认使用 EMA（指数移动平均），信号更灵敏
    - ADX 趋势过滤：ADX > adx_entry_threshold 时才允许开仓
    - ADX < adx_close_threshold 时关闭新开仓信号避免震荡市锯齿亏损

    信号规则：
    - 金叉（fast MA > slow MA，前一根 fast MA <= slow MA）且 ADX > 25 → 买入
    - 死叉（fast MA < slow MA，前一根 fast MA >= slow MA）→ 卖出（不受 ADX 限制）

    仓位状态机：
        FLAT → (金叉 + ADX确认) → LONG → (死叉信号) → FLAT
    """

    def __init__(
        self,
        symbol: str,
        fast_window: int = 10,
        slow_window: int = 30,
        order_qty: float = 0.01,
        use_ema: bool = True,
        adx_filter: bool = True,
        adx_entry_threshold: float = 25.0,
        adx_close_threshold: float = 20.0,
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
        self.use_ema = use_ema
        self.adx_filter = adx_filter
        self.adx_entry_threshold = adx_entry_threshold
        self.adx_close_threshold = adx_close_threshold
        self.volume_filter = volume_filter
        self.vol_ma_window = vol_ma_window
        self.vol_multiplier = vol_multiplier

        # 内部状态：滑动窗口缓存最近 N 根 K 线
        max_buf = max(slow_window, vol_ma_window) + 20  # ADX 也需要额外缓冲
        self._closes: Deque[float] = deque(maxlen=max_buf)
        self._highs: Deque[float] = deque(maxlen=max_buf)
        self._lows: Deque[float] = deque(maxlen=max_buf)
        self._volumes: Deque[float] = deque(maxlen=max_buf)

        # 持仓状态：True = 持有多头
        self._in_position: bool = False

        # 记录上一根 K 线的均线状态（用于判断穿越）
        self._prev_fast: Optional[float] = None
        self._prev_slow: Optional[float] = None

        log.info(
            "{} 初始化: fast={} slow={} qty={} ema={} adx_filter={} vol_filter={}",
            self.strategy_id,
            fast_window,
            slow_window,
            order_qty,
            use_ema,
            adx_filter,
            volume_filter,
        )

    def on_kline(self, event: KlineEvent) -> List[OrderRequestEvent]:
        """
        处理新 K 线：
        1. 更新滑动窗口缓存
        2. 检查是否在预热期
        3. 计算快慢均线（EMA 或 SMA）
        4. 计算 ADX 趋势强度（可选）
        5. 判断穿越信号（金叉/死叉）
        6. 成交量过滤确认（可选）
        7. 产出订单请求（或返回空列表）
        """
        if not event.is_closed or event.symbol != self.symbol:
            return []

        self._increment_bar(event)
        self._closes.append(float(event.close))
        self._highs.append(float(event.high))
        self._lows.append(float(event.low))
        self._volumes.append(float(event.volume))

        # 预热期检查
        if self._is_warming_up(self.slow_window):
            log.debug(
                "{} 预热中 ({}/{})",
                self.strategy_id,
                self._bar_count,
                self.slow_window,
            )
            return []

        # 构建 DataFrame 用于指标计算
        closes_series = pd.Series(list(self._closes))

        # 计算快慢均线
        if self.use_ema:
            fast_series = FeatureEngine.ema(closes_series, self.fast_window)
            slow_series = FeatureEngine.ema(closes_series, self.slow_window)
        else:
            fast_series = FeatureEngine.sma(closes_series, self.fast_window)
            slow_series = FeatureEngine.sma(closes_series, self.slow_window)

        curr_fast = fast_series.iloc[-1]
        curr_slow = slow_series.iloc[-1]

        if pd.isna(curr_fast) or pd.isna(curr_slow):
            return []

        # ADX 计算（需要 high/low/close）
        curr_adx = None
        if self.adx_filter:
            df = pd.DataFrame({
                "high": list(self._highs),
                "low": list(self._lows),
                "close": list(self._closes),
            })
            adx_series = FeatureEngine.adx(df, window=14)
            curr_adx = adx_series.iloc[-1] if not pd.isna(adx_series.iloc[-1]) else None

        ma_type = "EMA" if self.use_ema else "SMA"
        log.debug(
            "[{}] bar#{} close={:.4f} fast_{}={:.4f} slow_{}={:.4f} ADX={} in_pos={}",
            self.strategy_id, self._bar_count,
            float(self._closes[-1]),
            ma_type, curr_fast, ma_type, curr_slow,
            f"{curr_adx:.1f}" if curr_adx is not None else "N/A",
            self._in_position,
        )

        orders: List[OrderRequestEvent] = []

        # 判断穿越
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

            # ADX 过滤（仅限买入信号，卖出不受限制）
            adx_confirmed = True
            if self.adx_filter and curr_adx is not None:
                if curr_adx < self.adx_entry_threshold:
                    adx_confirmed = False
                    if golden_cross:
                        log.info(
                            "{} 金叉被 ADX 过滤: ADX={:.1f} < 阈值 {:.0f}",
                            self.strategy_id, curr_adx, self.adx_entry_threshold,
                        )

            if golden_cross and not self._in_position and vol_confirmed and adx_confirmed:
                ma_type = "EMA" if self.use_ema else "SMA"
                log.info(
                    "{} 金叉信号: close={} fast_{}={:.4f} slow_{}={:.4f}{}",
                    self.strategy_id,
                    float(event.close),
                    ma_type, curr_fast,
                    ma_type, curr_slow,
                    f" ADX={curr_adx:.1f}" if curr_adx is not None else "",
                )
                orders.append(
                    self._make_market_order(event, "buy", self.order_qty)
                )
                self._in_position = True

            elif death_cross and self._in_position:
                ma_type = "EMA" if self.use_ema else "SMA"
                log.info(
                    "{} 死叉信号: close={} fast_{}={:.4f} slow_{}={:.4f}{}",
                    self.strategy_id,
                    float(event.close),
                    ma_type, curr_fast,
                    ma_type, curr_slow,
                    f" ADX={curr_adx:.1f}" if curr_adx is not None else "",
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
        avg_vol = sum(volumes_list[-self.vol_ma_window:]) / self.vol_ma_window
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
