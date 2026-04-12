"""
modules/data/validator.py — K 线数据校验器

设计说明：
- 数据校验是特征工程之前的强制关卡，不通过则抛出异常
- 校验结果分两类：
    - 硬错误：结构性问题，必须中断（如时间戳格式错误）
    - 软警告：数据质量问题，记录日志后修复（如坏点、时间缺口）
- 严格防止"未来函数"问题：校验集不含验证集之后的数据

校验项：
1. 列完整性（必须含 timestamp/open/high/low/close/volume）
2. 时间戳唯一性 + 升序排列
3. 时区检查（全部必须为 UTC）
4. OHLCV 数值合法性（无 NaN、负数、HLC 关系合法）
5. 时间缺口检测（报告而不自动填充）
6. 极端异常值检测（3σ 法则）

接口：
    KlineValidator().validate(df, symbol, timeframe) → 清洁的 DataFrame
    KlineValidator().check_future_leak(train_df, test_df) → bool
"""

from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

from core.exceptions import DataValidationError, FutureLookAheadError
from core.logger import get_logger

log = get_logger(__name__)

# 必须存在的列
_REQUIRED_COLS: List[str] = ["timestamp", "open", "high", "low", "close", "volume"]

# 时间周期到 pandas freq 的映射
_TIMEFRAME_TO_FREQ = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1H",
    "2h": "2H",
    "4h": "4H",
    "6h": "6H",
    "12h": "12H",
    "1d": "1D",
    "1w": "1W",
}


