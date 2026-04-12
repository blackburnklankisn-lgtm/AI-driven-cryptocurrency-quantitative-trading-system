"""
tests/test_data_downloader.py — 下载器单元测试

使用 Mock CCXT exchange 测试下载器逻辑。
不发起真实 API 调用。

覆盖项：
- 正常下载并落盘
- 断点续传（跳过已有数据时间段）
- 网络错误超出重试次数时抛出 DataFetchError
- 时区校验（since/until 必须含时区）
- since >= until 时校验失败
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.exceptions import DataFetchError, DataValidationError
from modules.data.downloader import KlineDownloader
from modules.data.storage import ParquetStorage


def make_mock_ohlcv(n: int = 5, start_ms: int = 1_700_000_000_000) -> list:
    """生成 n 条模拟 CCXT OHLCV 数据（毫秒时间戳）。"""
    interval_ms = 3_600_000  # 1 小时
    return [
        [start_ms + i * interval_ms, 100.0 + i, 105.0 + i, 95.0 + i, 102.0 + i, 1000.0 + i]
        for i in range(n)
    ]


@pytest.fixture
def tmp_storage(tmp_path):
    """返回使用临时目录的 ParquetStorage。"""
    return ParquetStorage(root_dir=tmp_path / "storage", exchange_id="binance")


@pytest.fixture
def mock_exchange():
    """返回带 fetch_ohlcv mock 的假交易所对象。"""
    exchange = MagicMock()
    exchange.fetch_ohlcv.return_value = make_mock_ohlcv(5)
    return exchange


class TestKlineDownloaderInit:
    def test_invalid_exchange_raises(self, tmp_storage: ParquetStorage) -> None:
        """未知的交易所 ID 应抛出 DataFetchError。"""
        with pytest.raises(DataFetchError, match="未知的交易所"):
            KlineDownloader(
                exchange_id="fake_exchange_xyz",
                symbols=["BTC/USDT"],
                timeframe="1h",
                storage=tmp_storage,
            )


class TestInputValidation:
    def test_naive_since_raises(self, tmp_storage: ParquetStorage) -> None:
        """since 无时区信息时应抛出 DataValidationError。"""
        downloader = KlineDownloader.__new__(KlineDownloader)
        downloader.symbols = ["BTC/USDT"]
        downloader.timeframe = "1h"
        downloader.storage = tmp_storage
        downloader.max_retries = 3
        downloader.request_delay_ms = 0
        downloader._exchange = MagicMock()

        with pytest.raises(DataValidationError, match="时区"):
            downloader.download(
                since=datetime(2024, 1, 1),  # naive（无时区）
                until=datetime(2024, 1, 2),
            )

    def test_since_after_until_raises(self, tmp_storage: ParquetStorage) -> None:
        """since >= until 时应抛出 DataValidationError。"""
        downloader = KlineDownloader.__new__(KlineDownloader)
        downloader.symbols = ["BTC/USDT"]
        downloader.timeframe = "1h"
        downloader.storage = tmp_storage
        downloader.max_retries = 3
        downloader.request_delay_ms = 0
        downloader._exchange = MagicMock()

        since = datetime(2024, 1, 10, tzinfo=timezone.utc)
        until = datetime(2024, 1, 1, tzinfo=timezone.utc)
        with pytest.raises(DataValidationError, match="早于"):
            downloader.download(since=since, until=until)


class TestDownloadOne:
    def _make_downloader(self, storage: ParquetStorage, exchange_mock) -> KlineDownloader:
        """构建使用 mock exchange 的下载器。"""
        downloader = KlineDownloader.__new__(KlineDownloader)
        downloader.symbols = ["BTC/USDT"]
        downloader.timeframe = "1h"
        downloader.storage = storage
        downloader.max_retries = 3
        downloader.request_delay_ms = 0
        downloader._exchange = exchange_mock
        return downloader

    def test_downloads_and_writes_parquet(
        self, tmp_storage: ParquetStorage, mock_exchange
    ) -> None:
        """正常下载应写入 Parquet 文件。"""
        # 模拟：第一次返回 5 条数据，第二次返回空（终止循环）
        mock_exchange.fetch_ohlcv.side_effect = [
            make_mock_ohlcv(5, start_ms=1_700_000_000_000),
            [],  # 终止信号
        ]
        downloader = self._make_downloader(tmp_storage, mock_exchange)

        since = datetime(2023, 11, 15, tzinfo=timezone.utc)
        until = datetime(2023, 11, 16, tzinfo=timezone.utc)
        count = downloader.download_one("BTC/USDT", since, until)

        assert count > 0
        df = tmp_storage.read("BTC/USDT", "1h")
        assert df is not None
        assert not df.empty

    def test_resume_skips_existing_data(
        self, tmp_storage: ParquetStorage, mock_exchange
    ) -> None:
        """断点续传应从已有数据的最新时间戳之后开始请求。"""
        # 先写入初始数据
        initial_data = make_mock_ohlcv(3, start_ms=1_700_000_000_000)
        initial_df = KlineDownloader._raw_to_dataframe(initial_data, "BTC/USDT")
        tmp_storage.write(initial_df, "BTC/USDT", "1h")

        downloader = self._make_downloader(tmp_storage, mock_exchange)

        # 续传请求应从已有数据之后开始
        since = datetime(2023, 11, 14, tzinfo=timezone.utc)
        until = datetime(2023, 11, 20, tzinfo=timezone.utc)

        mock_exchange.fetch_ohlcv.side_effect = [
            make_mock_ohlcv(3, start_ms=1_700_010_800_000),  # 续传数据
            [],
        ]
        downloader.download_one("BTC/USDT", since, until)

        # 验证请求的 since_ms 大于初始数据的最新时间戳
        called_since_ms = mock_exchange.fetch_ohlcv.call_args_list[0][1]["since"]
        initial_max_ms = int(initial_df["timestamp"].max().timestamp() * 1000)
        assert called_since_ms > initial_max_ms

    def test_retry_on_network_error(
        self, tmp_storage: ParquetStorage
    ) -> None:
        """网络错误应触发重试，超出次数后抛出 DataFetchError。"""
        import ccxt

        exchange_mock = MagicMock()
        exchange_mock.fetch_ohlcv.side_effect = ccxt.NetworkError("连接超时")

        downloader = KlineDownloader.__new__(KlineDownloader)
        downloader.symbols = ["BTC/USDT"]
        downloader.timeframe = "1h"
        downloader.storage = tmp_storage
        downloader.max_retries = 2
        downloader.request_delay_ms = 0
        downloader._exchange = exchange_mock

        with pytest.raises(DataFetchError, match="下载失败"):
            downloader._fetch_with_retry("BTC/USDT", 0, 100)

        # 应该重试了 max_retries 次
        assert exchange_mock.fetch_ohlcv.call_count == 2


class TestRawToDataFrame:
    def test_converts_timestamps_to_utc(self) -> None:
        """毫秒时间戳应正确转换为 UTC datetime。"""
        raw = make_mock_ohlcv(3)
        df = KlineDownloader._raw_to_dataframe(raw, "BTC/USDT")

        assert str(df["timestamp"].dt.tz) == "UTC"
        assert list(df.columns) == ["timestamp", "symbol", "open", "high", "low", "close", "volume"]

    def test_sorted_ascending(self) -> None:
        """输出 DataFrame 应按 timestamp 升序排列。"""
        raw = make_mock_ohlcv(5)
        # 打乱顺序
        raw_shuffled = [raw[2], raw[0], raw[4], raw[1], raw[3]]
        df = KlineDownloader._raw_to_dataframe(raw_shuffled, "BTC/USDT")
        assert df["timestamp"].is_monotonic_increasing
