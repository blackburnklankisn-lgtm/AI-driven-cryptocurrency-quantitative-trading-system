"""
modules/data/onchain/providers.py — 链上数据 Provider 适配层

设计说明：
- 定义链上数据 provider 的统一接口（抽象基类 OnChainProvider）
- 每个 provider 实现独立的 API 采集逻辑，输出规范化字典
- 第一版内置三个正式 provider：
    * PublicOnChainProvider  — 公开 API 聚合的链上代理指标（真实实现）
    * GlassnodeProvider      — 接口留空（需真实 API Key，测试时可 mock）
    * CryptoQuantProvider    — 接口留空（同上）
    * MockOnChainProvider    — 纯 mock 实现，供测试和无 API Key 时降级使用
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

import requests

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


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _pct_change(previous: Optional[float], current: Optional[float]) -> Optional[float]:
    if previous is None or current is None or previous == 0:
        return None
    return (current - previous) / abs(previous)


def _extract_base_symbol(symbol: str) -> str:
    token = (symbol or "BTC").split(":", 1)[0]
    token = token.split("/", 1)[0]
    token = token.split("-", 1)[0]
    token = token.strip().upper()
    return token or "BTC"


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


class PublicOnChainProvider(OnChainProvider):
    """
    公开 API 聚合的链上代理指标 provider。

    数据来源：
    - Blockchain.com charts: 活跃地址数 / 交易量 / 交易笔数 / hash rate
    - CoinGecko: BTC 市值、成交量、稳定币市值

    注意：这是一组无需 API Key 的 public proxy 指标，主要用于产品化默认链路。
    对于非 BTC 资产，会显式使用 BTC 宏观链上代理值，并在 metadata 中标记。
    """

    _BLOCKCHAIN_BASE = "https://api.blockchain.info/charts"
    _COINGECKO_BASE = "https://api.coingecko.com/api/v3/coins"

    def __init__(
        self,
        timeout_sec: float = 10.0,
        stablecoin_ids: Optional[list[str]] = None,
    ) -> None:
        self._timeout_sec = timeout_sec
        self._stablecoin_ids = stablecoin_ids or ["tether", "usd-coin", "dai"]
        log.info(
            "[OnChain] PublicOnChainProvider 初始化: timeout={}s stablecoins={}",
            timeout_sec,
            self._stablecoin_ids,
        )

    @property
    def provider_name(self) -> str:
        return "public"

    def fetch(self, symbol: str = "BTC") -> OnChainRecord:
        requested_symbol = _extract_base_symbol(symbol)
        proxy_symbol = "BTC"

        btc_coin = self._safe_get_coin_payload("bitcoin")
        stablecoin_caps = []
        for coin_id in self._stablecoin_ids:
            cap_payload = self._safe_get_coin_payload(coin_id)
            stablecoin_caps.append(self._extract_market_cap(cap_payload) if cap_payload else None)

        unique_addresses = self._safe_get_chart_series("n-unique-addresses")
        tx_volume_usd = self._safe_get_chart_series("estimated-transaction-volume-usd")
        tx_count = self._safe_get_chart_series("n-transactions")
        hash_rate = self._safe_get_chart_series("hash-rate")

        # 允许部分数据源失败，只要至少一部分字段可计算就继续返回，避免 collector 全量降级。
        if (
            btc_coin is None
            and unique_addresses is None
            and tx_volume_usd is None
            and tx_count is None
            and hash_rate is None
        ):
            raise OnChainFetchError("公开 onchain API 全部不可用")

        btc_market_cap = self._extract_market_cap(btc_coin) if btc_coin else None
        btc_total_volume = self._extract_total_volume(btc_coin) if btc_coin else None

        latest_active, prev_active = self._last_two(unique_addresses or [])
        latest_tx_volume, _ = self._last_two(tx_volume_usd or [])
        latest_tx_count, _ = self._last_two(tx_count or [])
        latest_hash_rate, prev_hash_rate = self._last_two(hash_rate or [])

        active_addresses_change = _pct_change(prev_active, latest_active)

        exchange_inflow_ratio = None
        if latest_tx_volume and latest_tx_volume > 0:
            exchange_inflow_ratio = _clip(
                btc_total_volume / (btc_total_volume + latest_tx_volume),
                0.0,
                1.0,
            )

        whale_tx_count_ratio = None
        if latest_tx_volume and latest_tx_count and latest_tx_count > 0:
            average_tx_value = latest_tx_volume / latest_tx_count
            whale_tx_count_ratio = _clip(average_tx_value / 1_000_000.0, 0.0, 1.0)

        stablecoin_supply_ratio = None
        stablecoin_cap_sum = sum(cap for cap in stablecoin_caps if cap is not None)
        if btc_market_cap is not None and btc_market_cap > 0:
            stablecoin_supply_ratio = stablecoin_cap_sum / btc_market_cap

        miner_reserve_change = _pct_change(prev_hash_rate, latest_hash_rate)

        nvt_proxy = None
        if latest_tx_volume and latest_tx_volume > 0 and btc_market_cap is not None:
            nvt_proxy = btc_market_cap / latest_tx_volume
        elif btc_market_cap is not None and btc_total_volume and btc_total_volume > 0:
            nvt_proxy = btc_market_cap / btc_total_volume

        fields = {
            "active_addresses_change": active_addresses_change,
            "exchange_inflow_ratio": exchange_inflow_ratio,
            "whale_tx_count_ratio": whale_tx_count_ratio,
            "stablecoin_supply_ratio": stablecoin_supply_ratio,
            "miner_reserve_change": miner_reserve_change,
            "nvt_proxy": nvt_proxy,
        }

        metadata = {
            "requested_symbol": requested_symbol,
            "proxy_symbol": proxy_symbol,
            "proxy_mode": requested_symbol != proxy_symbol,
            "stablecoin_ids": list(self._stablecoin_ids),
            "stablecoin_market_cap_usd": stablecoin_cap_sum,
            "btc_market_cap_usd": btc_market_cap,
            "btc_total_volume_usd": btc_total_volume,
            "tx_volume_usd": latest_tx_volume,
            "tx_count": latest_tx_count,
        }

        if all(value is None for value in fields.values()):
            raise OnChainFetchError("公开 onchain API 返回空字段")

        return OnChainRecord(
            fetched_at=datetime.now(tz=timezone.utc),
            fields=fields,
            source_name=self.provider_name,
            metadata=metadata,
        )

    def _safe_get_coin_payload(self, coin_id: str) -> Optional[dict[str, Any]]:
        try:
            return self._get_coin_payload(coin_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("[OnChain] CoinGecko 拉取失败: coin_id={} error={}", coin_id, exc)
            return None

    def _safe_get_chart_series(self, metric: str) -> Optional[list[dict[str, Any]]]:
        try:
            return self._get_chart_series(metric)
        except Exception as exc:  # noqa: BLE001
            log.warning("[OnChain] Blockchain.com 拉取失败: metric={} error={}", metric, exc)
            return None

    def _get_coin_payload(self, coin_id: str) -> dict[str, Any]:
        response = requests.get(
            f"{self._COINGECKO_BASE}/{coin_id}",
            params={
                "localization": "false",
                "tickers": "false",
                "market_data": "true",
                "community_data": "false",
                "developer_data": "false",
                "sparkline": "false",
            },
            timeout=self._timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"CoinGecko {coin_id} 返回非对象结构")
        return payload

    def _get_chart_series(self, metric: str) -> list[dict[str, Any]]:
        response = requests.get(
            f"{self._BLOCKCHAIN_BASE}/{metric}",
            params={"timespan": "10days", "format": "json"},
            timeout=self._timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Blockchain.com {metric} 返回非对象结构")
        values = payload.get("values") or []
        if not isinstance(values, list) or len(values) < 2:
            raise ValueError(f"Blockchain.com {metric} 数据点不足")
        return values

    @staticmethod
    def _extract_market_cap(payload: dict[str, Any]) -> Optional[float]:
        market_data = payload.get("market_data") or {}
        market_cap = market_data.get("market_cap") or {}
        return _safe_float(market_cap.get("usd"))

    @staticmethod
    def _extract_total_volume(payload: dict[str, Any]) -> Optional[float]:
        market_data = payload.get("market_data") or {}
        total_volume = market_data.get("total_volume") or {}
        return _safe_float(total_volume.get("usd"))

    @staticmethod
    def _last_two(values: list[dict[str, Any]]) -> tuple[Optional[float], Optional[float]]:
        if len(values) < 2:
            return None, None
        latest = _safe_float(values[-1].get("y"))
        previous = _safe_float(values[-2].get("y"))
        return latest, previous


class GlassnodeProvider(OnChainProvider):
    """
    Glassnode API 适配层（正式实现，需要有效 API Key）。

    数据来源：https://api.glassnode.com/v1/metrics/
    认证方式：query param ``api_key=YOUR_KEY``

    API 返回格式：``[{"t": unix_timestamp, "v": value}, ...]``

    字段映射：
        active_addresses_change   ← addresses/active_count 日频两点差分
        exchange_inflow_ratio     ← transfers_volume_to_exchanges_sum / transfers_volume_sum
        whale_tx_count_ratio      ← transfers_count_to_exchanges / count（大流量代理）
        stablecoin_supply_ratio   ← market/marketcap_usd[USDT] / market/marketcap_usd[BTC]
        miner_reserve_change      ← mining/hash_rate_mean 两点差分
        nvt_proxy                 ← indicators/nvt（直接端点，或 marketcap/tx_volume 比）

    无 API Key 时返回全字段 None（graceful degradation），不再抛出异常。
    API Key 存在但请求失败时抛出 OnChainFetchError。

    Args:
        api_key:       Glassnode API Key（通过 GLASSNODE_API_KEY 环境变量注入）
        timeout_sec:   单次请求超时（秒）
    """

    _BASE_URL = "https://api.glassnode.com/v1/metrics"

    def __init__(self, api_key: str = "", timeout_sec: float = 15.0) -> None:
        self._api_key = api_key
        self._timeout_sec = timeout_sec
        log.info(
            "[OnChain] GlassnodeProvider 初始化: has_key={} timeout={}s",
            bool(api_key),
            timeout_sec,
        )

    @property
    def provider_name(self) -> str:
        return "glassnode"

    def fetch(self, symbol: str = "BTC") -> OnChainRecord:
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

        asset = "BTC"
        base_params = {"a": asset, "api_key": self._api_key, "i": "24h", "limit": 3}

        try:
            active_addr = self._fetch_series("addresses/active_count", base_params)
            exch_volume = self._fetch_series(
                "transactions/transfers_volume_to_exchanges_sum", base_params
            )
            total_volume = self._fetch_series(
                "transactions/transfers_volume_sum", base_params
            )
            exch_tx_count = self._fetch_series(
                "transactions/transfers_count_to_exchanges", base_params
            )
            total_tx_count = self._fetch_series("transactions/count", base_params)
            hash_rate = self._fetch_series("mining/hash_rate_mean", base_params)
            btc_mktcap = self._fetch_series("market/marketcap_usd", base_params)

            usdt_params = dict(base_params)
            usdt_params["a"] = "USDT"
            try:
                usdt_mktcap = self._fetch_series("market/marketcap_usd", usdt_params)
            except OnChainFetchError:
                usdt_mktcap = []

            try:
                nvt_series = self._fetch_series("indicators/nvt", base_params)
            except OnChainFetchError:
                nvt_series = []

        except requests.RequestException as exc:
            raise OnChainFetchError(f"Glassnode API 请求失败: {exc}") from exc

        latest_active, prev_active = self._last_two_v(active_addr)
        active_addresses_change = _pct_change(prev_active, latest_active)

        latest_exch_vol = self._last_v(exch_volume)
        latest_total_vol = self._last_v(total_volume)
        exchange_inflow_ratio = None
        if latest_exch_vol is not None and latest_total_vol and latest_total_vol > 0:
            exchange_inflow_ratio = _clip(latest_exch_vol / latest_total_vol, 0.0, 1.0)

        latest_exch_tx = self._last_v(exch_tx_count)
        latest_total_tx = self._last_v(total_tx_count)
        whale_tx_count_ratio = None
        if latest_exch_tx is not None and latest_total_tx and latest_total_tx > 0:
            whale_tx_count_ratio = _clip(latest_exch_tx / latest_total_tx, 0.0, 1.0)

        latest_btc_cap = self._last_v(btc_mktcap)
        latest_usdt_cap = self._last_v(usdt_mktcap)
        stablecoin_supply_ratio = None
        if latest_usdt_cap is not None and latest_btc_cap and latest_btc_cap > 0:
            stablecoin_supply_ratio = latest_usdt_cap / latest_btc_cap

        latest_hash, prev_hash = self._last_two_v(hash_rate)
        miner_reserve_change = _pct_change(prev_hash, latest_hash)

        nvt_proxy = self._last_v(nvt_series)
        if nvt_proxy is None and latest_btc_cap and latest_total_vol and latest_total_vol > 0:
            nvt_proxy = latest_btc_cap / latest_total_vol

        fields = {
            "active_addresses_change": active_addresses_change,
            "exchange_inflow_ratio": exchange_inflow_ratio,
            "whale_tx_count_ratio": whale_tx_count_ratio,
            "stablecoin_supply_ratio": stablecoin_supply_ratio,
            "miner_reserve_change": miner_reserve_change,
            "nvt_proxy": nvt_proxy,
        }
        metadata = {
            "requested_symbol": _extract_base_symbol(symbol),
            "proxy_asset": asset,
            "btc_marketcap_usd": latest_btc_cap,
            "usdt_marketcap_usd": latest_usdt_cap,
            "total_volume_usd": latest_total_vol,
            "exchange_inflow_usd": latest_exch_vol,
        }
        log.debug("[OnChain] GlassnodeProvider.fetch symbol={} fields={}", symbol, fields)
        return OnChainRecord(
            fetched_at=now,
            fields=fields,
            source_name=self.provider_name,
            metadata=metadata,
        )

    def _fetch_series(self, endpoint: str, params: dict) -> list:
        url = f"{self._BASE_URL}/{endpoint}"
        response = requests.get(url, params=params, timeout=self._timeout_sec)
        if response.status_code == 429:
            raise OnChainFetchError(f"Glassnode API 触发限流 (429): endpoint={endpoint}")
        if response.status_code == 401:
            raise OnChainFetchError(
                f"Glassnode API 认证失败 (401): endpoint={endpoint} — 请检查 GLASSNODE_API_KEY"
            )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise OnChainFetchError(
                f"Glassnode API HTTP {response.status_code}: endpoint={endpoint}"
            ) from exc
        data = response.json()
        if not isinstance(data, list):
            raise OnChainFetchError(f"Glassnode {endpoint} 返回格式异常: {type(data)}")
        return data

    @staticmethod
    def _last_v(series: list) -> Optional[float]:
        if not series:
            return None
        return _safe_float(series[-1].get("v"))

    @staticmethod
    def _last_two_v(series: list) -> tuple:
        if len(series) < 2:
            return _safe_float(series[-1].get("v")) if series else None, None
        return _safe_float(series[-1].get("v")), _safe_float(series[-2].get("v"))


class CryptoQuantProvider(OnChainProvider):
    """
    CryptoQuant API 适配层（正式实现，需要有效 API Key）。

    数据来源：https://api.cryptoquant.com/v1/
    认证方式：HTTP header ``Authorization: Bearer {api_key}``

    API 返回格式：``{"status": "success", "result": {"data": [{...}, ...], ...}}``

    字段映射：
        active_addresses_change  ← btc/network-data/active-addresses（日频差分）
        exchange_inflow_ratio    ← btc/exchange-flows/inflow / btc/market-data/price_usd·volume
        whale_tx_count_ratio     ← btc/transactions/large-transactions-count / total-tx-count
        stablecoin_supply_ratio  ← stablecoin/all/total-supply / btc/market-data/capitalization
        miner_reserve_change     ← btc/miner-flows/miner-reserve（差分）
        nvt_proxy                ← btc/market-data/nvt-ratio（或 marketcap / tx_volume）

    无 API Key 时返回全字段 None（graceful degradation）。

    Args:
        api_key:      CryptoQuant API Key（通过 CRYPTOQUANT_API_KEY 环境变量注入）
        timeout_sec:  单次请求超时（秒）
    """

    _BASE_URL = "https://api.cryptoquant.com/v1"

    def __init__(self, api_key: str = "", timeout_sec: float = 15.0) -> None:
        self._api_key = api_key
        self._timeout_sec = timeout_sec
        log.info(
            "[OnChain] CryptoQuantProvider 初始化: has_key={} timeout={}s",
            bool(api_key),
            timeout_sec,
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

        headers = {"Authorization": f"Bearer {self._api_key}"}

        try:
            active_addr = self._fetch_data("btc/network-data/active-addresses", headers)
            exch_inflow = self._fetch_data("btc/exchange-flows/inflow", headers)
            large_tx = self._fetch_data("btc/transactions/large-transactions-count", headers)
            miner_reserve = self._fetch_data("btc/miner-flows/miner-reserve", headers)
            mkt_data = self._fetch_data("btc/market-data/price-ohlcv", headers)

            try:
                stable_supply = self._fetch_data("stablecoin/all/total-supply", headers)
            except OnChainFetchError:
                stable_supply = []

            try:
                nvt_data = self._fetch_data("btc/market-data/nvt-ratio", headers)
            except OnChainFetchError:
                nvt_data = []

        except requests.RequestException as exc:
            raise OnChainFetchError(f"CryptoQuant API 请求失败: {exc}") from exc

        def _last_field(rows: list, *fields: str) -> Optional[float]:
            for row in reversed(rows):
                if not isinstance(row, dict):
                    continue
                for field in fields:
                    v = _safe_float(row.get(field))
                    if v is not None:
                        return v
            return None

        def _prev_field(rows: list, *fields: str) -> Optional[float]:
            if len(rows) < 2:
                return None
            for row in reversed(rows[:-1]):
                if not isinstance(row, dict):
                    continue
                for field in fields:
                    v = _safe_float(row.get(field))
                    if v is not None:
                        return v
            return None

        cur_active = _last_field(active_addr, "active_addresses", "count")
        prev_active = _prev_field(active_addr, "active_addresses", "count")
        active_addresses_change = _pct_change(prev_active, cur_active)

        latest_inflow = _last_field(exch_inflow, "inflow_total", "inflow", "value")
        mktcap = _last_field(mkt_data, "capitalization", "market_cap", "close")
        exchange_inflow_ratio = None
        if latest_inflow is not None and mktcap and mktcap > 0:
            exchange_inflow_ratio = _clip(latest_inflow / (mktcap + latest_inflow), 0.0, 1.0)

        latest_large_tx = _last_field(large_tx, "large_tx_count", "count", "value")
        total_tx = _last_field(active_addr, "total_tx_count", "tx_count")
        whale_tx_count_ratio = None
        if latest_large_tx is not None and total_tx and total_tx > 0:
            whale_tx_count_ratio = _clip(latest_large_tx / total_tx, 0.0, 1.0)
        elif latest_large_tx is not None:
            whale_tx_count_ratio = _clip(latest_large_tx / 1_000_000.0, 0.0, 1.0)

        latest_stable = _last_field(stable_supply, "total_supply", "supply", "value")
        stablecoin_supply_ratio = None
        if latest_stable is not None and mktcap and mktcap > 0:
            stablecoin_supply_ratio = latest_stable / mktcap

        cur_reserve = _last_field(miner_reserve, "miner_reserve", "reserve", "value")
        prev_reserve = _prev_field(miner_reserve, "miner_reserve", "reserve", "value")
        miner_reserve_change = _pct_change(prev_reserve, cur_reserve)

        nvt_proxy = _last_field(nvt_data, "nvt_ratio", "nvt", "value")
        if nvt_proxy is None and mktcap:
            vol = _last_field(exch_inflow, "inflow_total", "inflow", "value")
            if vol and vol > 0:
                nvt_proxy = mktcap / vol

        fields = {
            "active_addresses_change": active_addresses_change,
            "exchange_inflow_ratio": exchange_inflow_ratio,
            "whale_tx_count_ratio": whale_tx_count_ratio,
            "stablecoin_supply_ratio": stablecoin_supply_ratio,
            "miner_reserve_change": miner_reserve_change,
            "nvt_proxy": nvt_proxy,
        }
        metadata = {
            "requested_symbol": _extract_base_symbol(symbol),
            "active_addresses": cur_active,
            "miner_reserve": cur_reserve,
            "stablecoin_supply": latest_stable,
        }
        log.debug("[OnChain] CryptoQuantProvider.fetch symbol={} fields={}", symbol, fields)
        return OnChainRecord(
            fetched_at=now,
            fields=fields,
            source_name=self.provider_name,
            metadata=metadata,
        )

    def _fetch_data(self, endpoint: str, headers: dict) -> list:
        url = f"{self._BASE_URL}/{endpoint}"
        response = requests.get(
            url,
            headers=headers,
            params={"window": "day", "limit": 3},
            timeout=self._timeout_sec,
        )
        if response.status_code == 429:
            raise OnChainFetchError(f"CryptoQuant API 触发限流 (429): endpoint={endpoint}")
        if response.status_code == 401:
            raise OnChainFetchError(
                f"CryptoQuant API 认证失败 (401): endpoint={endpoint} — 请检查 CRYPTOQUANT_API_KEY"
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise OnChainFetchError(f"CryptoQuant {endpoint} 返回格式异常")
        result = payload.get("result") or {}
        data = result.get("data") or payload.get("data") or []
        if not isinstance(data, list):
            raise OnChainFetchError(f"CryptoQuant {endpoint} data 字段非列表")
        return data


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