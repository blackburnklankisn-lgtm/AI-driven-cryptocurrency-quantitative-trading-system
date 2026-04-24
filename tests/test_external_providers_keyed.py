"""
tests/test_external_providers_keyed.py — 带密钥外部数据 provider 真实实现合约测试

覆盖项：
1. GlassnodeProvider
   - 无 key 时返回降级空数据（不抛出异常）
   - 有 key 时调用正确 endpoint 并正确映射字段
   - API 返回 429 时抛出 OnChainFetchError
   - API 返回 401 时抛出 OnChainFetchError（含提示检查 key）
   - 字段计算：active_addresses_change / exchange_inflow_ratio / nvt_proxy 等
2. CryptoQuantProvider
   - 无 key 时返回降级空数据
   - 有 key 时调用 Bearer auth 并正确映射字段
   - API 返回 429 / 401 时抛出 OnChainFetchError
3. CryptoCompareProvider（Sentiment）
   - 无 key 时返回降级空数据
   - 有 key 时调用 Apikey auth 并正确映射字段
   - social / funding / OI 任一子请求失败时继续降级（不全部失败）
   - fear_greed_index 从 SentimentScore 正确映射到 [0, 100]
   - funding_rate_zscore 从 funding rate history 正确计算
   - open_interest_change 从 OI history 差分计算
   - sentiment_score_ema 综合权重计算
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from modules.data.onchain.providers import (
    GlassnodeProvider,
    CryptoQuantProvider,
    OnChainFetchError,
    ONCHAIN_FIELDS,
)
from modules.data.sentiment.providers import (
    CryptoCompareProvider,
    SentimentFetchError,
    SENTIMENT_FIELDS,
)


# ══════════════════════════════════════════════════════════════
# 辅助工厂
# ══════════════════════════════════════════════════════════════

def _mock_response(status_code: int = 200, json_data: Any = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.raise_for_status.side_effect = None if status_code < 400 else _http_error(status_code)
    return resp


def _http_error(status_code: int):
    import requests
    def _raise():
        err = requests.HTTPError(response=MagicMock(status_code=status_code))
        raise err
    return _raise


def _gl_series(values: list[float]) -> list[dict]:
    return [{"t": 1700000000 + i * 86400, "v": v} for i, v in enumerate(values)]


def _cq_payload(data: list[dict]) -> dict:
    return {"status": "success", "result": {"data": data}}


def _cc_social_payload(sentiment_score: float) -> dict:
    return {"Response": "Success", "Data": {"SentimentScore": sentiment_score}}


def _cc_funding_payload(rates: list[float]) -> dict:
    return {
        "Data": [{"FUNDING_RATE": r, "TIMESTAMP": 1700000000 + i * 3600}
                 for i, r in enumerate(rates)]
    }


def _cc_oi_payload(values: list[float]) -> dict:
    return {
        "Data": [{"OPEN_INTEREST_QUOTE": v, "TIMESTAMP": 1700000000 + i * 86400}
                 for i, v in enumerate(values)]
    }


def _cc_ohlcv_payload(closes: list[float]) -> dict:
    return {
        "Data": {
            "Data": [{"close": c, "time": 1700000000 + i * 3600} for i, c in enumerate(closes)]
        }
    }


# ══════════════════════════════════════════════════════════════
# 1. GlassnodeProvider
# ══════════════════════════════════════════════════════════════

class TestGlassnodeProvider:

    def test_no_api_key_returns_degraded_all_none(self):
        provider = GlassnodeProvider(api_key="")
        record = provider.fetch("BTC/USDT")
        assert record.source_name == "glassnode"
        assert record.metadata.get("degraded") is True
        for field in ONCHAIN_FIELDS:
            assert record.fields[field] is None

    def test_fetch_calls_correct_endpoints_with_key(self):
        provider = GlassnodeProvider(api_key="test-key")
        responses = {
            "addresses/active_count": _gl_series([900_000, 920_000]),
            "transactions/transfers_volume_to_exchanges_sum": _gl_series([5e8, 5.2e8]),
            "transactions/transfers_volume_sum": _gl_series([2e9, 2.1e9]),
            "transactions/transfers_count_to_exchanges": _gl_series([8000, 8200]),
            "transactions/count": _gl_series([300_000, 310_000]),
            "mining/hash_rate_mean": _gl_series([400e6, 410e6]),
            "market/marketcap_usd": _gl_series([5e11, 5.1e11]),
            "indicators/nvt": _gl_series([25.0, 26.0]),
        }

        def side_effect(url, params, timeout):
            for key, data in responses.items():
                if key in url:
                    return _mock_response(200, data)
            # USDT marketcap
            if "USDT" in str(params.get("a", "")):
                return _mock_response(200, _gl_series([8e10, 8.1e10]))
            return _mock_response(200, [])

        with patch("requests.get", side_effect=side_effect):
            record = provider.fetch("BTC/USDT")

        assert record.source_name == "glassnode"
        assert set(record.fields.keys()) == set(ONCHAIN_FIELDS)

        aac = record.fields["active_addresses_change"]
        assert aac is not None
        assert abs(aac - (920_000 - 900_000) / 900_000) < 1e-6

        eir = record.fields["exchange_inflow_ratio"]
        assert eir is not None
        assert 0.0 <= eir <= 1.0

        nvt = record.fields["nvt_proxy"]
        assert nvt is not None
        assert nvt == pytest.approx(26.0)

    def test_fetch_429_raises_onchain_error(self):
        provider = GlassnodeProvider(api_key="key")
        with patch("requests.get", return_value=_mock_response(429)):
            with pytest.raises(OnChainFetchError, match="限流"):
                provider.fetch("BTC")

    def test_fetch_401_raises_onchain_error_with_hint(self):
        provider = GlassnodeProvider(api_key="bad-key")
        with patch("requests.get", return_value=_mock_response(401)):
            with pytest.raises(OnChainFetchError, match="GLASSNODE_API_KEY"):
                provider.fetch("BTC")

    def test_miner_reserve_change_computed_from_hashrate_series(self):
        provider = GlassnodeProvider(api_key="key")
        prev_hash, cur_hash = 400e6, 440e6

        def side_effect(url, params, timeout):
            if "hash_rate" in url:
                return _mock_response(200, _gl_series([prev_hash, cur_hash]))
            if "USDT" in str(params.get("a", "")):
                return _mock_response(200, [])
            return _mock_response(200, _gl_series([1.0, 1.0]))

        with patch("requests.get", side_effect=side_effect):
            record = provider.fetch("BTC")

        mrc = record.fields["miner_reserve_change"]
        assert mrc is not None
        assert mrc == pytest.approx((cur_hash - prev_hash) / prev_hash)

    def test_stablecoin_supply_ratio_from_usdt_vs_btc_mktcap(self):
        provider = GlassnodeProvider(api_key="key")
        btc_cap, usdt_cap = 5e11, 8e10

        def side_effect(url, params, timeout):
            if "market/marketcap_usd" in url:
                if params.get("a") == "USDT":
                    return _mock_response(200, _gl_series([usdt_cap, usdt_cap]))
                return _mock_response(200, _gl_series([btc_cap, btc_cap]))
            return _mock_response(200, _gl_series([1.0, 1.0]))

        with patch("requests.get", side_effect=side_effect):
            record = provider.fetch("BTC")

        ssr = record.fields["stablecoin_supply_ratio"]
        assert ssr is not None
        assert ssr == pytest.approx(usdt_cap / btc_cap)

    def test_nvt_fallback_computed_when_endpoint_unavailable(self):
        provider = GlassnodeProvider(api_key="key")
        btc_cap, total_vol = 5e11, 2e10

        def side_effect(url, params, timeout):
            if "indicators/nvt" in url:
                resp = MagicMock()
                resp.status_code = 403
                import requests
                resp.raise_for_status.side_effect = requests.HTTPError()
                return resp
            if "market/marketcap_usd" in url:
                if params.get("a") == "USDT":
                    return _mock_response(200, [])
                return _mock_response(200, _gl_series([btc_cap, btc_cap]))
            if "transfers_volume_sum" in url:
                return _mock_response(200, _gl_series([total_vol, total_vol]))
            return _mock_response(200, _gl_series([1.0, 1.0]))

        with patch("requests.get", side_effect=side_effect):
            record = provider.fetch("BTC")

        nvt = record.fields["nvt_proxy"]
        assert nvt is not None
        assert nvt == pytest.approx(btc_cap / total_vol)


# ══════════════════════════════════════════════════════════════
# 2. CryptoQuantProvider
# ══════════════════════════════════════════════════════════════

class TestCryptoQuantProvider:

    def test_no_api_key_returns_degraded_all_none(self):
        provider = CryptoQuantProvider(api_key="")
        record = provider.fetch("BTC/USDT")
        assert record.source_name == "cryptoquant"
        assert record.metadata.get("degraded") is True
        for field in ONCHAIN_FIELDS:
            assert record.fields[field] is None

    def test_fetch_sends_bearer_auth_header(self):
        provider = CryptoQuantProvider(api_key="my-cq-key")
        received_headers: list = []

        def side_effect(url, headers, params, timeout):
            received_headers.append(headers)
            return _mock_response(200, _cq_payload([
                {"active_addresses": 900_000, "inflow_total": 1e8, "close": 5e11},
                {"active_addresses": 920_000, "inflow_total": 1.1e8, "close": 5.1e11},
            ]))

        with patch("requests.get", side_effect=side_effect):
            record = provider.fetch("BTC")

        assert record.source_name == "cryptoquant"
        assert all(h.get("Authorization") == "Bearer my-cq-key" for h in received_headers)

    def test_active_addresses_change_computed_from_two_rows(self):
        provider = CryptoQuantProvider(api_key="key")
        prev, cur = 900_000, 945_000

        def side_effect(url, headers, params, timeout):
            data = [
                {"active_addresses": prev, "inflow_total": 5e8, "close": 5e11,
                 "miner_reserve": 1e6, "large_tx_count": 50},
                {"active_addresses": cur, "inflow_total": 5e8, "close": 5e11,
                 "miner_reserve": 1e6, "large_tx_count": 50},
            ]
            return _mock_response(200, _cq_payload(data))

        with patch("requests.get", side_effect=side_effect):
            record = provider.fetch("BTC")

        aac = record.fields["active_addresses_change"]
        assert aac is not None
        assert aac == pytest.approx((cur - prev) / prev)

    def test_fetch_429_raises_onchain_error(self):
        provider = CryptoQuantProvider(api_key="key")

        def side_effect(url, headers, params, timeout):
            return _mock_response(429)

        with patch("requests.get", side_effect=side_effect):
            with pytest.raises(OnChainFetchError, match="限流"):
                provider.fetch("BTC")

    def test_fetch_401_raises_onchain_error_with_hint(self):
        provider = CryptoQuantProvider(api_key="bad")

        def side_effect(url, headers, params, timeout):
            return _mock_response(401)

        with patch("requests.get", side_effect=side_effect):
            with pytest.raises(OnChainFetchError, match="CRYPTOQUANT_API_KEY"):
                provider.fetch("BTC")

    def test_miner_reserve_change_computed_from_reserve_series(self):
        provider = CryptoQuantProvider(api_key="key")
        prev_r, cur_r = 1_000_000, 1_050_000

        def side_effect(url, headers, params, timeout):
            data = [
                {"active_addresses": 900_000, "inflow_total": 5e8, "close": 5e11,
                 "miner_reserve": prev_r, "large_tx_count": 50},
                {"active_addresses": 920_000, "inflow_total": 5e8, "close": 5e11,
                 "miner_reserve": cur_r, "large_tx_count": 50},
            ]
            return _mock_response(200, _cq_payload(data))

        with patch("requests.get", side_effect=side_effect):
            record = provider.fetch("BTC")

        mrc = record.fields["miner_reserve_change"]
        assert mrc is not None
        assert mrc == pytest.approx((cur_r - prev_r) / prev_r)

    def test_exchange_inflow_ratio_clipped_0_to_1(self):
        provider = CryptoQuantProvider(api_key="key")

        def side_effect(url, headers, params, timeout):
            data = [{"active_addresses": 900_000, "inflow_total": 1e13, "close": 5e11,
                     "miner_reserve": 1e6, "large_tx_count": 50}]
            return _mock_response(200, _cq_payload(data))

        with patch("requests.get", side_effect=side_effect):
            record = provider.fetch("BTC")

        eir = record.fields["exchange_inflow_ratio"]
        assert eir is not None
        assert 0.0 <= eir <= 1.0


# ══════════════════════════════════════════════════════════════
# 3. CryptoCompareProvider (Sentiment)
# ══════════════════════════════════════════════════════════════

class TestCryptoCompareProvider:

    def test_no_api_key_returns_degraded_all_none(self):
        provider = CryptoCompareProvider(api_key="")
        record = provider.fetch("BTC/USDT")
        assert record.source_name == "cryptocompare"
        assert record.metadata.get("degraded") is True
        for field in SENTIMENT_FIELDS:
            assert record.fields[field] is None

    def test_fear_greed_from_social_sentiment_score(self):
        provider = CryptoCompareProvider(api_key="cc-key")
        raw_score = 0.72

        def side_effect(url, params, headers, timeout):
            if "/social/coin/latest" in url:
                return _mock_response(200, _cc_social_payload(raw_score))
            if "/funding-rates" in url:
                return _mock_response(200, _cc_funding_payload([0.001, 0.0012, 0.0009]))
            if "/open-interest" in url:
                return _mock_response(200, _cc_oi_payload([1e9, 1.05e9, 1.02e9]))
            if "/histohour" in url:
                return _mock_response(200, _cc_ohlcv_payload([30000.0, 30300.0, 30200.0]))
            return _mock_response(200, {})

        with patch("requests.get", side_effect=side_effect):
            record = provider.fetch("BTC/USDT")

        assert record.source_name == "cryptocompare"
        fg = record.fields["fear_greed_index"]
        assert fg is not None
        assert fg == pytest.approx(raw_score * 100.0)
        assert 0.0 <= fg <= 100.0

    def test_funding_rate_zscore_computed_from_history(self):
        provider = CryptoCompareProvider(api_key="cc-key")
        rates = [0.001, 0.002, 0.001, 0.003, 0.002, 0.001, 0.004]

        def side_effect(url, params, headers, timeout):
            if "/social/coin/latest" in url:
                return _mock_response(200, _cc_social_payload(0.5))
            if "/funding-rates" in url:
                return _mock_response(200, _cc_funding_payload(rates))
            if "/open-interest" in url:
                return _mock_response(200, _cc_oi_payload([1e9, 1.05e9]))
            if "/histohour" in url:
                return _mock_response(200, _cc_ohlcv_payload([30000.0, 30100.0]))
            return _mock_response(200, {})

        with patch("requests.get", side_effect=side_effect):
            record = provider.fetch("BTC")

        fz = record.fields["funding_rate_zscore"]
        assert fz is not None
        mean = sum(rates) / len(rates)
        variance = sum((r - mean) ** 2 for r in rates) / len(rates)
        import math
        std = math.sqrt(variance)
        expected_zscore = (rates[-1] - mean) / std
        assert fz == pytest.approx(expected_zscore, rel=1e-4)

    def test_open_interest_change_from_oi_history(self):
        provider = CryptoCompareProvider(api_key="cc-key")
        oi_values = [1_000_000_000.0, 1_050_000_000.0, 1_020_000_000.0]

        def side_effect(url, params, headers, timeout):
            if "/social/coin/latest" in url:
                return _mock_response(200, _cc_social_payload(0.5))
            if "/funding-rates" in url:
                return _mock_response(200, _cc_funding_payload([0.001]))
            if "/open-interest" in url:
                return _mock_response(200, _cc_oi_payload(oi_values))
            if "/histohour" in url:
                return _mock_response(200, _cc_ohlcv_payload([30000.0, 30100.0]))
            return _mock_response(200, {})

        with patch("requests.get", side_effect=side_effect):
            record = provider.fetch("BTC")

        oic = record.fields["open_interest_change"]
        assert oic is not None
        expected = (oi_values[-1] - oi_values[-2]) / abs(oi_values[-2])
        assert oic == pytest.approx(expected, rel=1e-4)

    def test_social_failure_degrades_gracefully_other_fields_still_computed(self):
        provider = CryptoCompareProvider(api_key="cc-key")

        def side_effect(url, params, headers, timeout):
            if "/social/coin/latest" in url:
                return _mock_response(500, {"Response": "Error", "Message": "server error"})
            if "/funding-rates" in url:
                return _mock_response(200, _cc_funding_payload([0.001, 0.002]))
            if "/open-interest" in url:
                return _mock_response(200, _cc_oi_payload([1e9, 1.1e9]))
            if "/histohour" in url:
                return _mock_response(200, _cc_ohlcv_payload([30000.0, 30300.0]))
            return _mock_response(200, {})

        with patch("requests.get", side_effect=side_effect):
            record = provider.fetch("BTC")

        assert record.fields["fear_greed_index"] is None
        assert record.fields["funding_rate_zscore"] is not None
        assert record.fields["open_interest_change"] is not None

    def test_funding_failure_degrades_gracefully_social_still_present(self):
        provider = CryptoCompareProvider(api_key="cc-key")

        def side_effect(url, params, headers, timeout):
            if "/social/coin/latest" in url:
                return _mock_response(200, _cc_social_payload(0.65))
            if "/funding-rates" in url:
                return _mock_response(429)
            if "/open-interest" in url:
                return _mock_response(200, _cc_oi_payload([1e9, 1.1e9]))
            if "/histohour" in url:
                return _mock_response(200, _cc_ohlcv_payload([30000.0, 30300.0]))
            return _mock_response(200, {})

        with patch("requests.get", side_effect=side_effect):
            record = provider.fetch("BTC")

        assert record.fields["fear_greed_index"] == pytest.approx(65.0)
        assert record.fields["funding_rate_zscore"] is None

    def test_fetch_sends_apikey_auth_header(self):
        provider = CryptoCompareProvider(api_key="my-cc-key")
        received_headers: list = []

        def side_effect(url, params, headers, timeout):
            received_headers.append(dict(headers))
            return _mock_response(200, _cc_social_payload(0.5) if "/social/" in url
                                  else _cc_funding_payload([0.001]) if "/funding" in url
                                  else _cc_oi_payload([1e9, 1.1e9]) if "/open-interest" in url
                                  else _cc_ohlcv_payload([30000.0, 30100.0]))

        with patch("requests.get", side_effect=side_effect):
            provider.fetch("BTC")

        for h in received_headers:
            assert h.get("Authorization") == "Apikey my-cc-key"

    def test_sentiment_score_ema_not_none_when_fear_greed_and_oi_present(self):
        provider = CryptoCompareProvider(api_key="cc-key")

        def side_effect(url, params, headers, timeout):
            if "/social/coin/latest" in url:
                return _mock_response(200, _cc_social_payload(0.6))
            if "/funding-rates" in url:
                return _mock_response(200, _cc_funding_payload([0.001, 0.0015]))
            if "/open-interest" in url:
                return _mock_response(200, _cc_oi_payload([1e9, 1.05e9]))
            if "/histohour" in url:
                return _mock_response(200, _cc_ohlcv_payload([30000.0, 30300.0]))
            return _mock_response(200, {})

        with patch("requests.get", side_effect=side_effect):
            record = provider.fetch("BTC")

        sse = record.fields["sentiment_score_ema"]
        assert sse is not None
        assert 0.0 <= sse <= 1.0

    def test_perp_instrument_name_format(self):
        assert CryptoCompareProvider._perp_instrument("BTC") == "BTCUSDT_PERP"
        assert CryptoCompareProvider._perp_instrument("eth") == "ETHUSDT_PERP"

    def test_coin_id_btc_returns_1182(self):
        assert CryptoCompareProvider._coin_id("BTC") == 1182

    def test_coin_id_eth_returns_7605(self):
        assert CryptoCompareProvider._coin_id("ETH") == 7605

    def test_coin_id_unknown_falls_back_to_btc(self):
        assert CryptoCompareProvider._coin_id("UNKNOWNCOIN") == 1182
