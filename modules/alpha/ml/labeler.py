"""
modules/alpha/ml/labeler.py — 前向收益率标签生成器

这是整个 ML 管线中最危险的环节。
未来函数泄露最常见于此模块，特别是：
- 用 shift(-n) 计算未来收益时边界处理不当
- 训练集/测试集划分时标签泄露到训练窗口

设计原则（严格执行）：
- 只用 `shift(-n)` 计算标签（向前看 n 期），这是正确的标签计算方式
- 但标签只在拥有"完整 n 期未来数据"的行上有效
- 最后 n 行的标签必须强制设为 NaN（因为没有足够的未来数据）
- 在 Walk-Forward 训练时，训练窗口的最后 n 行标签必须丢弃
  （这是 "embargo period" 概念的核心）

标签类型：
1. 连续标签：未来 n 期的价格变化率（用于回归任务）
2. 分类标签：
   - 三类：-1（卖出）/ 0（持有）/ 1（买入）
   - 二类：0（不买）/ 1（买入）
   阈值：
   - 若未来收益率 > thresh → 1（做多信号）
   - 若未来收益率 < -thresh → -1（做空信号，现货忽略此类）
   - 否则 → 0（观望）

接口：
    ReturnLabeler(forward_bars, return_threshold)
    .label_continuous(df)      → Series（连续收益率，最后 n 行为 NaN）
    .label_classification(df)  → Series（-1/0/1 分类，最后 n 行为 NaN）
    .label_binary(df)           → Series（0/1 分类，现货多头专用）
    .check_no_leak(df, train_idx, test_idx) → None（有泄露则抛出异常）
"""

from __future__ import annotations

import warnings
from typing import Optional

import numpy as np
import pandas as pd

from core.exceptions import FutureLookAheadError
from core.logger import get_logger

log = get_logger(__name__)


