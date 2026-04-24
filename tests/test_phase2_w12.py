"""
tests/test_phase2_w12.py — Phase 2 W12 链上数据层单元测试

覆盖项：
- SourceContract: FreshnessStatus、SourceFreshness、SourceFrame
- FreshnessEvaluator: FRESH/STALE/MISSING/PARTIAL、逐字段 TTL
- SourceAligner: 正常对齐、空 source、前向填充、缺失比例
- OnChainProviders: MockOnChainProvider 正常采集/失败/字段缺失
- OnChainCache: write/read/read_field/evaluate_freshness/clear/wipe
- OnChainCollector: 正常采集、freshness-aware 跳过、失败后从缓存降级
- OnChainFeatureBuilder: 特征变换、log 变换、clip、缺失字段 NaN
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from modules.data.fusion.alignment import AlignmentConfig, SourceAligner
from modules.data.fusion.freshness import FieldTTL, FreshnessConfig, FreshnessEvaluator
from modules.data.fusion.source_contract import (
    FreshnessStatus,
    SourceFreshness,
    SourceFrame,
)
from modules.data.onchain.cache import OnChainCache
from modules.data.onchain.collector import CollectorConfig, OnChainCollector
from modules.data.onchain.feature_builder import (
    FeatureBuilderConfig,
    OnChainFeatureBuilder,
)
from modules.data.onchain.providers import (
    MockOnChainProvider,
    OnChainFetchError,
    OnChainRecord,
    ONCHAIN_FIELDS,
    PublicOnChainProvider,
)


# ─────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def make_kline_index(n: int = 24, freq: str = "1h") -> pd.DatetimeIndex:
    """生成 n 个 1h K 线时间索引（UTC）。"""
    end = pd.Timestamp.now(tz="UTC").floor("h")
    return pd.date_range(end=end, periods=n, freq=freq, tz="UTC")


def make_source_frame(
    source_name: str = "test",
    n_rows: int = 5,
    cols: list[str] | None = None,
    status: FreshnessStatus = FreshnessStatus.FRESH,
) -> SourceFrame:
    idx = make_kline_index(n_rows)
    cols = cols or ["col_a", "col_b"]
    df = pd.DataFrame(
        {c: range(n_rows) for c in cols},
        index=idx,
    )
    freshness = SourceFreshness(
        source_name=source_name,
        status=status,
        lag_sec=0.0,
        ttl_sec=3600,
        collected_at=now_utc(),
    )
    return SourceFrame(source_name=source_name, frame=df, freshness=freshness)


def make_onchain_record(
    missing: list[str] | None = None,
    fail: bool = False,
) -> OnChainRecord:
    if fail:
        raise OnChainFetchError("mock failure")
    missing = missing or []
    fields = {
        "active_addresses_change": 0.02,
        "exchange_inflow_ratio": 0.15,
        "whale_tx_count_ratio": 0.05,
        "stablecoin_supply_ratio": 0.20,
        "miner_reserve_change": -0.01,
        "nvt_proxy": 1.5,
    }
    for f in missing:
        fields[f] = None
    return OnChainRecord(
        fetched_at=now_utc(),
        fields=fields,
        source_name="test_provider",
    )


# ─────────────────────────────────────────────────────────────
# FreshnessStatus 测试
# ─────────────────────────────────────────────────────────────

class TestFreshnessStatus:
    def test_fresh_is_usable(self):
        assert FreshnessStatus.FRESH.is_usable() is True

    def test_partial_is_usable(self):
        assert FreshnessStatus.PARTIAL.is_usable() is True

    def test_stale_not_usable(self):
        assert FreshnessStatus.STALE.is_usable() is False

    def test_missing_not_usable(self):
        assert FreshnessStatus.MISSING.is_usable() is False

    def test_only_fresh_is_fresh(self):
        assert FreshnessStatus.FRESH.is_fresh() is True
        assert FreshnessStatus.PARTIAL.is_fresh() is False


# ─────────────────────────────────────────────────────────────
# SourceFreshness / SourceFrame 测试
# ─────────────────────────────────────────────────────────────

class TestSourceFrame:
    def test_make_empty_is_missing(self):
        sf = SourceFrame.make_empty("test", reason="无数据", ttl_sec=3600)
        assert sf.is_empty is True
        assert sf.freshness.status == FreshnessStatus.MISSING

    def test_row_count(self):
        sf = make_source_frame(n_rows=10)
        assert sf.row_count == 10

    def test_diagnostics_keys(self):
        sf = make_source_frame()
        diag = sf.diagnostics()
        assert "source_name" in diag
        assert "row_count" in diag
        assert "freshness" in diag

    def test_freshness_to_dict(self):
        sf = make_source_frame()
        d = sf.freshness.to_dict()
        assert "status" in d
        assert "lag_sec" in d
        assert "ttl_sec" in d


# ─────────────────────────────────────────────────────────────
# FreshnessEvaluator 测试
# ─────────────────────────────────────────────────────────────

class TestFreshnessEvaluator:
    def test_none_collected_at_is_missing(self):
        ev = FreshnessEvaluator("test")
        result = ev.evaluate(collected_at=None)
        assert result.status == FreshnessStatus.MISSING

    def test_recent_collected_at_is_fresh(self):
        ev = FreshnessEvaluator("test", FreshnessConfig(default_ttl_sec=3600))
        result = ev.evaluate(collected_at=now_utc())
        assert result.status == FreshnessStatus.FRESH

    def test_old_collected_at_is_stale(self):
        ev = FreshnessEvaluator("test", FreshnessConfig(default_ttl_sec=60))
        old = now_utc() - timedelta(seconds=120)
        result = ev.evaluate(collected_at=old)
        assert result.status == FreshnessStatus.STALE

    def test_empty_frame_is_missing(self):
        ev = FreshnessEvaluator("test")
        result = ev.evaluate(collected_at=now_utc(), frame=pd.DataFrame())
        assert result.status == FreshnessStatus.MISSING

    def test_per_field_ttl_override(self):
        """某字段 TTL 极短，应产生 PARTIAL 状态。"""
        field_ttls = [FieldTTL(field_name="slow_field", ttl_sec=1)]
        cfg = FreshnessConfig(default_ttl_sec=3600, field_ttls=field_ttls)
        ev = FreshnessEvaluator("test", cfg)

        old = now_utc() - timedelta(seconds=10)
        df = pd.DataFrame(
            {"fast_field": [1.0], "slow_field": [2.0]},
        )
        result = ev.evaluate(collected_at=old, frame=df)
        # fast_field: TTL=3600, lag=10 → FRESH
        # slow_field: TTL=1, lag=10 → STALE
        assert result.status == FreshnessStatus.PARTIAL

    def test_all_fresh_fields_is_fresh(self):
        cfg = FreshnessConfig(default_ttl_sec=3600)
        ev = FreshnessEvaluator("test", cfg)
        df = pd.DataFrame({"a": [1.0], "b": [2.0]})
        result = ev.evaluate(collected_at=now_utc(), frame=df)
        assert result.status == FreshnessStatus.FRESH

    def test_all_stale_fields_is_stale(self):
        cfg = FreshnessConfig(default_ttl_sec=1)
        ev = FreshnessEvaluator("test", cfg)
        df = pd.DataFrame({"a": [1.0], "b": [2.0]})
        old = now_utc() - timedelta(seconds=10)
        result = ev.evaluate(collected_at=old, frame=df)
        assert result.status == FreshnessStatus.STALE

    def test_lag_sec_correct(self):
        ev = FreshnessEvaluator("test")
        target_lag = 30.0
        old = now_utc() - timedelta(seconds=target_lag)
        result = ev.evaluate(collected_at=old)
        assert abs(result.lag_sec - target_lag) < 2.0  # 容忍 2 秒误差


# ─────────────────────────────────────────────────────────────
# SourceAligner 测试
# ─────────────────────────────────────────────────────────────

class TestSourceAligner:
    def test_align_basic(self):
        """基本对齐：source 覆盖全部 K 线时间范围。"""
        kline_idx = make_kline_index(24)
        # 每小时有数据
        src_idx = kline_idx[::2]  # 每 2 小时一个 source 数据点
        df = pd.DataFrame({"val": range(len(src_idx))}, index=src_idx)
        freshness = SourceFreshness(
            "test", FreshnessStatus.FRESH, 0, 3600, now_utc()
        )
        source = SourceFrame("test", df, freshness)

        aligner = SourceAligner(AlignmentConfig(max_fill_periods=24))
        result = aligner.align(source, kline_idx)
        assert result.is_usable
        assert result.kline_row_count == 24

    def test_empty_source_all_missing(self):
        kline_idx = make_kline_index(10)
        source = SourceFrame.make_empty("empty_src", reason="无数据")
        aligner = SourceAligner()
        result = aligner.align(source, kline_idx)
        assert result.missing_ratio == 1.0
        # empty source → aligned_frame has kline index but no data columns
        assert len(result.aligned_frame) == 10

    def test_empty_kline_index(self):
        source = make_source_frame(n_rows=5)
        aligner = SourceAligner()
        result = aligner.align(source, pd.DatetimeIndex([], tz="UTC"))
        assert result.kline_row_count == 0

    def test_forward_fill_limited(self):
        """前向填充不应超过 max_fill_periods。"""
        kline_idx = make_kline_index(10, freq="1h")
        # 只有第一个时间点有数据
        src_idx = pd.DatetimeIndex([kline_idx[0]], tz="UTC")
        df = pd.DataFrame({"val": [99.0]}, index=src_idx)
        freshness = SourceFreshness("t", FreshnessStatus.FRESH, 0, 3600, now_utc())
        source = SourceFrame("t", df, freshness)

        aligner = SourceAligner(AlignmentConfig(max_fill_periods=3))
        result = aligner.align(source, kline_idx)
        # 只填充 3 个周期，剩余 6 个应该是 NaN
        filled = result.aligned_frame["val"].notna().sum()
        assert filled <= 4  # 第 1 行 + 最多 3 个填充 = 4

    def test_missing_ratio_correct(self):
        kline_idx = make_kline_index(10)
        # 只有 5 个数据点，前向填充 0 → 期望 ~50% 缺失
        src_idx = kline_idx[:5]
        df = pd.DataFrame({"val": range(5)}, index=src_idx)
        freshness = SourceFreshness("t", FreshnessStatus.FRESH, 0, 3600, now_utc())
        source = SourceFrame("t", df, freshness)

        aligner = SourceAligner(AlignmentConfig(max_fill_periods=0))
        result = aligner.align(source, kline_idx)
        # 后 5 个 kline 无 source 数据 → missing >= 50%
        assert result.missing_ratio >= 0.4

    def test_align_multiple_sources(self):
        kline_idx = make_kline_index(12)
        s1 = make_source_frame("src1", n_rows=6)
        s2 = make_source_frame("src2", n_rows=8)
        aligner = SourceAligner()
        results = aligner.align_multiple([s1, s2], kline_idx)
        assert "src1" in results
        assert "src2" in results


# ─────────────────────────────────────────────────────────────
# MockOnChainProvider 测试
# ─────────────────────────────────────────────────────────────

class TestMockOnChainProvider:
    def test_fetch_returns_all_fields(self):
        provider = MockOnChainProvider(seed=42)
        record = provider.fetch("BTC")
        for f in ONCHAIN_FIELDS:
            assert f in record.fields
            assert record.fields[f] is not None

    def test_fetch_with_missing_fields(self):
        provider = MockOnChainProvider(missing_fields=["nvt_proxy", "exchange_inflow_ratio"])
        record = provider.fetch("BTC")
        assert record.fields["nvt_proxy"] is None
        assert record.fields["exchange_inflow_ratio"] is None
        assert record.fields["active_addresses_change"] is not None

    def test_fetch_with_fail_rate_1_raises(self):
        provider = MockOnChainProvider(fail_rate=1.0)
        with pytest.raises(OnChainFetchError):
            provider.fetch("BTC")

    def test_seed_deterministic(self):
        p1 = MockOnChainProvider(seed=99)
        p2 = MockOnChainProvider(seed=99)
        r1 = p1.fetch()
        r2 = p2.fetch()
        assert r1.fields == r2.fields

    def test_provider_name(self):
        assert MockOnChainProvider().provider_name == "mock_onchain"

    def test_has_all_fields(self):
        record = MockOnChainProvider().fetch()
        assert record.has_all_fields() is True

    def test_missing_fields_listed(self):
        provider = MockOnChainProvider(missing_fields=["nvt_proxy"])
        record = provider.fetch()
        assert "nvt_proxy" in record.missing_fields()


class TestPublicOnChainProvider:
    def test_fetch_builds_public_proxy_metrics(self):
        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        def _fake_get(url, params=None, timeout=None):
            if url.endswith("/bitcoin"):
                return _Resp(
                    {
                        "market_data": {
                            "market_cap": {"usd": 883.0},
                            "total_volume": {"usd": 60.0},
                        }
                    }
                )
            if url.endswith("/tether"):
                return _Resp({"market_data": {"market_cap": {"usd": 100.0}}})
            if url.endswith("/usd-coin"):
                return _Resp({"market_data": {"market_cap": {"usd": 50.0}}})
            if url.endswith("/dai"):
                return _Resp({"market_data": {"market_cap": {"usd": 5.0}}})
            if url.endswith("/n-unique-addresses"):
                return _Resp({"values": [{"y": 100.0}, {"y": 110.0}]})
            if url.endswith("/estimated-transaction-volume-usd"):
                return _Resp({"values": [{"y": 1000.0}, {"y": 1200.0}]})
            if url.endswith("/n-transactions"):
                return _Resp({"values": [{"y": 10.0}, {"y": 12.0}]})
            if url.endswith("/hash-rate"):
                return _Resp({"values": [{"y": 200.0}, {"y": 190.0}]})
            raise AssertionError(f"unexpected url: {url}")

        provider = PublicOnChainProvider(timeout_sec=1.0)
        with patch("modules.data.onchain.providers.requests.get", side_effect=_fake_get):
            record = provider.fetch("ETH/USDT")

        assert record.source_name == "public"
        assert record.fields["active_addresses_change"] == pytest.approx(0.10)
        assert record.fields["exchange_inflow_ratio"] == pytest.approx(60.0 / 1260.0)
        assert record.fields["stablecoin_supply_ratio"] == pytest.approx(155.0 / 883.0)
        assert record.fields["miner_reserve_change"] == pytest.approx(-0.05)
        assert record.fields["nvt_proxy"] == pytest.approx(883.0 / 1200.0)
        assert record.metadata["proxy_mode"] is True
        assert record.metadata["proxy_symbol"] == "BTC"


# ─────────────────────────────────────────────────────────────
# OnChainCache 测试
# ─────────────────────────────────────────────────────────────

class TestOnChainCache:
    def test_write_and_read(self, tmp_path: Path):
        cache = OnChainCache(path=tmp_path / "oc.json")
        record = make_onchain_record()
        cache.write("BTC", record)
        data = cache.read("BTC")
        assert data is not None
        assert "active_addresses_change" in data
        val, at = data["active_addresses_change"]["value"], data["active_addresses_change"]["collected_at"]
        assert val == pytest.approx(0.02)
        assert isinstance(at, datetime)

    def test_read_field(self, tmp_path: Path):
        cache = OnChainCache(path=tmp_path / "oc.json")
        record = make_onchain_record()
        cache.write("BTC", record)
        value, at = cache.read_field("BTC", "nvt_proxy")
        assert value == pytest.approx(1.5)
        assert at is not None

    def test_read_missing_symbol_returns_none(self, tmp_path: Path):
        cache = OnChainCache(path=tmp_path / "oc.json")
        assert cache.read("ETH") is None

    def test_read_field_missing_returns_none_none(self, tmp_path: Path):
        cache = OnChainCache(path=tmp_path / "oc.json")
        val, at = cache.read_field("BTC", "nonexistent")
        assert val is None
        assert at is None

    def test_none_value_not_overwrite_cache(self, tmp_path: Path):
        """None 值字段不覆盖已有缓存（保留上次有效值）。"""
        cache = OnChainCache(path=tmp_path / "oc.json")
        record1 = make_onchain_record()
        cache.write("BTC", record1)
        # 第二次写入时 nvt_proxy=None
        record2 = make_onchain_record(missing=["nvt_proxy"])
        cache.write("BTC", record2)
        value, _ = cache.read_field("BTC", "nvt_proxy")
        # 应该保留 record1 的值
        assert value == pytest.approx(1.5)

    def test_freshness_fresh_when_just_written(self, tmp_path: Path):
        cache = OnChainCache(path=tmp_path / "oc.json")
        record = make_onchain_record()
        cache.write("BTC", record)
        freshness = cache.evaluate_freshness("BTC", FreshnessConfig(default_ttl_sec=3600))
        assert freshness.status == FreshnessStatus.FRESH

    def test_freshness_missing_when_empty(self, tmp_path: Path):
        cache = OnChainCache(path=tmp_path / "oc.json")
        freshness = cache.evaluate_freshness("BTC")
        assert freshness.status == FreshnessStatus.MISSING

    def test_clear_removes_symbol(self, tmp_path: Path):
        cache = OnChainCache(path=tmp_path / "oc.json")
        cache.write("BTC", make_onchain_record())
        removed = cache.clear("BTC")
        assert removed is True
        assert cache.read("BTC") is None

    def test_clear_missing_symbol_returns_false(self, tmp_path: Path):
        cache = OnChainCache(path=tmp_path / "oc.json")
        assert cache.clear("NONEXISTENT") is False

    def test_wipe_clears_all(self, tmp_path: Path):
        cache = OnChainCache(path=tmp_path / "oc.json")
        cache.write("BTC", make_onchain_record())
        cache.write("ETH", make_onchain_record())
        cache.wipe()
        assert cache.read("BTC") is None

    def test_diagnostics_structure(self, tmp_path: Path):
        cache = OnChainCache(path=tmp_path / "oc.json")
        cache.write("BTC", make_onchain_record())
        diag = cache.diagnostics()
        assert "path" in diag
        assert "cached_symbols" in diag
        assert "BTC" in diag["cached_symbols"]


# ─────────────────────────────────────────────────────────────
# OnChainCollector 测试
# ─────────────────────────────────────────────────────────────

class TestOnChainCollector:
    def test_collect_success(self, tmp_path: Path):
        provider = MockOnChainProvider(seed=1)
        cache = OnChainCache(path=tmp_path / "oc.json")
        collector = OnChainCollector(provider, cache, CollectorConfig(max_retries=0))
        record = collector.collect("BTC")
        assert record is not None
        assert record.has_all_fields()

    def test_collect_writes_to_cache(self, tmp_path: Path):
        provider = MockOnChainProvider(seed=2)
        cache = OnChainCache(path=tmp_path / "oc.json")
        collector = OnChainCollector(provider, cache, CollectorConfig(max_retries=0))
        collector.collect("BTC")
        assert cache.read("BTC") is not None

    def test_collect_skips_if_fresh(self, tmp_path: Path):
        """缓存 FRESH 时应跳过 API 调用（fail_rate=1.0 的 provider 不应被调用）。"""
        provider = MockOnChainProvider(seed=3, fail_rate=0.0)
        cache = OnChainCache(path=tmp_path / "oc.json")
        # 先写入一条 FRESH 数据
        cache.write("BTC", make_onchain_record())

        # 换成 fail_rate=1.0 的 provider
        failing_provider = MockOnChainProvider(fail_rate=1.0)
        cfg = CollectorConfig(
            max_retries=0,
            skip_if_fresh=True,
            freshness_config=FreshnessConfig(default_ttl_sec=3600),
        )
        collector = OnChainCollector(failing_provider, cache, cfg)
        # 因为缓存 FRESH，应该从缓存读取而不调用 provider
        record = collector.collect("BTC")
        assert record is not None  # 从缓存恢复，不崩溃

    def test_collect_fallback_to_cache_on_failure(self, tmp_path: Path):
        """API 失败时应降级到缓存。"""
        provider = MockOnChainProvider(fail_rate=1.0)
        cache = OnChainCache(path=tmp_path / "oc.json")
        # 先写入一条缓存数据
        cache.write("BTC", make_onchain_record())

        cfg = CollectorConfig(max_retries=0, skip_if_fresh=False)
        collector = OnChainCollector(provider, cache, cfg)
        record = collector.collect("BTC")
        assert record is not None  # 从缓存恢复

    def test_collect_returns_none_when_no_cache_and_all_fail(self, tmp_path: Path):
        """API 失败且无缓存时返回 None（不崩溃）。"""
        provider = MockOnChainProvider(fail_rate=1.0)
        cache = OnChainCache(path=tmp_path / "oc.json")
        cfg = CollectorConfig(max_retries=0, skip_if_fresh=False)
        collector = OnChainCollector(provider, cache, cfg)
        record = collector.collect("BTC")
        assert record is None

    def test_last_result_after_collect(self, tmp_path: Path):
        provider = MockOnChainProvider(seed=5)
        cache = OnChainCache(path=tmp_path / "oc.json")
        collector = OnChainCollector(provider, cache)
        collector.collect("BTC")
        assert collector.last_result("BTC") is not None
        assert collector.last_result("ETH") is None

    def test_collect_all_multiple_symbols(self, tmp_path: Path):
        provider = MockOnChainProvider(seed=6)
        cache = OnChainCache(path=tmp_path / "oc.json")
        collector = OnChainCollector(provider, cache)
        results = collector.collect_all(["BTC", "ETH"])
        assert "BTC" in results
        assert "ETH" in results
        assert results["BTC"] is not None


# ─────────────────────────────────────────────────────────────
# OnChainFeatureBuilder 测试
# ─────────────────────────────────────────────────────────────

class TestOnChainFeatureBuilder:
    def test_build_returns_source_frame(self, tmp_path: Path):
        cache = OnChainCache(path=tmp_path / "oc.json")
        builder = OnChainFeatureBuilder(cache=cache)
        record = make_onchain_record()
        cache.write("BTC", record)
        sf = builder.build("BTC", record)
        assert isinstance(sf, SourceFrame)
        assert not sf.is_empty

    def test_log_transform_nvt(self, tmp_path: Path):
        """nvt_proxy 应该做 log1p 变换。"""
        record = make_onchain_record()  # nvt_proxy = 1.5
        builder = OnChainFeatureBuilder(config=FeatureBuilderConfig(log_transform_nvt=True))
        sf = builder.build("BTC", record)
        val = sf.frame["oc_nvt_log"].iloc[0]
        assert val == pytest.approx(math.log1p(1.5), abs=1e-6)

    def test_clip_ratio_fields(self, tmp_path: Path):
        """exchange_inflow_ratio > 1.0 应被 clip 到 1.0。"""
        fields = {f: 0.5 for f in ONCHAIN_FIELDS}
        fields["exchange_inflow_ratio"] = 5.0  # 超出 [0,1]
        record = OnChainRecord(fetched_at=now_utc(), fields=fields, source_name="test")
        builder = OnChainFeatureBuilder(config=FeatureBuilderConfig(clip_ratio_fields=True))
        sf = builder.build("BTC", record)
        val = sf.frame["oc_exchange_inflow"].iloc[0]
        assert val == pytest.approx(1.0)

    def test_missing_field_is_nan(self):
        """None 值字段应输出 NaN。"""
        record = make_onchain_record(missing=["nvt_proxy"])
        builder = OnChainFeatureBuilder()
        sf = builder.build("BTC", record)
        assert math.isnan(sf.frame["oc_nvt_log"].iloc[0])

    def test_build_with_kline_index(self):
        """提供 kline_index 时应将特征对齐到 K 线时刻。"""
        kline_idx = make_kline_index(10)
        record = make_onchain_record()
        builder = OnChainFeatureBuilder()
        sf = builder.build("BTC", record, kline_index=kline_idx)
        # 只有 1 行（fetched_at 时刻），供 SourceAligner 前向填充
        assert sf.row_count == 1

    def test_build_from_cache_empty_returns_missing_frame(self, tmp_path: Path):
        """缓存为空时应返回 MISSING SourceFrame。"""
        cache = OnChainCache(path=tmp_path / "oc_empty.json")
        builder = OnChainFeatureBuilder(cache=cache)
        sf = builder.build_from_cache("BTC")
        assert sf.freshness.status == FreshnessStatus.MISSING
        assert sf.is_empty

    def test_build_from_cache_returns_features(self, tmp_path: Path):
        cache = OnChainCache(path=tmp_path / "oc.json")
        record = make_onchain_record()
        cache.write("BTC", record)
        builder = OnChainFeatureBuilder(cache=cache)
        sf = builder.build_from_cache("BTC")
        assert not sf.is_empty
        assert "oc_nvt_log" in sf.frame.columns

    def test_source_name_format(self):
        record = make_onchain_record()
        builder = OnChainFeatureBuilder()
        sf = builder.build("BTC", record)
        assert sf.source_name == "onchain_btc"

    def test_metadata_contains_provider(self):
        record = make_onchain_record()
        builder = OnChainFeatureBuilder()
        sf = builder.build("BTC", record)
        assert "provider" in sf.metadata
        assert "fetched_at" in sf.metadata
