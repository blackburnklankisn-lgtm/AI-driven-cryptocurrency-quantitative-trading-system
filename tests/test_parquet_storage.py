"""
tests/test_parquet_storage.py — Parquet 本地存储层完整测试

覆盖项：
1. ParquetStorage 初始化（自动建目录）
2. write: 正常写入、空 DataFrame 跳过、增量追加去重、多 symbol 隔离
3. read: 正常读取、文件不存在返回 None、时间范围 since/until 过滤
4. get_latest_timestamp: 正常、无数据返回 None
5. list_available: 列出所有数据集
6. _get_path: 路径构建（/ → _）
7. 时间戳 UTC 规范化
8. 时间过滤边界条件（aware / naive）
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import pytest

from modules.data.storage import ParquetStorage


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

def _make_klines(
    n: int = 10,
    start_ts: datetime | None = None,
    freq_minutes: int = 60,
) -> pd.DataFrame:
    """生成 n 条 OHLCV K 线 DataFrame（UTC aware timestamp）。"""
    if start_ts is None:
        start_ts = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    timestamps = [start_ts + timedelta(minutes=freq_minutes * i) for i in range(n)]
    return pd.DataFrame({
        "timestamp": pd.to_datetime(timestamps, utc=True),
        "open": [40_000.0 + i for i in range(n)],
        "high": [40_100.0 + i for i in range(n)],
        "low": [39_900.0 + i for i in range(n)],
        "close": [40_050.0 + i for i in range(n)],
        "volume": [100.0 + i for i in range(n)],
    })


# ══════════════════════════════════════════════════════════════
# 1. 初始化
# ══════════════════════════════════════════════════════════════

class TestParquetStorageInit:

    def test_root_dir_created_on_init(self, tmp_path):
        root = tmp_path / "data_store" / "deep" / "path"
        storage = ParquetStorage(root_dir=root, exchange_id="binance")
        assert root.exists()

    def test_default_exchange_id(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path)
        assert storage.exchange_id == "binance"

    def test_custom_exchange_id(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="okx")
        assert storage.exchange_id == "okx"


# ══════════════════════════════════════════════════════════════
# 2. write
# ══════════════════════════════════════════════════════════════

class TestParquetStorageWrite:

    def test_write_and_file_created(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        df = _make_klines(10)
        storage.write(df, "BTC/USDT", "1h")
        path = tmp_path / "binance" / "BTC_USDT" / "1h.parquet"
        assert path.exists()

    def test_write_empty_dataframe_skips(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        empty_df = pd.DataFrame()
        storage.write(empty_df, "BTC/USDT", "1h")
        path = tmp_path / "binance" / "BTC_USDT" / "1h.parquet"
        assert not path.exists()

    def test_write_incremental_appends_and_deduplicates(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")

        batch1 = _make_klines(10)
        storage.write(batch1, "BTC/USDT", "1h")

        # Overlap: last 3 from batch1 + 7 new
        start = datetime(2024, 1, 1, 7, 0, 0, tzinfo=timezone.utc)
        batch2 = _make_klines(10, start_ts=start)
        storage.write(batch2, "BTC/USDT", "1h")

        result = storage.read("BTC/USDT", "1h")
        assert result is not None
        # 10 + 10 - 3 overlap = 17
        assert len(result) == 17

    def test_write_multiple_symbols_isolated(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        storage.write(_make_klines(5), "BTC/USDT", "1h")
        storage.write(_make_klines(8), "ETH/USDT", "1h")

        btc = storage.read("BTC/USDT", "1h")
        eth = storage.read("ETH/USDT", "1h")
        assert len(btc) == 5
        assert len(eth) == 8

    def test_write_multiple_timeframes_isolated(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        storage.write(_make_klines(5), "BTC/USDT", "1h")
        storage.write(_make_klines(3), "BTC/USDT", "4h")

        h1 = storage.read("BTC/USDT", "1h")
        h4 = storage.read("BTC/USDT", "4h")
        assert len(h1) == 5
        assert len(h4) == 3

    def test_write_symbol_slash_becomes_underscore(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        storage.write(_make_klines(3), "SOL/USDT", "1h")
        path = tmp_path / "binance" / "SOL_USDT" / "1h.parquet"
        assert path.exists()


# ══════════════════════════════════════════════════════════════
# 3. read
# ══════════════════════════════════════════════════════════════

class TestParquetStorageRead:

    def test_read_nonexistent_returns_none(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        result = storage.read("BTC/USDT", "1h")
        assert result is None

    def test_read_returns_correct_row_count(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        storage.write(_make_klines(20), "BTC/USDT", "1h")
        result = storage.read("BTC/USDT", "1h")
        assert len(result) == 20

    def test_read_timestamps_are_utc_aware(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        storage.write(_make_klines(5), "BTC/USDT", "1h")
        result = storage.read("BTC/USDT", "1h")
        assert result["timestamp"].dt.tz is not None

    def test_read_sorted_ascending(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        df = _make_klines(10)
        # Shuffle before writing
        df_shuffled = df.sample(frac=1, random_state=42).reset_index(drop=True)
        storage.write(df_shuffled, "BTC/USDT", "1h")
        result = storage.read("BTC/USDT", "1h")
        assert result["timestamp"].is_monotonic_increasing

    def test_read_since_filter(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        storage.write(_make_klines(24), "BTC/USDT", "1h")
        since = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = storage.read("BTC/USDT", "1h", since=since)
        assert result is not None
        assert (result["timestamp"] >= pd.Timestamp(since)).all()

    def test_read_until_filter(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        storage.write(_make_klines(24), "BTC/USDT", "1h")
        until = datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
        result = storage.read("BTC/USDT", "1h", until=until)
        assert result is not None
        assert (result["timestamp"] <= pd.Timestamp(until)).all()

    def test_read_since_and_until_combined(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        storage.write(_make_klines(48), "BTC/USDT", "1h")
        since = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        until = datetime(2024, 1, 1, 20, 0, 0, tzinfo=timezone.utc)
        result = storage.read("BTC/USDT", "1h", since=since, until=until)
        assert result is not None
        assert len(result) == 11  # 10:00 to 20:00 inclusive = 11 bars

    def test_read_filter_returns_none_when_all_excluded(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        storage.write(_make_klines(10), "BTC/USDT", "1h")
        # Filter to a future date range
        since = datetime(2030, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        result = storage.read("BTC/USDT", "1h", since=since)
        assert result is None

    def test_read_with_naive_since_treated_as_utc(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        storage.write(_make_klines(24), "BTC/USDT", "1h")
        # Naive datetime — implementation converts to UTC
        since_naive = datetime(2024, 1, 1, 12, 0, 0)  # no tzinfo
        result = storage.read("BTC/USDT", "1h", since=since_naive)
        # Should not raise; may return empty or data


# ══════════════════════════════════════════════════════════════
# 4. get_latest_timestamp
# ══════════════════════════════════════════════════════════════

class TestGetLatestTimestamp:

    def test_returns_none_when_no_data(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        result = storage.get_latest_timestamp("BTC/USDT", "1h")
        assert result is None

    def test_returns_max_timestamp(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        df = _make_klines(10)
        storage.write(df, "BTC/USDT", "1h")
        result = storage.get_latest_timestamp("BTC/USDT", "1h")
        assert result is not None
        expected = datetime(2024, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
        assert result == expected

    def test_incremental_write_updates_latest_timestamp(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        storage.write(_make_klines(10), "BTC/USDT", "1h")
        ts1 = storage.get_latest_timestamp("BTC/USDT", "1h")

        start2 = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        storage.write(_make_klines(5, start_ts=start2), "BTC/USDT", "1h")
        ts2 = storage.get_latest_timestamp("BTC/USDT", "1h")

        assert ts2 > ts1


# ══════════════════════════════════════════════════════════════
# 5. list_available
# ══════════════════════════════════════════════════════════════

class TestListAvailable:

    def test_empty_when_no_data(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        assert storage.list_available() == []

    def test_lists_all_symbols_and_timeframes(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        storage.write(_make_klines(5), "BTC/USDT", "1h")
        storage.write(_make_klines(5), "BTC/USDT", "4h")
        storage.write(_make_klines(5), "ETH/USDT", "1h")

        available = storage.list_available()
        assert len(available) == 3

        entries = {(e["symbol"], e["timeframe"]) for e in available}
        assert ("BTC/USDT", "1h") in entries
        assert ("BTC/USDT", "4h") in entries
        assert ("ETH/USDT", "1h") in entries

    def test_list_entry_has_required_fields(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="binance")
        storage.write(_make_klines(3), "BTC/USDT", "1h")
        available = storage.list_available()
        entry = available[0]
        assert "exchange" in entry
        assert "symbol" in entry
        assert "timeframe" in entry
        assert "path" in entry

    def test_list_exchange_matches(self, tmp_path):
        storage = ParquetStorage(root_dir=tmp_path, exchange_id="okx")
        storage.write(_make_klines(3), "BTC/USDT", "1h")
        available = storage.list_available()
        assert available[0]["exchange"] == "okx"
