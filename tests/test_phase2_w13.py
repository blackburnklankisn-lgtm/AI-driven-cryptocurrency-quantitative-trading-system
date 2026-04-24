"""
tests/test_phase2_w13.py — Phase 2 W13 情绪数据层单元测试

覆盖项：
- SentimentProviders: MockSentimentProvider 正常采集/失败/字段缺失
- SentimentCache: write/read/read_field/evaluate_freshness/clear/wipe
- SentimentCollector: 正常采集、freshness-aware 跳过、失败后从缓存降级
- SentimentFeatureBuilder: 特征变换、归一化、clip、缺失字段 NaN
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from modules.data.fusion.freshness import FreshnessConfig
from modules.data.fusion.source_contract import FreshnessStatus, SourceFrame
from modules.data.sentiment.cache import SentimentCache
from modules.data.sentiment.collector import SentimentCollector, SentimentCollectorConfig
from modules.data.sentiment.feature_builder import (
    SentimentFeatureBuilder,
    SentimentFeatureBuilderConfig,
)
from modules.data.sentiment.providers import (
    AlternativeMeProvider,
    HtxSentimentProvider,
    MockSentimentProvider,
    SentimentFetchError,
    SentimentRecord,
    SENTIMENT_FIELDS,
)


# ─────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def make_kline_index(n: int = 24, freq: str = "1h") -> pd.DatetimeIndex:
    end = pd.Timestamp.now(tz="UTC").floor("h")
    return pd.date_range(end=end, periods=n, freq=freq, tz="UTC")


def make_sentiment_record(
    missing: list[str] | None = None,
) -> SentimentRecord:
    missing = missing or []
    fields = {
        "fear_greed_index": 52.0,
        "funding_rate_zscore": 0.8,
        "long_short_ratio_change": 0.03,
        "open_interest_change": -0.05,
        "liquidation_imbalance": 0.2,
        "sentiment_score_ema": 0.6,
    }
    for f in missing:
        fields[f] = None
    return SentimentRecord(
        fetched_at=now_utc(),
        fields=fields,
        source_name="test_provider",
    )


# ─────────────────────────────────────────────────────────────
# MockSentimentProvider 测试
# ─────────────────────────────────────────────────────────────

class TestMockSentimentProvider:
    def test_fetch_returns_all_fields(self):
        provider = MockSentimentProvider(seed=42)
        record = provider.fetch("BTC")
        for f in SENTIMENT_FIELDS:
            assert f in record.fields
            assert record.fields[f] is not None

    def test_fetch_with_missing_fields(self):
        provider = MockSentimentProvider(
            missing_fields=["fear_greed_index", "funding_rate_zscore"]
        )
        record = provider.fetch("BTC")
        assert record.fields["fear_greed_index"] is None
        assert record.fields["funding_rate_zscore"] is None
        assert record.fields["sentiment_score_ema"] is not None

    def test_fetch_with_fail_rate_1_raises(self):
        provider = MockSentimentProvider(fail_rate=1.0)
        with pytest.raises(SentimentFetchError):
            provider.fetch("BTC")

    def test_seed_deterministic(self):
        p1 = MockSentimentProvider(seed=77)
        p2 = MockSentimentProvider(seed=77)
        r1 = p1.fetch()
        r2 = p2.fetch()
        assert r1.fields == r2.fields

    def test_provider_name(self):
        assert MockSentimentProvider().provider_name == "mock_sentiment"

    def test_has_all_fields(self):
        record = MockSentimentProvider().fetch()
        assert record.has_all_fields() is True

    def test_missing_fields_listed(self):
        provider = MockSentimentProvider(missing_fields=["fear_greed_index"])
        record = provider.fetch()
        assert "fear_greed_index" in record.missing_fields()

    def test_fear_greed_in_valid_range(self):
        provider = MockSentimentProvider(seed=1)
        for _ in range(10):
            record = provider.fetch()
            fg = record.fields["fear_greed_index"]
            assert 0.0 <= fg <= 100.0

    def test_funding_rate_zscore_in_valid_range(self):
        provider = MockSentimentProvider(seed=2)
        for _ in range(10):
            record = provider.fetch()
            fr = record.fields["funding_rate_zscore"]
            assert -3.0 <= fr <= 3.0

    def test_sentiment_ema_in_valid_range(self):
        provider = MockSentimentProvider(seed=3)
        for _ in range(10):
            record = provider.fetch()
            ema = record.fields["sentiment_score_ema"]
            assert 0.0 <= ema <= 1.0


class TestRealSentimentProviders:
    def test_alternative_me_fetch_parses_payload(self):
        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "data": [
                        {
                            "value": "39",
                            "value_classification": "Fear",
                            "timestamp": "1776988800",
                        }
                    ]
                }

        provider = AlternativeMeProvider(timeout_sec=1.0)
        with patch("modules.data.sentiment.providers.requests.get", return_value=_Resp()):
            record = provider.fetch("BTC")

        assert record.source_name == "alternative_me"
        assert record.fields["fear_greed_index"] == pytest.approx(39.0)
        assert record.metadata["classification"] == "Fear"

    def test_htx_provider_builds_sentiment_bundle(self):
        class _StubFearGreed:
            provider_name = "alternative_me"

            def fetch(self, symbol: str = "BTC") -> SentimentRecord:
                return SentimentRecord(
                    fetched_at=now_utc(),
                    fields={
                        "fear_greed_index": 60.0,
                        "funding_rate_zscore": None,
                        "long_short_ratio_change": None,
                        "open_interest_change": None,
                        "liquidation_imbalance": None,
                        "sentiment_score_ema": None,
                    },
                    source_name="alternative_me",
                )

        class _StubExchange:
            def fetch_funding_rate(self, symbol: str):
                assert symbol == "BTC/USDT:USDT"
                return {"fundingRate": 0.00012}

            def fetch_funding_rate_history(self, symbol: str, limit: int = 0):
                return [
                    {"fundingRate": 0.00005},
                    {"fundingRate": 0.00008},
                    {"fundingRate": 0.00010},
                ]

            def fetch_open_interest(self, symbol: str):
                return {"openInterestValue": 1200.0}

            def fetch_open_interest_history(self, symbol: str, timeframe: str = "1h", limit: int = 0):
                return [
                    {"openInterestValue": 1000.0},
                    {"openInterestValue": 1100.0},
                ]

            def fetch_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 0):
                return [
                    [1, 0, 0, 0, 100.0, 0],
                    [2, 0, 0, 0, 101.0, 0],
                    [3, 0, 0, 0, 102.0, 0],
                ]

        provider = HtxSentimentProvider(
            exchange=_StubExchange(),
            fear_greed_provider=_StubFearGreed(),
            history_limit=3,
        )
        record = provider.fetch("BTC/USDT")

        assert record.source_name == "htx"
        assert record.fields["fear_greed_index"] == pytest.approx(60.0)
        assert record.fields["funding_rate_zscore"] is not None
        assert record.fields["open_interest_change"] is not None
        assert record.fields["long_short_ratio_change"] is not None
        assert record.fields["liquidation_imbalance"] is not None
        assert record.fields["sentiment_score_ema"] is not None
        assert record.metadata["long_short_ratio_proxy"] is True
        assert record.metadata["liquidation_proxy"] is True


# ─────────────────────────────────────────────────────────────
# SentimentCache 测试
# ─────────────────────────────────────────────────────────────

class TestSentimentCache:
    def test_write_and_read(self, tmp_path: Path):
        cache = SentimentCache(path=tmp_path / "sc.json")
        record = make_sentiment_record()
        cache.write("BTC", record)
        data = cache.read("BTC")
        assert data is not None
        assert "fear_greed_index" in data
        val, at = data["fear_greed_index"]["value"], data["fear_greed_index"]["collected_at"]
        assert val == pytest.approx(52.0)
        assert isinstance(at, datetime)

    def test_read_field(self, tmp_path: Path):
        cache = SentimentCache(path=tmp_path / "sc.json")
        cache.write("BTC", make_sentiment_record())
        value, at = cache.read_field("BTC", "funding_rate_zscore")
        assert value == pytest.approx(0.8)
        assert at is not None

    def test_read_missing_symbol_returns_none(self, tmp_path: Path):
        cache = SentimentCache(path=tmp_path / "sc.json")
        assert cache.read("ETH") is None

    def test_read_field_missing_returns_none_none(self, tmp_path: Path):
        cache = SentimentCache(path=tmp_path / "sc.json")
        val, at = cache.read_field("BTC", "nonexistent_field")
        assert val is None
        assert at is None

    def test_none_value_not_overwrite_cache(self, tmp_path: Path):
        """None 值字段不覆盖已有缓存值。"""
        cache = SentimentCache(path=tmp_path / "sc.json")
        cache.write("BTC", make_sentiment_record())
        # 第二次写入 fear_greed_index=None
        record2 = make_sentiment_record(missing=["fear_greed_index"])
        cache.write("BTC", record2)
        value, _ = cache.read_field("BTC", "fear_greed_index")
        assert value == pytest.approx(52.0)

    def test_freshness_fresh_when_just_written(self, tmp_path: Path):
        cache = SentimentCache(path=tmp_path / "sc.json")
        cache.write("BTC", make_sentiment_record())
        freshness = cache.evaluate_freshness("BTC", FreshnessConfig(default_ttl_sec=3600))
        assert freshness.status == FreshnessStatus.FRESH

    def test_freshness_missing_when_empty(self, tmp_path: Path):
        cache = SentimentCache(path=tmp_path / "sc.json")
        freshness = cache.evaluate_freshness("BTC")
        assert freshness.status == FreshnessStatus.MISSING

    def test_clear_removes_symbol(self, tmp_path: Path):
        cache = SentimentCache(path=tmp_path / "sc.json")
        cache.write("BTC", make_sentiment_record())
        removed = cache.clear("BTC")
        assert removed is True
        assert cache.read("BTC") is None

    def test_clear_missing_symbol_returns_false(self, tmp_path: Path):
        cache = SentimentCache(path=tmp_path / "sc.json")
        assert cache.clear("NONEXISTENT") is False

    def test_wipe_clears_all(self, tmp_path: Path):
        cache = SentimentCache(path=tmp_path / "sc.json")
        cache.write("BTC", make_sentiment_record())
        cache.write("ETH", make_sentiment_record())
        cache.wipe()
        assert cache.read("BTC") is None
        assert cache.read("ETH") is None

    def test_diagnostics_structure(self, tmp_path: Path):
        cache = SentimentCache(path=tmp_path / "sc.json")
        cache.write("BTC", make_sentiment_record())
        diag = cache.diagnostics()
        assert "path" in diag
        assert "cached_symbols" in diag
        assert "BTC" in diag["cached_symbols"]

    def test_persist_across_instances(self, tmp_path: Path):
        """数据应在不同实例之间持久化。"""
        path = tmp_path / "sc.json"
        c1 = SentimentCache(path=path)
        c1.write("BTC", make_sentiment_record())
        c2 = SentimentCache(path=path)
        assert c2.read("BTC") is not None


# ─────────────────────────────────────────────────────────────
# SentimentCollector 测试
# ─────────────────────────────────────────────────────────────

class TestSentimentCollector:
    def test_collect_success(self, tmp_path: Path):
        provider = MockSentimentProvider(seed=1)
        cache = SentimentCache(path=tmp_path / "sc.json")
        collector = SentimentCollector(provider, cache, SentimentCollectorConfig(max_retries=0))
        record = collector.collect("BTC")
        assert record is not None
        assert record.has_all_fields()

    def test_collect_writes_to_cache(self, tmp_path: Path):
        provider = MockSentimentProvider(seed=2)
        cache = SentimentCache(path=tmp_path / "sc.json")
        collector = SentimentCollector(provider, cache, SentimentCollectorConfig(max_retries=0))
        collector.collect("BTC")
        assert cache.read("BTC") is not None

    def test_collect_skips_if_fresh(self, tmp_path: Path):
        """缓存 FRESH 时应跳过 API 调用。"""
        cache = SentimentCache(path=tmp_path / "sc.json")
        cache.write("BTC", make_sentiment_record())

        failing_provider = MockSentimentProvider(fail_rate=1.0)
        cfg = SentimentCollectorConfig(
            max_retries=0,
            skip_if_fresh=True,
            freshness_config=FreshnessConfig(default_ttl_sec=3600),
        )
        collector = SentimentCollector(failing_provider, cache, cfg)
        record = collector.collect("BTC")
        assert record is not None  # 从缓存读取，不崩溃

    def test_collect_fallback_to_cache_on_failure(self, tmp_path: Path):
        """API 失败时应从缓存降级。"""
        provider = MockSentimentProvider(fail_rate=1.0)
        cache = SentimentCache(path=tmp_path / "sc.json")
        cache.write("BTC", make_sentiment_record())

        cfg = SentimentCollectorConfig(max_retries=0, skip_if_fresh=False)
        collector = SentimentCollector(provider, cache, cfg)
        record = collector.collect("BTC")
        assert record is not None  # 从缓存恢复

    def test_collect_returns_none_when_no_cache_and_all_fail(self, tmp_path: Path):
        """API 失败且无缓存时返回 None，不崩溃。"""
        provider = MockSentimentProvider(fail_rate=1.0)
        cache = SentimentCache(path=tmp_path / "sc.json")
        cfg = SentimentCollectorConfig(max_retries=0, skip_if_fresh=False)
        collector = SentimentCollector(provider, cache, cfg)
        record = collector.collect("BTC")
        assert record is None

    def test_last_result_after_collect(self, tmp_path: Path):
        provider = MockSentimentProvider(seed=5)
        cache = SentimentCache(path=tmp_path / "sc.json")
        collector = SentimentCollector(provider, cache)
        collector.collect("BTC")
        assert collector.last_result("BTC") is not None
        assert collector.last_result("ETH") is None

    def test_collect_all_multiple_symbols(self, tmp_path: Path):
        provider = MockSentimentProvider(seed=6)
        cache = SentimentCache(path=tmp_path / "sc.json")
        collector = SentimentCollector(provider, cache)
        results = collector.collect_all(["BTC", "ETH"])
        assert "BTC" in results
        assert "ETH" in results
        assert results["BTC"] is not None


# ─────────────────────────────────────────────────────────────
# SentimentFeatureBuilder 测试
# ─────────────────────────────────────────────────────────────

class TestSentimentFeatureBuilder:
    def test_build_returns_source_frame(self, tmp_path: Path):
        cache = SentimentCache(path=tmp_path / "sc.json")
        record = make_sentiment_record()
        cache.write("BTC", record)
        builder = SentimentFeatureBuilder(cache=cache)
        sf = builder.build("BTC", record)
        assert isinstance(sf, SourceFrame)
        assert not sf.is_empty

    def test_fear_greed_normalized(self):
        """fear_greed_index=52.0 → st_fear_greed=0.52。"""
        record = make_sentiment_record()  # fear_greed_index = 52.0
        builder = SentimentFeatureBuilder(
            config=SentimentFeatureBuilderConfig(normalize_fear_greed=True)
        )
        sf = builder.build("BTC", record)
        val = sf.frame["st_fear_greed"].iloc[0]
        assert val == pytest.approx(0.52, abs=1e-6)

    def test_fear_greed_not_normalized_when_disabled(self):
        record = make_sentiment_record()  # fear_greed_index = 52.0
        builder = SentimentFeatureBuilder(
            config=SentimentFeatureBuilderConfig(normalize_fear_greed=False)
        )
        sf = builder.build("BTC", record)
        val = sf.frame["st_fear_greed"].iloc[0]
        assert val == pytest.approx(52.0)

    def test_funding_rate_clipped(self):
        """funding_rate_zscore 超出 [-3, 3] 应被 clip。"""
        fields = {f: 0.5 for f in SENTIMENT_FIELDS}
        fields["funding_rate_zscore"] = 10.0  # 超出范围
        record = SentimentRecord(fetched_at=now_utc(), fields=fields, source_name="test")
        builder = SentimentFeatureBuilder(
            config=SentimentFeatureBuilderConfig(clip_funding_rate=True)
        )
        sf = builder.build("BTC", record)
        val = sf.frame["st_funding_rate"].iloc[0]
        assert val == pytest.approx(3.0)

    def test_liq_imbalance_clipped(self):
        """liquidation_imbalance 超出 [-1, 1] 应被 clip。"""
        fields = {f: 0.5 for f in SENTIMENT_FIELDS}
        fields["liquidation_imbalance"] = 5.0
        record = SentimentRecord(fetched_at=now_utc(), fields=fields, source_name="test")
        builder = SentimentFeatureBuilder(
            config=SentimentFeatureBuilderConfig(clip_liq_imbalance=True)
        )
        sf = builder.build("BTC", record)
        val = sf.frame["st_liq_imbalance"].iloc[0]
        assert val == pytest.approx(1.0)

    def test_missing_field_is_nan(self):
        """None 值字段应输出 NaN。"""
        record = make_sentiment_record(missing=["fear_greed_index"])
        builder = SentimentFeatureBuilder()
        sf = builder.build("BTC", record)
        assert math.isnan(sf.frame["st_fear_greed"].iloc[0])

    def test_build_with_kline_index(self):
        """提供 kline_index 时应对齐到 K 线时刻（单行）。"""
        kline_idx = make_kline_index(10)
        record = make_sentiment_record()
        builder = SentimentFeatureBuilder()
        sf = builder.build("BTC", record, kline_index=kline_idx)
        assert sf.row_count == 1

    def test_build_from_cache_empty_returns_missing_frame(self, tmp_path: Path):
        """缓存为空时应返回 MISSING SourceFrame。"""
        cache = SentimentCache(path=tmp_path / "sc_empty.json")
        builder = SentimentFeatureBuilder(cache=cache)
        sf = builder.build_from_cache("BTC")
        assert sf.freshness.status == FreshnessStatus.MISSING
        assert sf.is_empty

    def test_build_from_cache_returns_features(self, tmp_path: Path):
        cache = SentimentCache(path=tmp_path / "sc.json")
        record = make_sentiment_record()
        cache.write("BTC", record)
        builder = SentimentFeatureBuilder(cache=cache)
        sf = builder.build_from_cache("BTC")
        assert not sf.is_empty
        assert "st_fear_greed" in sf.frame.columns

    def test_source_name_format(self):
        record = make_sentiment_record()
        builder = SentimentFeatureBuilder()
        sf = builder.build("BTC", record)
        assert sf.source_name == "sentiment_btc"

    def test_metadata_contains_provider(self):
        record = make_sentiment_record()
        builder = SentimentFeatureBuilder()
        sf = builder.build("BTC", record)
        assert "provider" in sf.metadata
        assert "fetched_at" in sf.metadata

    def test_all_feature_columns_present(self):
        """所有 6 个特征列都应存在。"""
        record = make_sentiment_record()
        builder = SentimentFeatureBuilder()
        sf = builder.build("BTC", record)
        for col in ["st_fear_greed", "st_funding_rate", "st_long_short_chg",
                    "st_oi_change", "st_liq_imbalance", "st_sentiment_ema"]:
            assert col in sf.frame.columns, f"缺失列: {col}"

    def test_sentiment_ema_clipped(self):
        """sentiment_score_ema 超出 [0, 1] 应被 clip。"""
        fields = {f: 0.5 for f in SENTIMENT_FIELDS}
        fields["sentiment_score_ema"] = 2.5  # 超出范围
        record = SentimentRecord(fetched_at=now_utc(), fields=fields, source_name="test")
        builder = SentimentFeatureBuilder(
            config=SentimentFeatureBuilderConfig(clip_sentiment_ema=True)
        )
        sf = builder.build("BTC", record)
        val = sf.frame["st_sentiment_ema"].iloc[0]
        assert val == pytest.approx(1.0)
