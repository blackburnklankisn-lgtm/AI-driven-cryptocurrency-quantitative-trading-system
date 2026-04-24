"""
modules/data/sentiment/providers.py — 情绪数据 Provider 适配层

设计说明：
- 定义情绪数据 provider 的统一接口（抽象基类 SentimentProvider）
- 每个 provider 实现独立的 API 采集逻辑，输出规范化字典
- 第一版内置三个 provider：
    * AlternativeMeProvider  — Alternative.me 恐慌贪婪指数（骨架）
    * CryptoCompareProvider  — CryptoCompare 资金费率/多空数据（骨架）
    * MockSentimentProvider  — 纯 mock 实现，供测试和无 API Key 时降级使用
- provider 只负责"拉取原始值 + 规范化字段名"，不做特征工程
- 采集失败时抛出 SentimentFetchError，上层 Collector 负责重试和降级

规范化字段（第一版 6 个）：
    fear_greed_index          — 恐慌贪婪指数 [0, 100]（0=极端恐慌，100=极端贪婪）
    funding_rate_zscore       — 资金费率 z-score（相对历史均值的标准差数）
    long_short_ratio_change   — 多空比变化率（相对前期）
    open_interest_change      — 持仓量变化率（相对前期）
    liquidation_imbalance     — 强平不平衡指数（多头强平 - 空头强平，归一化）
    sentiment_score_ema       — 综合情绪 EMA 评分 [0, 1]（0=极端负面，1=极端正面）

日志标签：[Sentiment]
"""

from __future__ import annotations

import abc
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger

log = get_logger(__name__)

# 规范化字段名（所有 provider 必须输出这些 key，缺失时用 None）
SENTIMENT_FIELDS = [
    "fear_greed_index",
    "funding_rate_zscore",
    "long_short_ratio_change",
    "open_interest_change",
    "liquidation_imbalance",
    "sentiment_score_ema",
]


class SentimentFetchError(Exception):
    """情绪数据采集失败异常。"""


@dataclass
class SentimentRecord:
    """
    单次情绪数据采集结果（规范化后的结构）。

    Attributes:
        fetched_at:  采集时间（UTC）
        fields:      规范化字段字典（字段名 -> 值，缺失字段为 None）
        source_name: provider 名称（用于 trace）
        metadata:    附加元数据（API 版本、请求耗时等）
    """

    fetched_at: datetime
    fields: dict[str, Optional[float]]
    source_name: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def missing_fields(self) -> list[str]:
        """返回值为 None 的字段列表。"""
        return [k for k, v in self.fields.items() if v is None]

    def has_all_fields(self) -> bool:
        return len(self.missing_fields()) == 0


class SentimentProvider(abc.ABC):
    """
    情绪数据 Provider 抽象基类。

    子类必须实现 fetch() 方法，返回规范化的 SentimentRecord。
    """

    @property
    @abc.abstractmethod
    def provider_name(self) -> str:
        """Provider 名称（用于日志和 trace）。"""

    @abc.abstractmethod
    def fetch(self, symbol: str = "BTC") -> SentimentRecord:
        """
        采集最新情绪数据。

        Args:
            symbol: 目标资产（默认 BTC）

        Returns:
            SentimentRecord 规范化后的情绪数据记录

        Raises:
            SentimentFetchError: 采集失败时抛出
        """


class AlternativeMeProvider(SentimentProvider):
    """
    Alternative.me 恐慌贪婪指数适配层（正式实现无需 API Key）。

    目前实现为骨架，返回全 None 字段（仅占位）。
    真实接入时替换 fetch() 内部的 HTTP 请求逻辑即可。
    """

    def __init__(self) -> None:
        log.info("[Sentiment] AlternativeMeProvider 初始化（骨架模式）")

    @property
    def provider_name(self) -> str:
        return "alternative_me"

    def fetch(self, symbol: str = "BTC") -> SentimentRecord:
        log.warning("[Sentiment] AlternativeMeProvider 尚未实现真实采集，返回空数据")
        return SentimentRecord(
            fetched_at=datetime.now(tz=timezone.utc),
            fields={f: None for f in SENTIMENT_FIELDS},
            source_name=self.provider_name,
            metadata={"mode": "skeleton"},
        )


