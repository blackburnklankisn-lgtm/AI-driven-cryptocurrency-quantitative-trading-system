"""
modules/data/onchain/providers.py — 链上数据 Provider 适配层

设计说明：
- 定义链上数据 provider 的统一接口（抽象基类 OnChainProvider）
- 每个 provider 实现独立的 API 采集逻辑，输出规范化字典
- 第一版内置两个 provider：
    * GlassnodeProvider   — 接口留空（需真实 API Key，测试时可 mock）
    * CryptoQuantProvider — 接口留空（同上）
    * MockOnChainProvider — 纯 mock 实现，供测试和无 API Key 时降级使用
- provider 只负责"拉取原始值 + 规范化字段名"，不做特征工程
- 采集失败时抛出 OnChainFetchError，上层 Collector 负责重试和降级

规范化字段（第一版 6 个）：
    active_addresses_change     — 活跃地址数变化率（相对前期）
    exchange_inflow_ratio       — 交易所净流入量占比
    whale_tx_count_ratio        — 大额交易笔数占比（相对总笔数）
    stablecoin_supply_ratio     — 稳定币供应量相对 BTC mktcap 比率
    miner_reserve_change        — 矿工余额变化率
    nvt_proxy                   — NVT 代理值（链上活动 vs 市值）

日志标签：[OnChain]
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
ONCHAIN_FIELDS = [
    "active_addresses_change",
    "exchange_inflow_ratio",
    "whale_tx_count_ratio",
    "stablecoin_supply_ratio",
    "miner_reserve_change",
    "nvt_proxy",
]


class OnChainFetchError(Exception):
    """链上数据采集失败异常。"""


@dataclass
class OnChainRecord:
    """
    单次链上数据采集结果（规范化后的结构）。

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


class OnChainProvider(abc.ABC):
    """
    链上数据 Provider 抽象基类。

    子类必须实现 fetch() 方法，返回规范化的 OnChainRecord。
    """

    @property
    @abc.abstractmethod
    def provider_name(self) -> str:
        """Provider 名称（用于日志和 trace）。"""

    @abc.abstractmethod
    def fetch(self, symbol: str = "BTC") -> OnChainRecord:
        """
        采集最新链上数据。

        Args:
            symbol: 目标资产（默认 BTC）

        Returns:
            OnChainRecord 规范化后的链上数据记录

        Raises:
            OnChainFetchError: 采集失败时抛出
        """


class GlassnodeProvider(OnChainProvider):
    """
    Glassnode API 适配层（正式实现需要有效 API Key）。

    目前实现为骨架，返回全 None 字段（模拟无效 Key 时降级行为）。
    真实接入时替换 fetch() 内部的 HTTP 请求逻辑即可。

    Args:
        api_key: Glassnode API Key（留空时自动降级为空数据）
    """

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key
        log.info(
            "[OnChain] GlassnodeProvider 初始化: has_key={}",
            bool(api_key),
        )

    @property
    def provider_name(self) -> str:
        return "glassnode"

    def fetch(self, symbol: str = "BTC") -> OnChainRecord:
        """
        采集 Glassnode 链上数据。

        注意：当前为骨架实现，真实 HTTP 请求留作后续接入。
        无 API Key 时直接返回全 None 降级记录。
        """
        now = datetime.now(tz=timezone.utc)

        if not self._api_key:
            log.warning(
                "[OnChain] GlassnodeProvider 无 API Key，返回降级空数据 symbol={}",
                symbol,
            )
            return OnChainRecord(
                fetched_at=now,
                fields={f: None for f in ONCHAIN_FIELDS},
                source_name=self.provider_name,
                metadata={"degraded": True, "reason": "no_api_key"},
            )

        # ── 真实 API 调用骨架（后续实现）────────────────────────
        # resp = requests.get(
        #     "https://api.glassnode.com/v1/metrics/...",
        #     headers={"X-Api-Key": self._api_key},
        #     params={"a": symbol, "i": "24h"},
        #     timeout=10,
        # )
        # resp.raise_for_status()
        # raw = resp.json()
        # fields = _parse_glassnode(raw)  # 解析逻辑待实现
        raise OnChainFetchError(
            "GlassnodeProvider.fetch() 真实实现尚未接入，请使用 MockOnChainProvider"
        )


