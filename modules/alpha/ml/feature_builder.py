"""
modules/alpha/ml/feature_builder.py — ML 特征矩阵构建器

设计说明：
- 在 FeatureEngine（技术指标）基础上，生成适合 ML 模型输入的特征矩阵
- 所有特征构建严格保证"只用过去数据"，绝不引用未来信息

特征类型：
1. 技术指标特征（直接来自 FeatureEngine）
2. 滞后特征（Lag Features）：将技术指标向后平移 N 期
3. 收益率特征：过去 N 期的价格变化率
4. 滚动统计（Rolling Statistics）：N 期窗口的均值/标准差/偏度
5. 时间特征（可选）：小时/星期/月份的周期编码（注意：仅在有显著季节性时使用）

防未来函数规则：
- 所有 `.shift(n)` 只允许 n > 0（向后移位，消费历史数据）
- 滚动窗口计算使用 min_periods=window（不用不足样本的值）
- 标签（y）的 shift 是向前的（表示未来收益），但特征（X）中绝不允许

接口：
    MLFeatureBuilder(config)
    .build(df) → pd.DataFrame   带所有特征列的 DataFrame，含 NaN 行（调用方负责 dropna）
    .get_feature_names() → List[str]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from modules.alpha.features import FeatureEngine
from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class FeatureConfig:
    """特征工程配置。"""
    # 技术指标窗口
    sma_windows: List[int] = field(default_factory=lambda: [10, 20, 50])
    ema_spans: List[int] = field(default_factory=lambda: [12, 26])
    rsi_window: int = 14
    atr_window: int = 14
    bb_window: int = 20
    macd_params: tuple = (12, 26, 9)

    # 滞后特征：对哪些列生成滞后
    lag_features: List[str] = field(default_factory=lambda: [
        "close_return",    # 收盘价收益率
        "rsi_14",
        "macd_histogram",
        "volume_ratio",
        "atr_pct_14",
        "bb_pctb",
    ])
    lag_periods: List[int] = field(default_factory=lambda: [1, 2, 3, 5, 10])

    # 滚动统计窗口
    rolling_windows: List[int] = field(default_factory=lambda: [5, 10, 20])

    # 是否使用时间特征（日内季节性）
    use_time_features: bool = False


class MLFeatureBuilder:
    """
    ML 特征矩阵构建器。

    使用方法：
        builder = MLFeatureBuilder(config=FeatureConfig())
        feature_df = builder.build(ohlcv_df)
        clean_df = feature_df.dropna()   # 移除预热期 NaN 行

    Args:
        config: FeatureConfig 参数对象
    """

    def __init__(self, config: Optional[FeatureConfig] = None) -> None:
        self.config = config or FeatureConfig()
        self._feature_names: List[str] = []

    def build(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        构建完整的特征矩阵。

        Args:
            df: 原始 OHLCV DataFrame（含 timestamp, open, high, low, close, volume）

        Returns:
            附加所有特征列的 DataFrame（含 NaN，调用方负责 dropna）

        Raises:
            ValueError: 输入 DataFrame 缺少必要列
        """
        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"MLFeatureBuilder.build() 缺少必要列: {missing}")

        result = df.copy()
        cfg = self.config

        log.debug("构建特征矩阵: {} 行输入", len(result))

        # ── 1. 技术指标特征 ──────────────────────────────────────
        result = FeatureEngine.add_all(
            result,
            sma_windows=cfg.sma_windows,
            ema_spans=cfg.ema_spans,
            rsi_window=cfg.rsi_window,
            atr_window=cfg.atr_window,
            bb_window=cfg.bb_window,
            macd_params=cfg.macd_params,
        )
        # MACD histogram 重命名为与 lag_features 一致
        if "histogram" in result.columns:
            result = result.rename(columns={"histogram": "macd_histogram"})

        # ── 2. 基础收益率特征 ────────────────────────────────────
        result["close_return"] = result["close"].pct_change()
        result["open_return"] = result["open"].pct_change()
        result["hl_range"] = (result["high"] - result["low"]) / result["close"]
        result["close_to_high"] = (result["high"] - result["close"]) / result["close"]
        result["close_to_low"] = (result["close"] - result["low"]) / result["close"]

        # ── 3. 滞后特征（防未来函数：只用 shift > 0）───────────────
        for col in cfg.lag_features:
            if col not in result.columns:
                log.debug("跳过不存在的滞后特征列: {}", col)
                continue
            for lag in cfg.lag_periods:
                lag_col = f"{col}_lag{lag}"
                result[lag_col] = result[col].shift(lag)  # shift(+n) = 向后，使用历史

        # ── 4. 滚动统计特征 ──────────────────────────────────────
        for w in cfg.rolling_windows:
            col = "close_return"
            if col in result.columns:
                result[f"ret_roll_mean_{w}"] = (
                    result[col].rolling(w, min_periods=w).mean()
                )
                result[f"ret_roll_std_{w}"] = (
                    result[col].rolling(w, min_periods=w).std()
                )
                # 偏度（衡量分布不对称性，捕捉尾部风险）
                result[f"ret_roll_skew_{w}"] = (
                    result[col].rolling(w, min_periods=w).skew()
                )

        # ── 5. 均线相对位置（趋势强度特征）─────────────────────────
        for w in cfg.sma_windows:
            sma_col = f"sma_{w}"
            if sma_col in result.columns:
                result[f"price_vs_sma_{w}"] = (
                    (result["close"] - result[sma_col]) / result[sma_col]
                )

        # 均线间距（金叉/死叉强度）
        if "sma_10" in result.columns and "sma_20" in result.columns:
            result["sma_10_20_spread"] = (
                (result["sma_10"] - result["sma_20"]) / result["sma_20"]
            )

        # ── 6. 时间特征（可选，慎用）────────────────────────────────
        if cfg.use_time_features and "timestamp" in result.columns:
            ts = pd.to_datetime(result["timestamp"])
            # 使用正弦/余弦编码，保持周期性连续
            result["hour_sin"] = np.sin(2 * np.pi * ts.dt.hour / 24)
            result["hour_cos"] = np.cos(2 * np.pi * ts.dt.hour / 24)
            result["dow_sin"] = np.sin(2 * np.pi * ts.dt.dayofweek / 7)
            result["dow_cos"] = np.cos(2 * np.pi * ts.dt.dayofweek / 7)

        # 记录特征列名
        non_feature_cols = set(df.columns)  # 原始输入列不是特征
        self._feature_names = [
            c for c in result.columns
            if c not in non_feature_cols and c not in {"timestamp", "symbol"}
        ]

        log.debug(
            "特征矩阵构建完成: {} 行, {} 个特征",
            len(result),
            len(self._feature_names),
        )
        return result

    def get_feature_names(self) -> List[str]:
        """
        返回最近一次 build() 产出的所有特征列名。
        必须在 build() 之后调用。
        """
        return list(self._feature_names)

    def get_feature_matrix(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        构建并返回只含特征列的纯特征矩阵（X）。

        会自动删除含 NaN 的行（预热期）。

        Returns:
            纯特征 DataFrame（已 dropna）
        """
        full_df = self.build(df)
        X = full_df[self._feature_names].dropna()
        log.info(
            "特征矩阵（已删除 NaN）: {} 行 x {} 列",
            len(X),
            len(self._feature_names),
        )
        return X