class ReturnLabeler:
    """
    前向收益率标签生成器。

    所有标签方法均在最后 `forward_bars` 行强制写入 NaN，
    保证训练集不含"没有完整未来数据"的样本。

    Args:
        forward_bars:     向前看多少根 K 线（持仓周期）
        return_threshold: 三分类标签的收益率阈值（如 0.01 = 1%）
        use_log_return:   是否使用对数收益率（更适合统计建模，默认 True）
    """

    def __init__(
        self,
        forward_bars: int = 5,
        return_threshold: float = 0.005,
        use_log_return: bool = True,
    ) -> None:
        if forward_bars <= 0:
            raise ValueError(f"forward_bars 必须 > 0，实际: {forward_bars}")
        if return_threshold <= 0:
            raise ValueError(f"return_threshold 必须 > 0，实际: {return_threshold}")

        self.forward_bars = forward_bars
        self.return_threshold = return_threshold
        self.use_log_return = use_log_return

    # ────────────────────────────────────────────────────────────
    # 标签生成方法
    # ────────────────────────────────────────────────────────────

    def label_continuous(self, df: pd.DataFrame) -> pd.Series:
        """
        生成连续收益率标签（未来 N 期持有收益）。

        y[i] = (close[i + N] - close[i]) / close[i]   （简单收益率）
        或    y[i] = log(close[i + N] / close[i])      （对数收益率）

        重要：
        - 使用 shift(-N) 计算未来值（这是正确用法）
        - 最后 N 行强制为 NaN（无完整未来数据）

        Returns:
            Series，index 与 df 一致，最后 N 行为 NaN
        """
        close = df["close"].astype(float)
        n = self.forward_bars

        # 向前看 N 期的收盘价（shift(-n) 意味着"未来第 n 根 K 线的值"）
        future_close = close.shift(-n)

        if self.use_log_return:
            label = np.log(future_close / close)
        else:
            label = (future_close - close) / close

        # 强制将最后 N 行设为 NaN（这些行没有完整 N 期的未来数据）
        label.iloc[-n:] = np.nan

        log.debug(
            "连续标签生成完成: {} 有效样本 / {} 总行数 (最后 {} 行 NaN)",
            label.notna().sum(),
            len(label),
            n,
        )
        return label

    def label_classification(self, df: pd.DataFrame) -> pd.Series:
        """
        三分类标签：1（买入）/ 0（持仓观望）/ -1（卖出，现货忽略）。

        阈值规则：
        - forward_return > threshold  → 1
        - forward_return < -threshold → -1
        - 否则                        → 0

        Returns:
            Series（-1/0/1），最后 N 行为 NaN
        """
        cont = self.label_continuous(df)

        def classify(r: float) -> Optional[float]:
            if pd.isna(r):
                return np.nan
            if r > self.return_threshold:
                return 1.0
            elif r < -self.return_threshold:
                return -1.0
            return 0.0

        label = cont.map(classify)

        n_buy = (label == 1).sum()
        n_sell = (label == -1).sum()
        n_hold = (label == 0).sum()
        log.info(
            "分类标签分布: 买入={} 卖出={} 观望={} NaN={}",
            n_buy, n_sell, n_hold, label.isna().sum(),
        )
        return label

    def label_binary(self, df: pd.DataFrame) -> pd.Series:
        """
        二分类标签（现货多头专用）：1（未来涨超阈值）/ 0（其他）。

        注意：
        - 将 -1（看跌）归入 0 类（因为现货不做空）
        - 这意味着 0 类包含"小幅上涨/横盘/下跌"三种情况
        - 模型学习"超额强势上涨"的特征，而非"普通上涨"

        Returns:
            Series（0/1），最后 N 行为 NaN
        """
        three_class = self.label_classification(df)
        # -1 → 0，1 不变，0 不变
        return (three_class == 1).astype(float).where(three_class.notna(), other=np.nan)

    # ────────────────────────────────────────────────────────────
    # 时序切分验证（核心防泄露工具）
    # ────────────────────────────────────────────────────────────

    def check_no_leak(
        self,
        train_idx: pd.Index,
        test_idx: pd.Index,
        embargo_bars: Optional[int] = None,
    ) -> None:
        """
        验证训练集和测试集之间没有时序泄露。

        规则（从严到宽）：
        1. 训练集最大时间戳 < 测试集最小时间戳（基本时序隔离）
        2. 两者之间需要 embargo_bars 个 K 线的隔离期
           （防止"训练集末尾的标签计算使用了测试集数据"）

        Args:
            train_idx:     训练集时间戳索引
            test_idx:      测试集时间戳索引
            embargo_bars:  隔离期长度（默认使用 self.forward_bars）

        Raises:
            FutureLookAheadError: 检测到泄露风险
        """
        if len(train_idx) == 0 or len(test_idx) == 0:
            return

        embargo = embargo_bars if embargo_bars is not None else self.forward_bars

        # 获取时间戳（允许 DatetimeIndex 或 RangeIndex 两种情况）
        train_max = train_idx.max()
        test_min = test_idx.min()

        if train_max >= test_min:
            raise FutureLookAheadError(
                f"时序泄露！训练集最大时间 ({train_max}) >= "
                f"测试集最小时间 ({test_min})。"
                f"请确保训练集在测试集之前。"
            )

        # 检查 embargo 隔离期（适用于整数索引）
        if hasattr(train_idx, "dtype") and np.issubdtype(train_idx.dtype, np.integer):
            gap = int(test_min) - int(train_max) - 1
            if gap < embargo:
                raise FutureLookAheadError(
                    f"隔离期不足！训练集末尾与测试集之间只有 {gap} 行，"
                    f"需要至少 {embargo} 行的 embargo 隔离期。"
                    f"这是因为标签计算向前看 {self.forward_bars} 根 K 线，"
                    f"训练集末尾的标签使用了测试集范围内的数据。"
                )

        log.info(
            "时序切分检查通过: train_max={} test_min={} embargo={}",
            train_max, test_min, embargo,
        )

    def compute_class_weights(self, labels: pd.Series) -> dict:
        """
        计算类别权重（用于处理不平衡数据集）。

        Returns:
            {class_value: weight} 字典，稀有类权重更大
        """
        valid = labels.dropna()
        if len(valid) == 0:
            return {}

        counts = valid.value_counts()
        total = len(valid)
        weights = {cls: total / (len(counts) * cnt) for cls, cnt in counts.items()}
        log.info("类别权重: {}", weights)
        return weights
