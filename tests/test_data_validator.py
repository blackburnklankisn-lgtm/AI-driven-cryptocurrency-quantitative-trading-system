"""
tests/test_data_validator.py — 数据校验器单元测试

覆盖项：
- 缺少必要列时抛出 DataValidationError
- 时间戳正确标准化为 UTC
- 重复时间戳被去除
- NaN 行被删除
- 负价格行被删除
- high < low 的异常行被删除
- 时间缺口正确检测（只告警，不崩溃）
- check_future_leak 正确检测交叉切分
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from core.exceptions import DataValidationError, FutureLookAheadError
from modules.data.validator import KlineValidator


def make_valid_df(n: int = 10, freq: str = "1h", start: str = "2024-01-01") -> pd.DataFrame:
    """生成 n 条合法的 OHLCV 数据（UTC 时间戳）。"""
    timestamps = pd.date_range(start=start, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({
        "timestamp": timestamps,
        "open":   [100.0 + i for i in range(n)],
        "high":   [105.0 + i for i in range(n)],
        "low":    [95.0 + i for i in range(n)],
        "close":  [102.0 + i for i in range(n)],
        "volume": [1000.0 + i * 10 for i in range(n)],
        "symbol": "BTC/USDT",
    })


validator = KlineValidator()


class TestColumnCheck:
    def test_missing_column_raises(self) -> None:
        """缺少必要列应抛出 DataValidationError。"""
        df = make_valid_df().drop(columns=["close"])
        with pytest.raises(DataValidationError, match="缺少必要列"):
            validator.validate(df, "BTC/USDT", "1h")

    def test_all_columns_present_passes(self) -> None:
        """包含所有必要列时应正常通过。"""
        df = make_valid_df()
        result = validator.validate(df, "BTC/USDT", "1h")
        assert "timestamp" in result.columns
        assert "close" in result.columns


class TestTimestampNormalization:
    def test_utc_timestamps_preserved(self) -> None:
        """UTC 时间戳应保持不变。"""
        df = make_valid_df()
        result = validator.validate(df, "BTC/USDT", "1h")
        assert result["timestamp"].dt.tz is not None
        assert str(result["timestamp"].dt.tz) == "UTC"

    def test_naive_timestamps_localized(self) -> None:
        """无时区时间戳应被自动本地化为 UTC。"""
        df = make_valid_df()
        df["timestamp"] = df["timestamp"].dt.tz_localize(None)  # 去掉时区
        result = validator.validate(df, "BTC/USDT", "1h")
        assert str(result["timestamp"].dt.tz) == "UTC"

    def test_non_utc_timezone_converted(self) -> None:
        """非 UTC 时区应被转换为 UTC。"""
        df = make_valid_df()
        df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Shanghai")
        result = validator.validate(df, "BTC/USDT", "1h")
        assert str(result["timestamp"].dt.tz) == "UTC"


class TestDeduplication:
    def test_duplicate_timestamps_removed(self) -> None:
        """重复时间戳应被去除。"""
        df = make_valid_df(n=5)
        # 人为重复第 3 行
        df_dup = pd.concat([df, df.iloc[[2]]], ignore_index=True)
        result = validator.validate(df_dup, "BTC/USDT", "1h")
        assert len(result) == 5
        assert result["timestamp"].is_unique

    def test_sorted_after_dedup(self) -> None:
        """去重后应按时间升序排列。"""
        df = make_valid_df(n=5)
        df_shuffled = df.sample(frac=1, random_state=42).reset_index(drop=True)
        result = validator.validate(df_shuffled, "BTC/USDT", "1h")
        assert result["timestamp"].is_monotonic_increasing


class TestOHLCVValidation:
    def test_nan_rows_dropped(self) -> None:
        """含 NaN 的行应被删除。"""
        df = make_valid_df(n=5)
        df.loc[2, "close"] = float("nan")
        result = validator.validate(df, "BTC/USDT", "1h")
        assert len(result) == 4

    def test_negative_price_rows_dropped(self) -> None:
        """负价格行应被删除。"""
        df = make_valid_df(n=5)
        df.loc[1, "open"] = -1.0
        result = validator.validate(df, "BTC/USDT", "1h")
        assert len(result) == 4

    def test_zero_price_rows_dropped(self) -> None:
        """零价格行应被删除。"""
        df = make_valid_df(n=5)
        df.loc[3, "low"] = 0.0
        result = validator.validate(df, "BTC/USDT", "1h")
        assert len(result) == 4

    def test_high_less_than_low_dropped(self) -> None:
        """high < low 的异常行应被删除。"""
        df = make_valid_df(n=5)
        df.loc[0, "high"] = 50.0  # high < low (95)
        result = validator.validate(df, "BTC/USDT", "1h")
        assert len(result) == 4

    def test_negative_volume_set_to_zero(self) -> None:
        """负成交量应被修正为 0（而不是删除该行）。"""
        df = make_valid_df(n=5)
        df.loc[1, "volume"] = -100.0
        result = validator.validate(df, "BTC/USDT", "1h")
        assert len(result) == 5  # 行不删除
        assert result.loc[result.index[1], "volume"] == 0.0


class TestGapDetection:
    def test_missing_bar_detected_but_not_crash(self) -> None:
        """时间缺口应记录警告，不抛出异常，不自动填充。"""
        df = make_valid_df(n=10)
        # 删除第 5 行制造缺口
        df_gap = pd.concat([df.iloc[:5], df.iloc[6:]], ignore_index=True)
        # 不应抛出异常
        result = validator.validate(df_gap, "BTC/USDT", "1h")
        # 不自动填充，行数应为 9
        assert len(result) == 9


class TestFutureLeak:
    def test_no_leak_passes(self) -> None:
        """时间严格切分时不应抛出异常。"""
        train = make_valid_df(n=10, start="2024-01-01")
        test = make_valid_df(n=5, start="2024-01-11")
        KlineValidator.check_future_leak(train, test)  # 不抛出

    def test_overlapping_splits_raises(self) -> None:
        """训练集时间戳覆盖测试集时，应抛出 FutureLookAheadError。"""
        # 使用日频确保 10 条数据覆盖 10 天（Jan 1-10）
        train = make_valid_df(n=10, freq="1d", start="2024-01-01")
        test = make_valid_df(n=5, freq="1d", start="2024-01-05")  # 与 train 有重叠
        with pytest.raises(FutureLookAheadError):
            KlineValidator.check_future_leak(train, test)

    def test_empty_dfs_no_error(self) -> None:
        """空 DataFrame 时不应报错。"""
        empty = pd.DataFrame(columns=["timestamp", "close"])
        KlineValidator.check_future_leak(empty, empty)  # 不抛出
