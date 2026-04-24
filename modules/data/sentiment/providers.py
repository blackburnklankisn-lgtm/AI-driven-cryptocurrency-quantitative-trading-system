"""
modules/data/sentiment/providers.py — 情绪数据 Provider 适配层

设计说明：
- 定义情绪数据 provider 的统一接口（抽象基类 SentimentProvider）
- 每个 provider 实现独立的 API 采集逻辑，输出规范化字典
- 第一版内置四个 provider：
    * AlternativeMeProvider  — Alternative.me 恐慌贪婪指数（真实实现）
    * HtxSentimentProvider   — HTX 公共衍生品情绪数据（真实实现）
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
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import ccxt
import requests

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


def _extract_metric(payload: Any, *keys: str) -> Optional[float]:
    if isinstance(payload, dict):
        for key in keys:
            value = _safe_float(payload.get(key))
            if value is not None:
                return value
    for key in keys:
        value = _safe_float(getattr(payload, key, None))
        if value is not None:
            return value
    return None


def _zscore(current: Optional[float], history: list[float]) -> Optional[float]:
    if current is None:
        return None
    series = [value for value in history if value is not None]
    if len(series) < 2:
        return 0.0
    mean = sum(series) / len(series)
    variance = sum((value - mean) ** 2 for value in series) / len(series)
    std = math.sqrt(variance)
    if std == 0:
        return 0.0
    return (current - mean) / std


def _normalize_centered(value: Optional[float], scale: float) -> Optional[float]:
    if value is None or scale <= 0:
        return None
    return _clip(0.5 + (value / (2.0 * scale)), 0.0, 1.0)


def _compute_sentiment_score(
    fear_greed_index: Optional[float],
    funding_rate_zscore: Optional[float],
    long_short_ratio_change: Optional[float],
    open_interest_change: Optional[float],
    liquidation_imbalance: Optional[float],
) -> Optional[float]:
    weighted_components: list[tuple[float, float]] = []

    if fear_greed_index is not None:
        weighted_components.append((0.30, _clip(fear_greed_index / 100.0, 0.0, 1.0)))

    funding_norm = _normalize_centered(funding_rate_zscore, 3.0)
    if funding_norm is not None:
        weighted_components.append((0.20, funding_norm))

    long_short_norm = _normalize_centered(long_short_ratio_change, 1.0)
    if long_short_norm is not None:
        weighted_components.append((0.20, long_short_norm))

    oi_norm = _normalize_centered(open_interest_change, 0.25)
    if oi_norm is not None:
        weighted_components.append((0.15, oi_norm))

    liq_norm = _normalize_centered(liquidation_imbalance, 1.0)
    if liq_norm is not None:
        weighted_components.append((0.15, liq_norm))

    if not weighted_components:
        return None

    total_weight = sum(weight for weight, _ in weighted_components)
    score = sum(weight * value for weight, value in weighted_components) / total_weight
    return round(_clip(score, 0.0, 1.0), 4)


def _extract_base_symbol(symbol: str) -> str:
    token = (symbol or "BTC").split(":", 1)[0]
    token = token.split("/", 1)[0]
    token = token.split("-", 1)[0]
    token = token.strip().upper()
    return token or "BTC"


def _to_htx_swap_symbol(symbol: str) -> str:
    raw_symbol = (symbol or "BTC").strip().upper()
    if ":" in raw_symbol and "/" in raw_symbol:
        return raw_symbol
    if "/" in raw_symbol:
        base, quote = raw_symbol.split("/", 1)
        return f"{base}/{quote}:{quote}"
    base = _extract_base_symbol(raw_symbol)
    return f"{base}/USDT:USDT"


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
    Alternative.me 恐慌贪婪指数适配层（无需 API Key）。
    """

    _URL = "https://api.alternative.me/fng/"

    def __init__(self, timeout_sec: float = 10.0) -> None:
        self._timeout_sec = timeout_sec
        log.info("[Sentiment] AlternativeMeProvider 初始化: timeout={}s", timeout_sec)

    @property
    def provider_name(self) -> str:
        return "alternative_me"

    def fetch(self, symbol: str = "BTC") -> SentimentRecord:
        try:
            response = requests.get(
                self._URL,
                params={"limit": 2},
                timeout=self._timeout_sec,
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data") or []
            if not rows:
                raise SentimentFetchError("Alternative.me 返回空数据")

            latest = rows[0]
            fear_greed = _safe_float(latest.get("value"))
            if fear_greed is None:
                raise SentimentFetchError("Alternative.me value 字段缺失或非法")
        except requests.RequestException as exc:
            raise SentimentFetchError(f"Alternative.me 请求失败: {exc}") from exc
        except ValueError as exc:
            raise SentimentFetchError(f"Alternative.me 响应解析失败: {exc}") from exc

        fields = {field_name: None for field_name in SENTIMENT_FIELDS}
        fields["fear_greed_index"] = fear_greed
        return SentimentRecord(
            fetched_at=datetime.now(tz=timezone.utc),
            fields=fields,
            source_name=self.provider_name,
            metadata={
                "symbol": _extract_base_symbol(symbol),
                "classification": latest.get("value_classification"),
                "raw_timestamp": latest.get("timestamp"),
                "rows": len(rows),
            },
        )


class HtxSentimentProvider(SentimentProvider):
    """
    HTX 公共衍生品情绪数据适配层。

    使用 CCXT 的 HTX 公共接口聚合：
    - funding rate / funding history
    - open interest / open interest history
    - perpetual OHLCV（用于方向性代理）
    - Alternative.me fear & greed（若可用）

    由于 HTX 不直接提供公开 long/short ratio 与 liquidation breakdown，
    long_short_ratio_change 和 liquidation_imbalance 使用公开 funding/OI/price
    组合构造可解释 proxy，并在 metadata 中明确标记。
    """

    def __init__(
        self,
        exchange: Optional[Any] = None,
        fear_greed_provider: Optional[SentimentProvider] = None,
        history_limit: int = 12,
    ) -> None:
        self._exchange = exchange or ccxt.htx({"enableRateLimit": True})
        self._fear_greed_provider = fear_greed_provider or AlternativeMeProvider()
        self._history_limit = max(int(history_limit), 3)
        log.info(
            "[Sentiment] HtxSentimentProvider 初始化: history_limit={} fg_provider={}",
            self._history_limit,
            getattr(self._fear_greed_provider, "provider_name", None),
        )

    @property
    def provider_name(self) -> str:
        return "htx"

    def fetch(self, symbol: str = "BTC") -> SentimentRecord:
        swap_symbol = _to_htx_swap_symbol(symbol)
        try:
            funding_now = self._exchange.fetch_funding_rate(swap_symbol)
            funding_history = self._exchange.fetch_funding_rate_history(
                swap_symbol,
                limit=self._history_limit,
            ) or []
            open_interest_now = self._exchange.fetch_open_interest(swap_symbol)
            open_interest_history = self._exchange.fetch_open_interest_history(
                swap_symbol,
                timeframe="1h",
                limit=self._history_limit,
            ) or []
            ohlcv = self._exchange.fetch_ohlcv(
                swap_symbol,
                timeframe="1h",
                limit=3,
            ) or []
        except Exception as exc:  # noqa: BLE001
            raise SentimentFetchError(f"HTX 公共情绪数据采集失败: {exc}") from exc

        fear_greed_index: Optional[float] = None
        fear_greed_meta: dict[str, Any] = {}
        if self._fear_greed_provider is not None:
            try:
                fear_greed_record = self._fear_greed_provider.fetch(symbol)
                fear_greed_index = fear_greed_record.fields.get("fear_greed_index")
                fear_greed_meta = dict(fear_greed_record.metadata)
            except SentimentFetchError as exc:
                fear_greed_meta = {"error": str(exc)}
                log.debug("[Sentiment] HTX fear&greed 子源失败，继续降级: {}", exc)

        current_funding = _extract_metric(funding_now, "fundingRate")
        funding_series = [
            value
            for value in (
                _extract_metric(entry, "fundingRate")
                for entry in funding_history
            )
            if value is not None
        ]
        if current_funding is not None:
            funding_series.append(current_funding)
        funding_rate_zscore = _zscore(current_funding, funding_series)

        current_open_interest = _extract_metric(
            open_interest_now,
            "openInterestValue",
            "quoteVolume",
            "openInterestAmount",
            "baseVolume",
        )
        open_interest_series = [
            value
            for value in (
                _extract_metric(
                    entry,
                    "openInterestValue",
                    "quoteVolume",
                    "openInterestAmount",
                    "baseVolume",
                )
                for entry in open_interest_history
            )
            if value is not None
        ]
        if current_open_interest is not None:
            open_interest_series.append(current_open_interest)

        open_interest_change = None
        if len(open_interest_series) >= 2:
            open_interest_change = _pct_change(
                open_interest_series[-2],
                open_interest_series[-1],
            )

        closes = [
            _safe_float(row[4])
            for row in ohlcv
            if isinstance(row, (list, tuple)) and len(row) >= 5
        ]
        price_change = None
        if len(closes) >= 2:
            price_change = _pct_change(closes[-2], closes[-1])

        long_short_ratio_change = None
        if price_change is not None and open_interest_change is not None:
            long_short_ratio_change = _clip(
                (price_change * 6.0) + (open_interest_change * 2.0),
                -1.0,
                1.0,
            )

        liquidation_imbalance = None
        if price_change is not None and open_interest_change is not None:
            oi_unwind = max(-open_interest_change, 0.0)
            if price_change > 0:
                direction = 1.0
            elif price_change < 0:
                direction = -1.0
            else:
                direction = 0.0
            liquidation_imbalance = _clip(direction * oi_unwind * 20.0, -1.0, 1.0)

        sentiment_score_ema = _compute_sentiment_score(
            fear_greed_index,
            funding_rate_zscore,
            long_short_ratio_change,
            open_interest_change,
            liquidation_imbalance,
        )

        fields = {
            "fear_greed_index": fear_greed_index,
            "funding_rate_zscore": funding_rate_zscore,
            "long_short_ratio_change": long_short_ratio_change,
            "open_interest_change": open_interest_change,
            "liquidation_imbalance": liquidation_imbalance,
            "sentiment_score_ema": sentiment_score_ema,
        }
        return SentimentRecord(
            fetched_at=datetime.now(tz=timezone.utc),
            fields=fields,
            source_name=self.provider_name,
            metadata={
                "symbol": swap_symbol,
                "fear_greed_provider": getattr(
                    self._fear_greed_provider,
                    "provider_name",
                    None,
                ),
                "fear_greed_metadata": fear_greed_meta,
                "price_change_1h": price_change,
                "funding_history_points": len(funding_series),
                "open_interest_points": len(open_interest_series),
                "long_short_ratio_proxy": True,
                "liquidation_proxy": True,
            },
        )


class CryptoCompareProvider(SentimentProvider):
    """
    CryptoCompare API 适配层（正式实现，需要有效 API Key）。

    数据来源：
        - https://min-api.cryptocompare.com/data/ （传统 min-api）
        - https://data-api.cryptocompare.com/     （新版 data-api，用于衍生品数据）

    认证方式：HTTP header ``Authorization: Apikey {api_key}``

    字段映射：
        fear_greed_index        ← social/coin/latest 的 SentimentScore * 100（代理）
        funding_rate_zscore     ← futures/v1/funding-rates/by-exchange 的最新资金费率 z-score
        long_short_ratio_change ← 多个交易所持仓量差分的综合 proxy
        open_interest_change    ← futures/v1/open-interest 差分
        liquidation_imbalance   ← price_change + open_interest_change 构建 proxy
        sentiment_score_ema     ← 综合加权评分

    无 API Key 时返回全字段 None（graceful degradation）。

    Args:
        api_key:        CryptoCompare API Key（通过 CRYPTOCOMPARE_API_KEY 环境变量注入）
        exchange:       衍生品数据交易所（默认 Binance）
        timeout_sec:    单次请求超时（秒）
        history_limit:  历史窗口点数（用于 z-score 计算）
    """

    _MINAPI_BASE = "https://min-api.cryptocompare.com/data"
    _DATAAPI_BASE = "https://data-api.cryptocompare.com"

    def __init__(
        self,
        api_key: str = "",
        exchange: str = "Binance",
        timeout_sec: float = 15.0,
        history_limit: int = 12,
    ) -> None:
        self._api_key = api_key
        self._exchange = exchange
        self._timeout_sec = timeout_sec
        self._history_limit = max(int(history_limit), 3)
        log.info(
            "[Sentiment] CryptoCompareProvider 初始化: has_key={} exchange={} timeout={}s",
            bool(api_key),
            exchange,
            timeout_sec,
        )

    @property
    def provider_name(self) -> str:
        return "cryptocompare"

    def fetch(self, symbol: str = "BTC") -> SentimentRecord:
        now = datetime.now(tz=timezone.utc)
        if not self._api_key:
            log.warning("[Sentiment] CryptoCompareProvider 无 API Key，返回降级空数据")
            return SentimentRecord(
                fetched_at=now,
                fields={f: None for f in SENTIMENT_FIELDS},
                source_name=self.provider_name,
                metadata={"degraded": True, "reason": "no_api_key"},
            )

        base_symbol = _extract_base_symbol(symbol)
        headers = {"Authorization": f"Apikey {self._api_key}"}

        # --- 1. Social / news sentiment score (fear-greed proxy) ---
        fear_greed_index: Optional[float] = None
        social_meta: dict = {}
        try:
            social = self._get_minapi(
                "/social/coin/latest",
                params={"coinId": self._coin_id(base_symbol)},
                headers=headers,
            )
            data_node = social.get("Data") or {}
            sentiment_score = _extract_metric(data_node, "SentimentScore", "sentiment_score")
            if sentiment_score is not None:
                fear_greed_index = _clip(float(sentiment_score) * 100.0, 0.0, 100.0)
            social_meta = {
                "coin_id": self._coin_id(base_symbol),
                "raw_sentiment_score": sentiment_score,
            }
        except (SentimentFetchError, KeyError, TypeError) as exc:
            log.debug("[Sentiment] CryptoCompare social 数据失败，降级: {}", exc)

        # --- 2. Derivatives: funding rates ---
        funding_rate_zscore: Optional[float] = None
        funding_meta: dict = {}
        try:
            instrument = self._perp_instrument(base_symbol)
            fr_data = self._get_dataapi(
                "/futures/v1/funding-rates/by-exchange",
                params={
                    "market": self._exchange.upper(),
                    "instrument": instrument,
                    "limit": self._history_limit,
                },
                headers=headers,
            )
            fr_list = fr_data.get("Data") or []
            fr_rates = [
                _safe_float(entry.get("FUNDING_RATE") or entry.get("funding_rate"))
                for entry in fr_list
                if isinstance(entry, dict)
            ]
            fr_rates = [r for r in fr_rates if r is not None]
            current_fr = fr_rates[-1] if fr_rates else None
            funding_rate_zscore = _zscore(current_fr, fr_rates)
            funding_meta = {
                "instrument": instrument,
                "exchange": self._exchange,
                "funding_rate_points": len(fr_rates),
                "current_funding_rate": current_fr,
            }
        except (SentimentFetchError, KeyError, TypeError) as exc:
            log.debug("[Sentiment] CryptoCompare 资金费率数据失败，降级: {}", exc)

        # --- 3. Open Interest (history for change calculation) ---
        open_interest_change: Optional[float] = None
        oi_meta: dict = {}
        try:
            instrument = self._perp_instrument(base_symbol)
            oi_data = self._get_dataapi(
                "/futures/v1/open-interest/history/days",
                params={
                    "market": self._exchange.upper(),
                    "instrument": instrument,
                    "limit": self._history_limit,
                },
                headers=headers,
            )
            oi_list = oi_data.get("Data") or []
            oi_values = [
                _safe_float(
                    entry.get("OPEN_INTEREST_QUOTE")
                    or entry.get("OPEN_INTEREST")
                    or entry.get("open_interest")
                )
                for entry in oi_list
                if isinstance(entry, dict)
            ]
            oi_values = [v for v in oi_values if v is not None]
            if len(oi_values) >= 2:
                open_interest_change = _pct_change(oi_values[-2], oi_values[-1])
            oi_meta = {
                "open_interest_points": len(oi_values),
                "latest_oi": oi_values[-1] if oi_values else None,
            }
        except (SentimentFetchError, KeyError, TypeError) as exc:
            log.debug("[Sentiment] CryptoCompare OI 数据失败，降级: {}", exc)

        # --- 4. Price OHLCV for direction proxy ---
        price_change: Optional[float] = None
        try:
            ohlcv = self._get_minapi(
                "/v2/histohour",
                params={"fsym": base_symbol, "tsym": "USD", "limit": 3},
                headers=headers,
            )
            ohlcv_rows = (ohlcv.get("Data") or {}).get("Data") or ohlcv.get("Data") or []
            closes = [_safe_float(r.get("close")) for r in ohlcv_rows if isinstance(r, dict)]
            closes = [c for c in closes if c is not None]
            if len(closes) >= 2:
                price_change = _pct_change(closes[-2], closes[-1])
        except (SentimentFetchError, KeyError, TypeError) as exc:
            log.debug("[Sentiment] CryptoCompare OHLCV 数据失败，降级: {}", exc)

        # --- 5. Derive remaining fields from available data ---
        long_short_ratio_change: Optional[float] = None
        if price_change is not None and open_interest_change is not None:
            long_short_ratio_change = _clip(
                (price_change * 6.0) + (open_interest_change * 2.0), -1.0, 1.0
            )

        liquidation_imbalance: Optional[float] = None
        if price_change is not None and open_interest_change is not None:
            oi_unwind = max(-open_interest_change, 0.0)
            direction = 1.0 if price_change > 0 else (-1.0 if price_change < 0 else 0.0)
            liquidation_imbalance = _clip(direction * oi_unwind * 20.0, -1.0, 1.0)

        sentiment_score_ema = _compute_sentiment_score(
            fear_greed_index,
            funding_rate_zscore,
            long_short_ratio_change,
            open_interest_change,
            liquidation_imbalance,
        )

        fields = {
            "fear_greed_index": fear_greed_index,
            "funding_rate_zscore": funding_rate_zscore,
            "long_short_ratio_change": long_short_ratio_change,
            "open_interest_change": open_interest_change,
            "liquidation_imbalance": liquidation_imbalance,
            "sentiment_score_ema": sentiment_score_ema,
        }
        log.debug("[Sentiment] CryptoCompareProvider.fetch symbol={} fields={}", symbol, fields)
        return SentimentRecord(
            fetched_at=now,
            fields=fields,
            source_name=self.provider_name,
            metadata={
                "symbol": base_symbol,
                "exchange": self._exchange,
                "social": social_meta,
                "funding": funding_meta,
                "open_interest": oi_meta,
                "price_change_1h": price_change,
                "long_short_ratio_proxy": True,
                "liquidation_proxy": True,
            },
        )

    def _get_minapi(self, path: str, params: dict, headers: dict) -> dict:
        url = f"{self._MINAPI_BASE}{path}"
        response = requests.get(url, params=params, headers=headers, timeout=self._timeout_sec)
        self._check_response(response, path)
        payload = response.json()
        if not isinstance(payload, dict):
            raise SentimentFetchError(f"CryptoCompare min-api {path} 返回格式异常")
        if payload.get("Response") == "Error":
            raise SentimentFetchError(
                f"CryptoCompare min-api {path} 返回错误: {payload.get('Message')}"
            )
        return payload

    def _get_dataapi(self, path: str, params: dict, headers: dict) -> dict:
        url = f"{self._DATAAPI_BASE}{path}"
        response = requests.get(url, params=params, headers=headers, timeout=self._timeout_sec)
        self._check_response(response, path)
        payload = response.json()
        if not isinstance(payload, dict):
            raise SentimentFetchError(f"CryptoCompare data-api {path} 返回格式异常")
        return payload

    @staticmethod
    def _check_response(response, path: str) -> None:
        if response.status_code == 429:
            raise SentimentFetchError(f"CryptoCompare API 触发限流 (429): path={path}")
        if response.status_code in (401, 403):
            raise SentimentFetchError(
                f"CryptoCompare API 认证失败 ({response.status_code}): path={path}"
                " — 请检查 CRYPTOCOMPARE_API_KEY"
            )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise SentimentFetchError(
                f"CryptoCompare API HTTP {response.status_code}: path={path}"
            ) from exc

    @staticmethod
    def _coin_id(symbol: str) -> int:
        _coin_ids = {
            "BTC": 1182,
            "ETH": 7605,
            "SOL": 5426,
            "BNB": 4030,
            "XRP": 5031,
            "ADA": 321992,
            "DOGE": 4432,
            "MATIC": 3890,
        }
        return _coin_ids.get(symbol.upper(), 1182)

    @staticmethod
    def _perp_instrument(symbol: str) -> str:
        return f"{symbol.upper()}USDT_PERP"


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