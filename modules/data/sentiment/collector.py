"""
modules/data/sentiment/collector.py — 情绪数据采集协调器

设计说明：
- 调度 SentimentProvider 的采集工作，带重试和 freshness-aware 跳过逻辑
- freshness-aware 采集：如果缓存仍在 TTL 内，可跳过采集（避免多余 API 调用）
- 重试策略：指数退避，最多 max_retries 次
- 采集结果写入 SentimentCache，供 FeatureBuilder 使用
- 采集失败时记录 WARNING，不抛出（降级不崩溃原则）
- 情绪数据采集频率建议：fear_greed 每日、资金费率每 8h、多空比每 1h

接口：
    SentimentCollector(provider, cache, config)
    .collect(symbol, force=False) -> SentimentRecord | None
        force=True 时忽略 freshness，强制重新采集
    .collect_all(symbols, force=False) -> dict[str, SentimentRecord | None]
    .last_result(symbol) -> SentimentRecord | None
    .diagnostics() -> dict

日志标签：[Sentiment]
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.data.fusion.freshness import FreshnessConfig
from modules.data.fusion.source_contract import FreshnessStatus
from modules.data.sentiment.cache import SentimentCache
from modules.data.sentiment.providers import (
    SentimentFetchError,
    SentimentProvider,
    SentimentRecord,
    SENTIMENT_FIELDS,
)

log = get_logger(__name__)


@dataclass
class SentimentCollectorConfig:
    """
    SentimentCollector 配置。

    Attributes:
        max_retries:         最大重试次数（不含首次尝试）
        retry_backoff_sec:   初始退避时间（秒），每次重试翻倍
        skip_if_fresh:       是否跳过缓存仍 FRESH 的采集（节省 API 配额）
        freshness_config:    freshness 评估配置（用于判断是否跳过）
    """

    max_retries: int = 2
    retry_backoff_sec: float = 5.0
    skip_if_fresh: bool = True
    freshness_config: Optional[FreshnessConfig] = None


class SentimentCollector:
    """
    情绪数据采集协调器。

    将 Provider（负责 API 请求）与 Cache（负责本地持久化）解耦。
    调用方只需 .collect(symbol) 即可获得最新有效的情绪数据。

    Args:
        provider: SentimentProvider 实例（可在测试中替换为 MockSentimentProvider）
        cache:    SentimentCache 实例（负责缓存读写）
        config:   SentimentCollectorConfig 配置
    """

    def __init__(
        self,
        provider: SentimentProvider,
        cache: SentimentCache,
        config: Optional[SentimentCollectorConfig] = None,
    ) -> None:
        self.provider = provider
        self.cache = cache
        self.config = config or SentimentCollectorConfig()
        self._last_results: dict[str, Optional[SentimentRecord]] = {}
        log.info(
            "[Sentiment] SentimentCollector 初始化: provider={} max_retries={} "
            "skip_if_fresh={}",
            provider.provider_name,
            self.config.max_retries,
            self.config.skip_if_fresh,
        )

    # ──────────────────────────────────────────────────────────────
    # 核心采集接口
    # ──────────────────────────────────────────────────────────────

    def collect(
        self,
        symbol: str = "BTC",
        force: bool = False,
    ) -> Optional[SentimentRecord]:
        """
        采集指定 symbol 的情绪数据。

        如果 skip_if_fresh=True 且缓存仍在 TTL 内，跳过 API 调用。
        失败时返回 None（不抛出），降级由调用方处理。

        Args:
            symbol: 目标资产（如 "BTC"）
            force:  是否强制重新采集（忽略 freshness 跳过逻辑）

        Returns:
            SentimentRecord（成功）或 None（失败后读取缓存或完全降级）
        """
        # ── freshness-aware 跳过 ──────────────────────────────────
        if not force and self.config.skip_if_fresh:
            freshness = self.cache.evaluate_freshness(
                symbol,
                self.config.freshness_config,
            )
            if freshness.status == FreshnessStatus.FRESH:
                log.debug(
                    "[Sentiment] 跳过采集（缓存 FRESH）: symbol={} lag={:.0f}s",
                    symbol,
                    freshness.lag_sec,
                )
                return self._build_record_from_cache(symbol)

        # ── 带重试的采集 ─────────────────────────────────────────
        last_exc: Optional[Exception] = None
        backoff = self.config.retry_backoff_sec

        for attempt in range(1 + self.config.max_retries):
            try:
                t0 = time.monotonic()
                record = self.provider.fetch(symbol)
                elapsed = time.monotonic() - t0

                self.cache.write(symbol, record)
                self._last_results[symbol] = record

                log.debug(
                    "[Sentiment] 采集成功: symbol={} provider={} "
                    "attempt={}/{} elapsed={:.2f}s missing={}",
                    symbol,
                    self.provider.provider_name,
                    attempt + 1,
                    1 + self.config.max_retries,
                    elapsed,
                    record.missing_fields(),
                )
                return record

            except SentimentFetchError as exc:
                last_exc = exc
                log.warning(
                    "[Sentiment] 采集失败 attempt={}/{}: symbol={} error={}",
                    attempt + 1,
                    1 + self.config.max_retries,
                    symbol,
                    exc,
                )
                if attempt < self.config.max_retries:
                    log.debug("[Sentiment] 等待 {:.1f}s 后重试", backoff)
                    time.sleep(backoff)
                    backoff *= 2

        # ── 全部重试失败：从缓存降级 ─────────────────────────────
        log.warning(
            "[Sentiment] 所有采集重试均失败，尝试从缓存降级: symbol={} last_error={}",
            symbol,
            last_exc,
        )
        cached_record = self._build_record_from_cache(symbol)
        if cached_record:
            log.info("[Sentiment] 从缓存恢复数据: symbol={}", symbol)
        else:
            log.warning("[Sentiment] 缓存也为空，返回 None: symbol={}", symbol)
        self._last_results[symbol] = cached_record
        return cached_record

    def collect_all(
        self,
        symbols: list[str],
        force: bool = False,
    ) -> dict[str, Optional[SentimentRecord]]:
        """
        批量采集多个 symbol 的情绪数据。

        Args:
            symbols: 目标资产列表
            force:   是否强制重新采集

        Returns:
            { symbol: SentimentRecord | None }
        """
        results: dict[str, Optional[SentimentRecord]] = {}
        for sym in symbols:
            results[sym] = self.collect(sym, force=force)
        return results

    def last_result(self, symbol: str) -> Optional[SentimentRecord]:
        """返回最近一次采集结果（可能为 None）。"""
        return self._last_results.get(symbol)

    def diagnostics(self) -> dict[str, Any]:
        """返回采集器诊断信息。"""
        return {
            "provider": self.provider.provider_name,
            "config": {
                "max_retries": self.config.max_retries,
                "skip_if_fresh": self.config.skip_if_fresh,
                "retry_backoff_sec": self.config.retry_backoff_sec,
            },
            "last_results_symbols": list(self._last_results.keys()),
            "cache_diagnostics": self.cache.diagnostics(),
        }

    # ──────────────────────────────────────────────────────────────
    # 内部辅助
    # ──────────────────────────────────────────────────────────────

    def _build_record_from_cache(self, symbol: str) -> Optional[SentimentRecord]:
        """从缓存重建 SentimentRecord（降级路径使用）。"""
        raw = self.cache.read(symbol)
        if not raw:
            return None

        fields: dict[str, Optional[float]] = {}
        latest_at: Optional[datetime] = None

        for field_name in SENTIMENT_FIELDS:
            entry = raw.get(field_name)
            if entry is None:
                fields[field_name] = None
                continue
            fields[field_name] = entry.get("value")
            at = entry.get("collected_at")
            if at and (latest_at is None or at > latest_at):
                latest_at = at

        fetched_at = latest_at or datetime.now(tz=timezone.utc)
        return SentimentRecord(
            fetched_at=fetched_at,
            fields=fields,
            source_name=f"{self.provider.provider_name}_cache",
            metadata={"source": "cache_fallback", "symbol": symbol},
        )
