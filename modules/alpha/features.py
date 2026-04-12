"""
modules/alpha/features.py — 特征工程引擎

设计说明：
- 所有技术指标计算集中于此，策略层复用而不重复实现
- 所有计算函数均为纯函数（无副作用），便于测试和复用
- 严格防止未来函数：所有计算只使用 index 及之前的数据
- 参数化设计：所有窗口/周期参数显式传入，不使用全局默认值

指标库（当前阶段）：
- SMA（简单移动平均）
- EMA（指数移动平均）
- RSI（相对强弱指标）
- ATR（真实波幅）
- Bollinger Bands（布林带）
- MACD（移动平均收敛散度）
- Volume MA（成交量均线）

防未来函数规则：
- 所有 rolling() 调用默认 min_periods=window（不用不足样本凑数）
- 所有 shift() 向后移位，禁止向前移位（负索引 shift）
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class FeatureEngine:
    """
    技术指标计算工具类（全部为静态方法）。

    用法：
        features = FeatureEngine.add_all(df, config={...})
        或单独调用：
        sma = FeatureEngine.sma(df["close"], window=20)
    """

    # ────────────────────────────────────────────────────────────
    # 趋势类指标
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def sma(series: pd.Series, window: int) -> pd.Series:
        """
        简单移动平均（SMA）。

        Args:
            series: 价格序列（通常为 close）
            window: 计算窗口

        Returns:
            SMA 序列，前 (window-1) 条为 NaN
        """
        return series.rolling(window=window, min_periods=window).mean()

    @staticmethod
    def ema(series: pd.Series, span: int) -> pd.Series:
        """
        指数移动平均（EMA），使用调整后的 Wilder 公式。

        Args:
            series: 价格序列
            span:   EMA 跨度（等价于传统的 N 日 EMA）

        Returns:
            EMA 序列
        """
        return series.ewm(span=span, adjust=False).mean()

    @staticmethod
    def macd(
        series: pd.Series,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> pd.DataFrame:
        """
        MACD 指标。

        Returns:
            DataFrame，列：[macd_line, signal_line, histogram]
        """
        ema_fast = FeatureEngine.ema(series, fast)
        ema_slow = FeatureEngine.ema(series, slow)
        macd_line = ema_fast - ema_slow
        signal_line = FeatureEngine.ema(macd_line, signal)
        histogram = macd_line - signal_line

        return pd.DataFrame({
            "macd_line": macd_line,
            "signal_line": signal_line,
            "histogram": histogram,
        })

    # ────────────────────────────────────────────────────────────
    # 震荡类指标
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def rsi(series: pd.Series, window: int = 14) -> pd.Series:
        """
        相对强弱指标（RSI）。Wilder 平滑法。

        Args:
            series: close 价格序列
            window: 计算窗口（默认 14）

        Returns:
            RSI 序列，值域 [0, 100]
        """
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)

        # 使用 Wilder 平滑（等价于 RMA，alpha = 1/window）
        avg_gain = gain.ewm(alpha=1 / window, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / window, adjust=False).mean()

        rs = avg_gain / avg_loss.replace(0, float("nan"))
        return 100 - (100 / (1 + rs))

    @staticmethod
    def bollinger_bands(
        series: pd.Series,
        window: int = 20,
        num_std: float = 2.0,
    ) -> pd.DataFrame:
        """
        布林带（Bollinger Bands）。

        Returns:
            DataFrame，列：[bb_mid, bb_upper, bb_lower, bb_pctb, bb_width]
            - bb_pctb:  %B 指标（当前价格在布林带中的位置，0~1 为正常范围）
            - bb_width: 带宽（(upper - lower) / mid，衡量波动率）
        """
        mid = FeatureEngine.sma(series, window)
        std = series.rolling(window=window, min_periods=window).std()

        upper = mid + num_std * std
        lower = mid - num_std * std
        pctb = (series - lower) / (upper - lower).replace(0, float("nan"))
        width = (upper - lower) / mid.replace(0, float("nan"))

        return pd.DataFrame({
            "bb_mid": mid,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_pctb": pctb,
            "bb_width": width,
        })

    # ────────────────────────────────────────────────────────────
    # 波动率类指标
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
        """
        平均真实波幅（ATR）。

        Args:
            df:     必须含 high, low, close 列
            window: 计算窗口（默认 14）

        Returns:
            ATR 序列（绝对价格单位）
        """
        high = df["high"]
        low = df["low"]
        prev_close = df["close"].shift(1)

        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()

        true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return true_range.ewm(alpha=1 / window, adjust=False).mean()

    @staticmethod
    def atr_pct(df: pd.DataFrame, window: int = 14) -> pd.Series:
        """
        ATR 百分比（相对于 close 的归一化，便于跨币种比较）。
        """
        return FeatureEngine.atr(df, window) / df["close"]

    # ────────────────────────────────────────────────────────────
    # 成交量类指标
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def volume_ma(df: pd.DataFrame, window: int = 20) -> pd.Series:
        """成交量移动平均。"""
        return FeatureEngine.sma(df["volume"], window)

    @staticmethod
    def volume_ratio(df: pd.DataFrame, window: int = 20) -> pd.Series:
        """
        当前成交量 / 均量比（>1 为放量，<1 为缩量）。
        """
        vma = FeatureEngine.volume_ma(df, window)
        return df["volume"] / vma.replace(0, float("nan"))

    # ────────────────────────────────────────────────────────────
    # 动量类指标
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def roc(series: pd.Series, window: int = 10) -> pd.Series:
        """
        价格变化率（Rate of Change）。

        Returns:
            ROC 序列（百分比形式）
        """
        return (series / series.shift(window) - 1) * 100

    @staticmethod
    def momentum(series: pd.Series, window: int = 10) -> pd.Series:
        """N 期价格动量（当前价格 - N 期前价格）。"""
        return series - series.shift(window)

    # ────────────────────────────────────────────────────────────
    # 批量计算（一次性生成所有特征）
    # ────────────────────────────────────────────────────────────

    @classmethod
    def add_all(
        cls,
        df: pd.DataFrame,
        sma_windows: list = None,
        ema_spans: list = None,
        rsi_window: int = 14,
        atr_window: int = 14,
        bb_window: int = 20,
        macd_params: tuple = (12, 26, 9),
    ) -> pd.DataFrame:
        """
        批量计算所有技术指标，直接附加到传入 DataFrame 的新列中。

        重要：返回带有附加列的 DataFrame 副本，不修改原始数据。

        Args:
            df:            原始 OHLCV DataFrame（含 timestamp, open, high, low, close, volume）
            sma_windows:   SMA 窗口列表，默认 [20, 50, 200]
            ema_spans:     EMA 跨度列表，默认 [12, 26]
            rsi_window:    RSI 窗口
            atr_window:    ATR 窗口
            bb_window:     布林带窗口
            macd_params:   (fast, slow, signal) 元组

        Returns:
            含所有指标列的 DataFrame 副本
        """
        if sma_windows is None:
            sma_windows = [20, 50, 200]
        if ema_spans is None:
            ema_spans = [12, 26]

        result = df.copy()
        close = result["close"]

        # SMA
        for w in sma_windows:
            result[f"sma_{w}"] = cls.sma(close, w)

        # EMA
        for span in ema_spans:
            result[f"ema_{span}"] = cls.ema(close, span)

        # RSI
        result[f"rsi_{rsi_window}"] = cls.rsi(close, rsi_window)

        # ATR
        result[f"atr_{atr_window}"] = cls.atr(result, atr_window)
        result[f"atr_pct_{atr_window}"] = cls.atr_pct(result, atr_window)

        # Bollinger Bands
        bb = cls.bollinger_bands(close, bb_window)
        for col in bb.columns:
            result[col] = bb[col]

        # MACD
        fast, slow, signal = macd_params
        macd = cls.macd(close, fast, slow, signal)
        for col in macd.columns:
            result[col] = macd[col]

        # 成交量
        result["volume_ma_20"] = cls.volume_ma(result, 20)
        result["volume_ratio"] = cls.volume_ratio(result, 20)

        # 动量
        result["roc_10"] = cls.roc(close, 10)
        result["momentum_10"] = cls.momentum(close, 10)

        return result