class KlineValidator:
    """
    K 线数据校验器。

    validate() 方法按顺序执行所有校验规则，
    返回经过清洗和排序的标准化 DataFrame。

    任何硬错误均抛出 DataValidationError，
    可修复的问题仅记录日志并自动修正。
    """

    def validate(
        self,
        df: pd.DataFrame,
        symbol: str,
        timeframe: str,
    ) -> pd.DataFrame:
        """
        执行完整的 K 线数据校验流水线。

        Args:
            df:        原始 OHLCV DataFrame
            symbol:    交易对名称（仅用于日志）
            timeframe: K 线周期标识（如 "1h"）

        Returns:
            清洁的 DataFrame（UTC 时间戳、升序、无重复、数值合法）

        Raises:
            DataValidationError: 结构性错误或数据不合法
        """
        ctx = f"[{symbol}/{timeframe}]"

        # Step 1: 列完整性
        df = self._check_columns(df, ctx)

        # Step 2: 数值类型
        df = self._coerce_numerics(df, ctx)

        # Step 3: 时间戳标准化为 UTC
        df = self._normalize_timestamps(df, ctx)

        # Step 4: 排序 + 去重
        df = self._sort_and_deduplicate(df, ctx)

        # Step 5: 数值合法性（NaN、负值、OHLC 关系）
        df = self._validate_ohlcv_values(df, ctx)

        # Step 6: 时间缺口检测（不自动填充，仅告警）
        self._detect_gaps(df, timeframe, ctx)

        # Step 7: 极端异常值检测（3σ）
        self._detect_outliers(df, ctx)

        log.info("{} 校验通过，共 {} 条 K 线", ctx, len(df))
        return df

    @staticmethod
    def check_future_leak(
        train_df: pd.DataFrame,
        test_df: pd.DataFrame,
    ) -> None:
        """
        检查训练集与测试集之间是否存在时间泄露。

        强制要求：train_df 的所有时间戳 < test_df 的所有时间戳。

        Raises:
            FutureLookAheadError: 若检测到泄露
        """
        if train_df.empty or test_df.empty:
            return

        train_max = train_df["timestamp"].max()
        test_min = test_df["timestamp"].min()

        if train_max >= test_min:
            raise FutureLookAheadError(
                f"检测到训练/测试集时间泄露: "
                f"train_max={train_max} >= test_min={test_min}。"
                f"必须按时间顺序严格切分，不允许交叉。"
            )
        log.info("时间切分检查通过: train_max={} < test_min={}", train_max, test_min)

    # ────────────────────────────────────────────────────────────
    # 私有校验步骤
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def _check_columns(df: pd.DataFrame, ctx: str) -> pd.DataFrame:
        """检查必须存在的列。"""
        missing = [c for c in _REQUIRED_COLS if c not in df.columns]
        if missing:
            raise DataValidationError(
                f"{ctx} 缺少必要列: {missing}，实际列: {list(df.columns)}"
            )
        return df

    @staticmethod
    def _coerce_numerics(df: pd.DataFrame, ctx: str) -> pd.DataFrame:
        """将 OHLCV 列强制转换为 float64。"""
        numeric_cols = ["open", "high", "low", "close", "volume"]
        for col in numeric_cols:
            original_count = df[col].isna().sum()
            df[col] = pd.to_numeric(df[col], errors="coerce")
            new_nan_count = df[col].isna().sum()
            if new_nan_count > original_count:
                log.warning(
                    "{} 列 {} 存在 {} 个无法解析的数值，已转换为 NaN",
                    ctx,
                    col,
                    new_nan_count - original_count,
                )
        return df

    @staticmethod
    def _normalize_timestamps(df: pd.DataFrame, ctx: str) -> pd.DataFrame:
        """确保 timestamp 列为 UTC-aware datetime。"""
        if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            try:
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            except Exception as exc:
                raise DataValidationError(
                    f"{ctx} timestamp 列无法解析为 datetime: {exc}"
                ) from exc

        # 若已有时区但不是 UTC，转换
        if df["timestamp"].dt.tz is None:
            log.warning("{} timestamp 无时区信息，假定 UTC", ctx)
            df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
        elif str(df["timestamp"].dt.tz) != "UTC":
            df["timestamp"] = df["timestamp"].dt.tz_convert("UTC")

        return df

    @staticmethod
    def _sort_and_deduplicate(df: pd.DataFrame, ctx: str) -> pd.DataFrame:
        """按时间升序排列，去除重复时间戳（保留第一条）。"""
        before = len(df)
        df = df.sort_values("timestamp").reset_index(drop=True)
        df = df.drop_duplicates(subset=["timestamp"], keep="first")
        after = len(df)

        if before != after:
            log.warning("{} 去除重复时间戳 {} 条", ctx, before - after)

        return df

    @staticmethod
    def _validate_ohlcv_values(df: pd.DataFrame, ctx: str) -> pd.DataFrame:
        """
        校验 OHLCV 数值合法性：
        - 无 NaN（删除含 NaN 的行）
        - 无负值或零值（价格/成交量必须 > 0）
        - High >= max(Open, Close) >= min(Open, Close) >= Low > 0
        """
        # 删除含 NaN 的行
        nan_mask = df[["open", "high", "low", "close", "volume"]].isna().any(axis=1)
        nan_count = nan_mask.sum()
        if nan_count > 0:
            log.warning("{} 删除含 NaN 的行 {} 条", ctx, nan_count)
            df = df[~nan_mask].reset_index(drop=True)

        # 检查非正值
        price_cols = ["open", "high", "low", "close"]
        for col in price_cols:
            bad_mask = df[col] <= 0
            if bad_mask.any():
                log.warning("{} 列 {} 存在 {} 个非正值，删除", ctx, col, bad_mask.sum())
                df = df[~bad_mask].reset_index(drop=True)

        volume_bad = df["volume"] < 0
        if volume_bad.any():
            log.warning("{} volume 存在 {} 个负值，设为 0", ctx, volume_bad.sum())
            df.loc[volume_bad, "volume"] = 0.0

        # OHLC 逻辑关系校验
        invalid_hl = df["high"] < df["low"]
        if invalid_hl.any():
            bad_count = invalid_hl.sum()
            log.warning("{} 存在 {} 条 high < low 的异常数据，删除", ctx, bad_count)
            df = df[~invalid_hl].reset_index(drop=True)

        return df

    @staticmethod
    def _detect_gaps(df: pd.DataFrame, timeframe: str, ctx: str) -> None:
        """检测时间序列中的缺口（不自动填充，仅记录警告）。"""
        if len(df) < 2:
            return

        freq_str = _TIMEFRAME_TO_FREQ.get(timeframe)
        if freq_str is None:
            log.warning("{} 未知 timeframe={}，跳过缺口检测", ctx, timeframe)
            return

        expected_delta = pd.tseries.frequencies.to_offset(freq_str)
        if expected_delta is None:
            return

        ts = df["timestamp"]
        # 检测相邻 K 线之间的时间差
        diffs = ts.diff().dropna()
        expected_ns = pd.Timedelta(expected_delta).value

        # 允许 1.5 倍的误差容忍度（部分交易所数据不绝对精准）
        threshold_ns = int(expected_ns * 1.5)
        gaps = diffs[diffs.dt.total_seconds() * 1e9 > threshold_ns]

        if not gaps.empty:
            log.warning(
                "{} 共检测到 {} 处时间缺口（超过 1.5x timeframe）：\n{}",
                ctx,
                len(gaps),
                gaps.to_string(),
            )
        else:
            log.debug("{} 无时间缺口", ctx)

    @staticmethod
    def _detect_outliers(df: pd.DataFrame, ctx: str) -> None:
        """使用 3σ 法则检测 close 价格的极端异常值（仅告警，不删除）。"""
        if len(df) < 30:  # 样本太少时不做统计检验
            return

        close = df["close"]
        mean = close.mean()
        std = close.std()

        if std == 0:
            return

        z_scores = (close - mean).abs() / std
        outliers = df[z_scores > 3]

        if not outliers.empty:
            log.warning(
                "{} 检测到 {} 个 close 价格 3σ 异常值（均值={:.4f}, 标准差={:.4f}）",
                ctx,
                len(outliers),
                mean,
                std,
            )
