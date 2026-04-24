"""
modules/data/onchain/collector.py — 链上数据采集协调器

设计说明：
- 调度 OnChainProvider 的采集工作，带重试和 freshness-aware 跳过逻辑
- freshness-aware 采集：如果缓存仍在 TTL 内，可跳过采集（避免多余 API 调用）
- 重试策略：指数退避，最多 max_retries 次
- 采集结果写入 OnChainCache，供 FeatureBuilder 使用
- 采集失败时记录 WARNING，不抛出（降级不崩溃原则）

接口：
    OnChainCollector(provider, cache, config)
    .collect(symbol, force=False) -> OnChainRecord | None
        force=True 时忽略 freshness，强制重新采集
    .collect_all(symbols, force=False) -> dict[str, OnChainRecord | None]
    .last_result(symbol) -> OnChainRecord | None

日志标签：[OnChain]
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.data.fusion.freshness import FreshnessConfig
from modules.data.fusion.source_contract import FreshnessStatus
from modules.data.onchain.cache import OnChainCache
from modules.data.onchain.providers import (
    OnChainFetchError,
    OnChainProvider,
    OnChainRecord,
)

log = get_logger(__name__)


@dataclass
class CollectorConfig:
    """
    OnChainCollector 配置。

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


class OnChainCollector:
    """
    链上数据采集协调器。

    将 Provider（负责 API 请求）与 Cache（负责本地持久化）解耦。
    调用方只需 .collect(symbol) 即可获得最新有效的链上数据。

    Args:
        provider: OnChainProvider 实例（可在测试中替换为 MockOnChainProvider）
        cache:    OnChainCache 实例（负责缓存读写）
        config:   CollectorConfig 配置
    """

    def __init__(
        self,
        provider: OnChainProvider,
        cache: OnChainCache,
        config: Optional[CollectorConfig] = None,
    ) -> None:
        self.provider = provider
        self.cache = cache
        self.config = config or CollectorConfig()
        self._last_results: dict[str, Optional[OnChainRecord]] = {}
        log.info(
            "[OnChain] OnChainCollector 初始化: provider={} max_retries={} "
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
    ) -> Optional[OnChainRecord]:
        """
        采集指定 symbol 的链上数据。

        如果 skip_if_fresh=True 且缓存仍在 TTL 内，跳过 API 调用。
        失败时返回 None（不抛出），降级由调用方处理。

        Args:
            symbol: 目标资产（如 "BTC"）
            force:  是否强制重新采集（忽略 freshness 跳过逻辑）

        Returns:
            OnChainRecord（成功）或 None（失败后读取缓存或完全降级）
        """
        # ── freshness-aware 跳过 ──────────────────────────────────
        if not force and self.config.skip_if_fresh:
            freshness = self.cache.evaluate_freshness(
                symbol,
                self.config.freshness_config,
            )
            if freshness.status == FreshnessStatus.FRESH:
                log.debug(
                    "[OnChain] 跳过采集（缓存 FRESH）: symbol={} lag={:.0f}s",
                    symbol,
                    freshness.lag_sec,
                )
                # 从缓存构造 OnChainRecord 返回
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
                    "[OnChain] 采集成功: symbol={} provider={} "
                    "attempt={}/{} elapsed={:.2f}s missing={}",
                    symbol,
                    self.provider.provider_name,
                    attempt + 1,
                    1 + self.config.max_retries,
                    elapsed,
                    record.missing_fields(),
                )
                return record

            except OnChainFetchError as exc:
                last_exc = exc
                log.warning(
                    "[OnChain] 采集失败 attempt={}/{}: symbol={} error={}",
                    attempt + 1,
                    1 + self.config.max_retries,
                    symbol,
                    exc,
                )
                if attempt < self.config.max_retries:
                    log.debug("[OnChain] 等待 {:.1f}s 后重试", backoff)
                    time.sleep(backoff)
                    backoff *= 2  # 指数退避

        # ── 全部重试失败：从缓存降级 ─────────────────────────────
        log.warning(
            "[OnChain] 所有采集重试均失败，尝试从缓存降级: symbol={} last_error={}",
            symbol,
            last_exc,
        )
        cached_record = self._build_record_from_cache(symbol)
        if cached_record:
            log.info("[OnChain] 从缓存恢复数据: symbol={}", symbol)
        else:
            log.warning("[OnChain] 缓存也为空，返回 None: symbol={}", symbol)
        self._last_results[symbol] = cached_record
        return cached_record

    def collect_all(
        self,
        symbols: list[str],
        force: bool = False,
    ) -> dict[str, Optional[OnChainRecord]]:
        """
        批量采集多个 symbol 的链上数据。

        Args:
            symbols: 目标资产列表
            force:   是否强制重新采集

        Returns:
            dict: symbol -> OnChainRecord（或 None）
        """
        results: dict[str, Optional[OnChainRecord]] = {}
        for symbol in symbols:
            results[symbol] = self.collect(symbol, force=force)
        return results

    def last_result(self, symbol: str) -> Optional[OnChainRecord]:
        """返回上次 collect() 的结果（用于诊断，不触发新采集）。"""
        return self._last_results.get(symbol)

    # ──────────────────────────────────────────────────────────────
    # 内部辅助
    # ──────────────────────────────────────────────────────────────

    def _build_record_from_cache(self, symbol: str) -> Optional[OnChainRecord]:
        """从缓存数据构造 OnChainRecord。"""
        cached = self.cache.read(symbol)
        if not cached:
            return None
        fields = {
            field_name: entry["value"]
            for field_name, entry in cached.items()
            if "value" in entry
        }
        # 取最新的 collected_at
        collected_ats = [
            entry["collected_at"]
            for entry in cached.values()
            if entry.get("collected_at") is not None
        ]
        fetched_at = max(collected_ats) if collected_ats else datetime.now(tz=timezone.utc)

        return OnChainRecord(
            fetched_at=fetched_at,
            fields=fields,
            source_name=f"cache/{self.provider.provider_name}",
            metadata={"from_cache": True, "symbol": symbol},
        )

    def diagnostics(self) -> dict[str, Any]:
        """返回采集器诊断信息。"""
        return {
            "provider": self.provider.provider_name,
            "max_retries": self.config.max_retries,
            "skip_if_fresh": self.config.skip_if_fresh,
            "last_collected_symbols": list(self._last_results.keys()),
            "cache": self.cache.diagnostics(),
        }