class CryptoQuantProvider(OnChainProvider):
    """
    CryptoQuant API 适配层骨架（需要有效 API Key）。

    当前为骨架实现，无 API Key 时降级为空数据。
    """

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key
        log.info(
            "[OnChain] CryptoQuantProvider 初始化: has_key={}",
            bool(api_key),
        )

    @property
    def provider_name(self) -> str:
        return "cryptoquant"

    def fetch(self, symbol: str = "BTC") -> OnChainRecord:
        now = datetime.now(tz=timezone.utc)

        if not self._api_key:
            log.warning(
                "[OnChain] CryptoQuantProvider 无 API Key，返回降级空数据 symbol={}",
                symbol,
            )
            return OnChainRecord(
                fetched_at=now,
                fields={f: None for f in ONCHAIN_FIELDS},
                source_name=self.provider_name,
                metadata={"degraded": True, "reason": "no_api_key"},
            )

        raise OnChainFetchError(
            "CryptoQuantProvider.fetch() 真实实现尚未接入"
        )


class MockOnChainProvider(OnChainProvider):
    """
    Mock 链上数据 Provider —— 用于测试、无 API Key 降级运行。

    生成确定性随机数（可设置 seed），模拟真实链上数据的范围特征：
    - active_addresses_change: [-0.05, +0.05]（相对变化率）
    - exchange_inflow_ratio:   [0.01, 0.30]
    - whale_tx_count_ratio:    [0.01, 0.15]
    - stablecoin_supply_ratio: [0.05, 0.40]
    - miner_reserve_change:    [-0.03, +0.03]
    - nvt_proxy:               [0.5, 3.0]

    Args:
        seed:            随机种子（默认 42，确保测试可重现）
        fail_rate:       模拟 API 失败的概率（0~1，默认 0 不失败）
        missing_fields:  指定哪些字段返回 None（模拟部分字段缺失）
    """

    def __init__(
        self,
        seed: int = 42,
        fail_rate: float = 0.0,
        missing_fields: Optional[list[str]] = None,
    ) -> None:
        self._rng = random.Random(seed)
        self._fail_rate = fail_rate
        self._missing_fields = set(missing_fields or [])
        log.info(
            "[OnChain] MockOnChainProvider 初始化: seed={} fail_rate={} missing={}",
            seed,
            fail_rate,
            list(self._missing_fields),
        )

    @property
    def provider_name(self) -> str:
        return "mock_onchain"

    def fetch(self, symbol: str = "BTC") -> OnChainRecord:
        # 模拟 API 失败
        if self._fail_rate > 0 and self._rng.random() < self._fail_rate:
            raise OnChainFetchError(
                f"MockOnChainProvider 模拟采集失败 (fail_rate={self._fail_rate})"
            )

        now = datetime.now(tz=timezone.utc)
        rng = self._rng

        fields: dict[str, Optional[float]] = {
            "active_addresses_change": rng.uniform(-0.05, 0.05)
            if "active_addresses_change" not in self._missing_fields
            else None,
            "exchange_inflow_ratio": rng.uniform(0.01, 0.30)
            if "exchange_inflow_ratio" not in self._missing_fields
            else None,
            "whale_tx_count_ratio": rng.uniform(0.01, 0.15)
            if "whale_tx_count_ratio" not in self._missing_fields
            else None,
            "stablecoin_supply_ratio": rng.uniform(0.05, 0.40)
            if "stablecoin_supply_ratio" not in self._missing_fields
            else None,
            "miner_reserve_change": rng.uniform(-0.03, 0.03)
            if "miner_reserve_change" not in self._missing_fields
            else None,
            "nvt_proxy": rng.uniform(0.5, 3.0)
            if "nvt_proxy" not in self._missing_fields
            else None,
        }

        log.debug(
            "[OnChain] MockOnChainProvider.fetch symbol={} fields={}",
            symbol,
            {k: f"{v:.4f}" if v is not None else "None" for k, v in fields.items()},
        )

        return OnChainRecord(
            fetched_at=now,
            fields=fields,
            source_name=self.provider_name,
            metadata={"symbol": symbol, "mock": True},
        )
