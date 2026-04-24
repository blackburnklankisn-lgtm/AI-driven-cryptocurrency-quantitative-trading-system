"""
modules/alpha/regime/feature_source.py — Regime 特征提取器

从 DataKitchen 或原始 OHLCV DataFrame 中，提取市场环境识别所需的结构化特征向量。

设计说明：
- 不依赖 MLFeatureBuilder 的完整 60+ 特征，只提取 Regime 所需的少量、可解释特征
- 支持直接从原始 OHLCV 提取（冷启动时 DataKitchen 还未初始化）
- 也支持从已有 feature_frame（DataKitchen 的 regime_features 视图）复用，避免重复计算
- 输出 RegimeFeatures 结构化容器，供 RegimeScorer 消费

日志标签：[RegimeFeatureSource]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from core.logger import get_logger

log = get_logger(__name__)

# Regime 特征提取默认窗口参数
_DEFAULT_RETURNS_WINDOW = 20
_DEFAULT_VOL_WINDOW = 20
_DEFAULT_ADX_PERIOD = 14
_DEFAULT_RSI_PERIOD = 14
_DEFAULT_ATR_PERIOD = 14
_MIN_BARS_REQUIRED = 30  # 至少需要多少根 bar 才能产出有效特征


@dataclass
class RegimeFeatures:
    """Regime 评分所需的结构化特征容器。"""

    # 收益率特征
    return_1: float = 0.0           # 最近 1 根 bar 收益率
    return_5: float = 0.0           # 最近 5 根 bar 累计收益率
    return_20: float = 0.0          # 最近 20 根 bar 累计收益率
    ret_roll_mean_20: float = 0.0   # 滚动均值（方向信号）
    ret_roll_std_20: float = 0.01   # 滚动波动率（>0）

    # 趋势特征
    price_vs_sma20: float = 0.0     # close / SMA20 - 1（价格偏离度）
    price_vs_sma50: float = 0.0     # close / SMA50 - 1
    adx: float = 0.0                # ADX（趋势强度，0-100）

    # 动量特征
    rsi_14: float = 50.0            # RSI（0-100）

    # 波动率特征
    atr_pct: float = 0.0            # ATR / close（百分比 ATR）
    bb_width: float = 0.0           # 布林带宽度（(upper-lower)/mid）

    # 成交量特征
    volume_ratio: float = 1.0       # volume / rolling_mean_volume

    # 元信息
    valid: bool = True              # 特征是否有效（bar 数不足时为 False）
    n_bars: int = 0                 # 实际可用 bar 数量


class RegimeFeatureSource:
    """
    Regime 特征提取器。

    使用方式（优先从 regime_features 视图直接读取）：
        source = RegimeFeatureSource()

        # 方式 A：从原始 OHLCV 提取（冷启动）
        rf = source.extract_from_ohlcv(ohlcv_df)

        # 方式 B：从 DataKitchen 的 regime_features 视图提取（热路径）
        rf = source.extract_from_frame(regime_feature_df)

    Args:
        returns_window:   滚动均值/标准差窗口
        vol_window:       波动率统计窗口
        min_bars:         产出有效特征所需最少 bar 数
    """

    def __init__(
        self,
        returns_window: int = _DEFAULT_RETURNS_WINDOW,
        vol_window: int = _DEFAULT_VOL_WINDOW,
        min_bars: int = _MIN_BARS_REQUIRED,
    ) -> None:
        self.returns_window = returns_window
        self.vol_window = vol_window
        self.min_bars = min_bars

    # ────────────────────────────────────────────────────────────
    # 公开接口
    # ────────────────────────────────────────────────────────────

    def extract_from_ohlcv(self, df: pd.DataFrame) -> RegimeFeatures:
        """
        直接从原始 OHLCV DataFrame 提取 Regime 特征（适合冷启动）。

        Args:
            df: 必须包含列 [open, high, low, close, volume]；按时间升序

        Returns:
            RegimeFeatures（bar 数不足时 valid=False）
        """
        n = len(df)
        if n < self.min_bars:
            log.warning(
                "[RegimeFeatureSource] bar 数不足 ({}), 需要至少 {} 根, 返回降级特征",
                n, self.min_bars,
            )
            return RegimeFeatures(valid=False, n_bars=n)

        try:
            close = df["close"].values.astype(float)
            high = df["high"].values.astype(float)
            low = df["low"].values.astype(float)
            volume = df["volume"].values.astype(float)

            # 收益率序列
            returns = np.diff(np.log(close + 1e-10))

            rf = RegimeFeatures(
                return_1=float(returns[-1]) if len(returns) > 0 else 0.0,
                return_5=float(np.sum(returns[-5:])) if len(returns) >= 5 else 0.0,
                return_20=float(np.sum(returns[-20:])) if len(returns) >= 20 else 0.0,
                ret_roll_mean_20=float(np.mean(returns[-20:])) if len(returns) >= 20 else 0.0,
                ret_roll_std_20=float(np.std(returns[-20:]) + 1e-8) if len(returns) >= 20 else 0.01,
                price_vs_sma20=self._price_vs_sma(close, 20),
                price_vs_sma50=self._price_vs_sma(close, 50),
                adx=self._calc_adx(high, low, close, period=_DEFAULT_ADX_PERIOD),
                rsi_14=self._calc_rsi(close, period=_DEFAULT_RSI_PERIOD),
                atr_pct=self._calc_atr_pct(high, low, close, period=_DEFAULT_ATR_PERIOD),
                bb_width=self._calc_bb_width(close, period=20),
                volume_ratio=self._calc_volume_ratio(volume, period=20),
                valid=True,
                n_bars=n,
            )

            log.debug(
                "[RegimeFeatureSource] OHLCV提取完成: bars={} ret_20={:.4f} "
                "ret_std={:.4f} adx={:.1f} rsi={:.1f} atr_pct={:.4f} vol_ratio={:.2f}",
                n, rf.return_20, rf.ret_roll_std_20,
                rf.adx, rf.rsi_14, rf.atr_pct, rf.volume_ratio,
            )
            return rf

        except Exception:
            log.exception("[RegimeFeatureSource] 特征提取失败，返回降级特征")
            return RegimeFeatures(valid=False, n_bars=n)

    def extract_from_frame(self, regime_df: pd.DataFrame) -> RegimeFeatures:
        """
        从 DataKitchen 的 regime_features 视图提取特征（热路径，复用已计算列）。

        Args:
            regime_df: DataKitchen.transform()["regime_features"] 的最后一行（或多行）

        Returns:
            RegimeFeatures
        """
        if len(regime_df) == 0:
            log.warning("[RegimeFeatureSource] regime_df 为空，返回降级特征")
            return RegimeFeatures(valid=False, n_bars=0)

        row = regime_df.iloc[-1]

        def _get(col: str, default: float) -> float:
            if col in regime_df.columns:
                v = row[col]
                return float(v) if not (isinstance(v, float) and np.isnan(v)) else default
            return default

        rf = RegimeFeatures(
            return_1=_get("close_return", 0.0),
            return_5=0.0,   # frame 视图通常没有多步累积
            return_20=0.0,
            ret_roll_mean_20=_get("ret_roll_mean_20", 0.0),
            ret_roll_std_20=max(_get("ret_roll_std_20", 0.01), 1e-8),
            price_vs_sma20=_get("price_vs_sma_20", 0.0),
            price_vs_sma50=_get("price_vs_sma_50", 0.0),
            adx=_get("adx_14", 0.0),
            rsi_14=_get("rsi_14", 50.0),
            atr_pct=_get("atr_pct_14", 0.0),
            bb_width=_get("bb_width", 0.0),
            volume_ratio=_get("volume_ratio", 1.0),
            valid=True,
            n_bars=len(regime_df),
        )

        log.debug(
            "[RegimeFeatureSource] frame提取完成: ret_mean={:.4f} adx={:.1f} rsi={:.1f}",
            rf.ret_roll_mean_20, rf.adx, rf.rsi_14,
        )
        return rf

    # ────────────────────────────────────────────────────────────
    # 技术指标计算（纯 numpy，无外部依赖）
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def _price_vs_sma(close: np.ndarray, period: int) -> float:
        if len(close) < period:
            return 0.0
        sma = float(np.mean(close[-period:]))
        if sma == 0:
            return 0.0
        return float(close[-1] / sma - 1.0)

    @staticmethod
    def _calc_rsi(close: np.ndarray, period: int = 14) -> float:
        if len(close) < period + 1:
            return 50.0
        deltas = np.diff(close[-(period + 1):])
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = float(np.mean(gains))
        avg_loss = float(np.mean(losses))
        if avg_loss < 1e-10:
            return 100.0
        rs = avg_gain / avg_loss
        return float(100.0 - 100.0 / (1.0 + rs))

    @staticmethod
    def _calc_atr_pct(
        high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
    ) -> float:
        if len(close) < period + 1:
            return 0.0
        trs = []
        for i in range(-period, 0):
            hl = high[i] - low[i]
            hc = abs(high[i] - close[i - 1])
            lc = abs(low[i] - close[i - 1])
            trs.append(max(hl, hc, lc))
        atr = float(np.mean(trs))
        ref = float(close[-1])
        return float(atr / ref) if ref > 0 else 0.0

    @staticmethod
    def _calc_adx(
        high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
    ) -> float:
        """简化版 ADX（使用 Wilder 平滑均值，但用简单均值近似避免迭代复杂度）。"""
        n = len(close)
        if n < period + 2:
            return 0.0

        window = min(period * 2, n - 1)
        h = high[-window - 1:]
        l = low[-window - 1:]
        c = close[-window - 1:]

        dm_plus = []
        dm_minus = []
        trs = []

        for i in range(1, len(c)):
            up = h[i] - h[i - 1]
            down = l[i - 1] - l[i]
            dm_plus.append(max(up, 0.0) if up > down else 0.0)
            dm_minus.append(max(down, 0.0) if down > up else 0.0)
            tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
            trs.append(tr)

        if len(trs) < period:
            return 0.0

        tr14 = float(np.mean(trs[-period:]))
        dmp14 = float(np.mean(dm_plus[-period:]))
        dmm14 = float(np.mean(dm_minus[-period:]))

        if tr14 < 1e-10:
            return 0.0

        di_plus = 100.0 * dmp14 / tr14
        di_minus = 100.0 * dmm14 / tr14
        di_sum = di_plus + di_minus

        if di_sum < 1e-10:
            return 0.0

        dx = 100.0 * abs(di_plus - di_minus) / di_sum
        return float(dx)

    @staticmethod
    def _calc_bb_width(close: np.ndarray, period: int = 20) -> float:
        if len(close) < period:
            return 0.0
        w = close[-period:]
        mid = float(np.mean(w))
        std = float(np.std(w))
        if mid == 0:
            return 0.0
        upper = mid + 2 * std
        lower = mid - 2 * std
        return float((upper - lower) / mid)

    @staticmethod
    def _calc_volume_ratio(volume: np.ndarray, period: int = 20) -> float:
        if len(volume) < period:
            return 1.0
        avg_vol = float(np.mean(volume[-period:]))
        if avg_vol < 1e-10:
            return 1.0
        return float(volume[-1] / avg_vol)
