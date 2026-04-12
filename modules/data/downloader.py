"""
modules/data/downloader.py — CCXT 历史 K 线下载器

设计说明：
- 统一通过 CCXT 拉取历史 K 线，支持所有 CCXT 兼容交易所
- 支持断点续传：跳过本地已存在的时间段，避免重复拉取
- 支持速率限制（尊重交易所 rate limit）
- 原始数据只做时区对齐（UTC），不做任何特征工程
- 严格按时间顺序拉取，防止任何形式的"未来数据"混入

接口：
    KlineDownloader(exchange_id, symbols, timeframe, storage)
    .download(since, until)    → 下载并落盘，返回下载统计
    .download_one(symbol, ...) → 下载单个币种，用于并行调用

失败模式：
- 网络超时：自动重试 max_retries 次，超出抛出 DataFetchError
- 交易所返回空数据：记录 WARNING，跳过而不崩溃
- 数据校验不通过：抛出 DataValidationError

测试策略：
- 使用 mock CCXT exchange 返回预设数据
- 验证断点续传逻辑正确跳过已有时间段
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import ccxt
import pandas as pd

from core.exceptions import DataFetchError, DataValidationError
from core.logger import get_logger
from modules.data.storage import ParquetStorage
from modules.data.validator import KlineValidator

log = get_logger(__name__)

# CCXT OHLCV 列顺序（固定）
_OHLCV_COLS = ["timestamp", "open", "high", "low", "close", "volume"]


class KlineDownloader:
    """
    基于 CCXT 的历史 K 线下载器。

    支持：
    - 多交易所（CCXT 兼容）
    - 断点续传（基于 ParquetStorage 中已有数据的最新时间戳）
    - 自动限速与重试

    Args:
        exchange_id:  CCXT 交易所 ID，如 "binance", "okx"
        symbols:      目标交易对列表，如 ["BTC/USDT", "ETH/USDT"]
        timeframe:    K 线周期，如 "1h", "4h", "1d"
        storage:      ParquetStorage 实例，用于读取已有数据与写入新数据
        api_key:      可选，交易所 API Key（只读公开数据可留空）
        secret:       可选，交易所 Secret
        max_retries:  单次 API 调用最大重试次数
        request_delay_ms: 每次请求之间的最小间隔（ms），避免触发频率限制
    """

    def __init__(
        self,
        exchange_id: str,
        symbols: List[str],
        timeframe: str,
        storage: ParquetStorage,
        api_key: str = "",
        secret: str = "",
        max_retries: int = 3,
        request_delay_ms: int = 200,
    ) -> None:
        self.symbols = symbols
        self.timeframe = timeframe
        self.storage = storage
        self.max_retries = max_retries
        self.request_delay_ms = request_delay_ms

        # 构建 CCXT exchange 实例
        exchange_class = getattr(ccxt, exchange_id, None)
        if exchange_class is None:
            raise DataFetchError(f"未知的交易所 ID: {exchange_id}")

        config: Dict[str, object] = {
            "enableRateLimit": True,
            "rateLimit": max(request_delay_ms, 100),
        }
        if api_key:
            config["apiKey"] = api_key
        if secret:
            config["secret"] = secret

        self._exchange: ccxt.Exchange = exchange_class(config)
        log.info(
            "KlineDownloader 初始化: exchange={} symbols={} timeframe={}",
            exchange_id,
            symbols,
            timeframe,
        )

    # ────────────────────────────────────────────────────────────
    # 公开接口
    # ────────────────────────────────────────────────────────────

    def download(
        self,
        since: datetime,
        until: Optional[datetime] = None,
        batch_size: int = 1000,
    ) -> Dict[str, int]:
        """
        批量下载所有目标交易对的历史 K 线，并落盘。

        Args:
            since:      开始时间（UTC）
            until:      结束时间（UTC），None 表示到当前时间
            batch_size: 每次 API 调用拉取的 K 线数量上限

        Returns:
            dict: {symbol: 新增 K 线条数}
        """
        if until is None:
            until = datetime.now(tz=timezone.utc)

        if since.tzinfo is None or until.tzinfo is None:
            raise DataValidationError("since 和 until 必须包含时区信息（UTC）")

        if since >= until:
            raise DataValidationError(f"since={since} 必须早于 until={until}")

        stats: Dict[str, int] = {}
        for symbol in self.symbols:
            try:
                count = self.download_one(symbol, since, until, batch_size)
                stats[symbol] = count
            except DataFetchError as exc:
                log.error("下载失败, symbol={}, 错误={}", symbol, exc)
                stats[symbol] = 0

        log.info("下载完成: {}", stats)
        return stats

    def download_one(
        self,
        symbol: str,
        since: datetime,
        until: datetime,
        batch_size: int = 1000,
    ) -> int:
        """
        下载单个交易对的历史 K 线，支持断点续传。

        Returns:
            新增写入的 K 线条数（不含已存在的）
        """
        # ── 断点续传：找出本地已有数据的最新时间戳 ──────────────
        resume_ts = self._get_resume_timestamp(symbol, since)
        log.info(
            "开始下载: symbol={} timeframe={} from={} until={}",
            symbol,
            self.timeframe,
            resume_ts.isoformat(),
            until.isoformat(),
        )

        all_rows: List[pd.DataFrame] = []
        current_since_ms = int(resume_ts.timestamp() * 1000)
        until_ms = int(until.timestamp() * 1000)
        total_new = 0

        while current_since_ms < until_ms:
            raw = self._fetch_with_retry(symbol, current_since_ms, batch_size)

            if not raw:
                log.warning("交易所返回空数据: symbol={} since_ms={}", symbol, current_since_ms)
                break

            df = self._raw_to_dataframe(raw, symbol)

            # 过滤超出 until 的数据
            df = df[df["timestamp"] <= until]
            if df.empty:
                break

            all_rows.append(df)
            total_new += len(df)

            # 推进游标到已获取数据的下一个时间点
            last_ts_ms = int(df["timestamp"].max().timestamp() * 1000)
            if last_ts_ms <= current_since_ms:
                break  # 防止死循环
            current_since_ms = last_ts_ms + 1

            time.sleep(self.request_delay_ms / 1000)

        if all_rows:
            combined = pd.concat(all_rows, ignore_index=True)
            # 落盘前先校验
            validator = KlineValidator()
            combined = validator.validate(combined, symbol=symbol, timeframe=self.timeframe)
            self.storage.write(combined, symbol=symbol, timeframe=self.timeframe)
            log.info("落盘完成: symbol={} 新增={}条", symbol, total_new)
        else:
            log.info("无新数据需要写入: symbol={}", symbol)

        return total_new

    # ────────────────────────────────────────────────────────────
    # 内部方法
    # ────────────────────────────────────────────────────────────

    def _get_resume_timestamp(self, symbol: str, default_since: datetime) -> datetime:
        """
        若本地已有数据，返回最新时间戳 + 1 个 timeframe 作为断点续传起点。
        否则返回 default_since。
        """
        try:
            existing = self.storage.read(symbol=symbol, timeframe=self.timeframe)
            if existing is not None and not existing.empty:
                last_ts: pd.Timestamp = existing["timestamp"].max()
                # 推进一个 K 线周期，避免重复下载最后一根
                freq = pd.tseries.frequencies.to_offset(self.timeframe.upper())
                if freq is not None:
                    next_ts = last_ts + freq
                    result = next_ts.to_pydatetime()
                    if result.tzinfo is None:
                        result = result.replace(tzinfo=timezone.utc)
                    log.info("断点续传: symbol={} 从 {} 继续", symbol, result.isoformat())
                    return result
        except Exception as exc:  # noqa: BLE001
            log.warning("读取已有数据失败，从头开始: {}", exc)

        return default_since

    def _fetch_with_retry(
        self,
        symbol: str,
        since_ms: int,
        limit: int,
    ) -> list:
        """带重试机制的 CCXT fetch_ohlcv 调用。"""
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                data = self._exchange.fetch_ohlcv(
                    symbol,
                    timeframe=self.timeframe,
                    since=since_ms,
                    limit=limit,
                )
                return data or []
            except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
                last_exc = exc
                wait = 2 ** attempt  # 指数退避
                log.warning(
                    "网络错误，第 {}/{} 次重试，等待 {}s: {}",
                    attempt,
                    self.max_retries,
                    wait,
                    exc,
                )
                time.sleep(wait)
            except ccxt.RateLimitExceeded as exc:
                last_exc = exc
                log.warning("触发频率限制，等待 10s: {}", exc)
                time.sleep(10)
            except ccxt.ExchangeError as exc:
                # 非网络错误，不重试
                raise DataFetchError(f"交易所返回错误: {exc}") from exc

        raise DataFetchError(
            f"下载失败（已重试 {self.max_retries} 次）: symbol={symbol}"
        ) from last_exc

    @staticmethod
    def _raw_to_dataframe(raw: list, symbol: str) -> pd.DataFrame:
        """将 CCXT 原始 OHLCV 数组转换为标准 DataFrame（UTC 时间戳）。"""
        df = pd.DataFrame(raw, columns=_OHLCV_COLS)

        # CCXT 返回毫秒级 Unix 时间戳，转换为 UTC datetime
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df["symbol"] = symbol

        # 确保数值列类型正确
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # 按时间升序排列
        df = df.sort_values("timestamp").reset_index(drop=True)

        return df[["timestamp", "symbol", "open", "high", "low", "close", "volume"]]