class CryptoCompareProvider(SentimentProvider):
    """
    CryptoCompare 资金费率/多空数据适配层（正式实现需要有效 API Key）。

    目前实现为骨架，返回全 None 字段。
    真实接入时替换 fetch() 内部的 HTTP 请求逻辑即可。

    Args:
        api_key: CryptoCompare API Key（留空时自动降级为空数据）
    """

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key
        log.info(
            "[Sentiment] CryptoCompareProvider 初始化: has_key={}",
            bool(api_key),
        )

    @property
    def provider_name(self) -> str:
        return "cryptocompare"

    def fetch(self, symbol: str = "BTC") -> SentimentRecord:
        if not self._api_key:
            log.warning("[Sentiment] CryptoCompareProvider 无 API Key，返回空数据")
            return SentimentRecord(
                fetched_at=datetime.now(tz=timezone.utc),
                fields={f: None for f in SENTIMENT_FIELDS},
                source_name=self.provider_name,
                metadata={"mode": "no_key"},
            )
        raise SentimentFetchError(
            "CryptoCompareProvider 真实采集尚未实现，请配置 API Key 后补全逻辑"
        )


class MockSentimentProvider(SentimentProvider):
    """
    情绪数据 Mock Provider（测试与无 API Key 降级使用）。

    生成确定性随机值，符合各字段合理范围：
        fear_greed_index:        [0, 100]
        funding_rate_zscore:     [-3.0, 3.0]
        long_short_ratio_change: [-0.10, 0.10]
        open_interest_change:    [-0.15, 0.15]
        liquidation_imbalance:   [-1.0, 1.0]
        sentiment_score_ema:     [0.0, 1.0]

    Args:
        seed:           随机种子（固定时输出可确定）
        fail_rate:      每次 fetch() 随机失败概率 [0, 1]
        missing_fields: 强制设为 None 的字段列表（模拟缺失场景）
    """

    def __init__(
        self,
        seed: Optional[int] = None,
        fail_rate: float = 0.0,
        missing_fields: Optional[list[str]] = None,
    ) -> None:
        self._rng = random.Random(seed)
        self._fail_rate = fail_rate
        self._missing = set(missing_fields or [])
        log.info(
            "[Sentiment] MockSentimentProvider 初始化: seed={} fail_rate={} "
            "missing_fields={}",
            seed,
            fail_rate,
            missing_fields,
        )

    @property
    def provider_name(self) -> str:
        return "mock_sentiment"

    def fetch(self, symbol: str = "BTC") -> SentimentRecord:
        if self._fail_rate > 0 and self._rng.random() < self._fail_rate:
            raise SentimentFetchError(f"MockSentimentProvider 模拟采集失败: symbol={symbol}")

        def _v(field_name: str) -> Optional[float]:
            if field_name in self._missing:
                return None
            if field_name == "fear_greed_index":
                return round(self._rng.uniform(0.0, 100.0), 2)
            if field_name == "funding_rate_zscore":
                return round(self._rng.uniform(-3.0, 3.0), 4)
            if field_name == "long_short_ratio_change":
                return round(self._rng.uniform(-0.10, 0.10), 4)
            if field_name == "open_interest_change":
                return round(self._rng.uniform(-0.15, 0.15), 4)
            if field_name == "liquidation_imbalance":
                return round(self._rng.uniform(-1.0, 1.0), 4)
            if field_name == "sentiment_score_ema":
                return round(self._rng.uniform(0.0, 1.0), 4)
            return None

        fields = {f: _v(f) for f in SENTIMENT_FIELDS}
        record = SentimentRecord(
            fetched_at=datetime.now(tz=timezone.utc),
            fields=fields,
            source_name=self.provider_name,
            metadata={"symbol": symbol, "seed": self._rng.getstate()},
        )
        log.debug(
            "[Sentiment] Mock 采集完成: symbol={} missing={}",
            symbol,
            record.missing_fields(),
        )
        return record
