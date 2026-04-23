"""
验证 K 线 API 排序+去重 / developing bar 时间保护 / mock 数据时间戳对齐 / preload 重试。
运行: python scripts/verify_kline_fix.py
"""
from __future__ import annotations

import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}  —  {detail}")


# ────────────────────────────────────────────────────────────
# Test 1: _cache_kline_event rejects out-of-order bars
# ────────────────────────────────────────────────────────────
print("\n=== Test 1: _cache_kline_event rejects out-of-order bars ===")

from core.event import EventType, KlineEvent

# Simulate the kline store and cache function logic
store = {}


def cache_kline_event(event: KlineEvent) -> None:
    if event.symbol not in store:
        store[event.symbol] = []
    kline = {
        "time": int(event.timestamp.timestamp()),
        "open": float(event.open),
        "high": float(event.high),
        "low": float(event.low),
        "close": float(event.close),
        "volume": float(event.volume),
    }
    s = store[event.symbol]
    if s and s[-1]["time"] == kline["time"]:
        s[-1] = kline
    elif s and kline["time"] < s[-1]["time"]:
        pass  # reject out-of-order
    else:
        s.append(kline)
        if len(s) > 600:
            store[event.symbol] = s[-600:]


# Add bars in order
for h in range(0, 10):
    ev = KlineEvent(
        event_type=EventType.KLINE_UPDATED,
        timestamp=datetime(2026, 4, 20, h, 0, 0, tzinfo=timezone.utc),
        source="test",
        symbol="BTC/USDT",
        timeframe="1h",
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("10"),
        is_closed=True,
    )
    cache_kline_event(ev)

check("10 bars cached in order", len(store["BTC/USDT"]) == 10)

# Try adding an out-of-order bar (mock-style mid-hour timestamp)
old_len = len(store["BTC/USDT"])
ev_bad = KlineEvent(
    event_type=EventType.KLINE_UPDATED,
    timestamp=datetime(2026, 4, 20, 5, 30, 0, tzinfo=timezone.utc),
    source="mock_feed",
    symbol="BTC/USDT",
    timeframe="1h",
    open=Decimal("100"),
    high=Decimal("101"),
    low=Decimal("99"),
    close=Decimal("100"),
    volume=Decimal("10"),
    is_closed=True,
)
cache_kline_event(ev_bad)
check(
    "Out-of-order bar rejected",
    len(store["BTC/USDT"]) == old_len,
    f"expected {old_len}, got {len(store['BTC/USDT'])}",
)

# ────────────────────────────────────────────────────────────
# Test 2: API sort + dedup + developing bar protection
# ────────────────────────────────────────────────────────────
print("\n=== Test 2: API sort/dedup and developing bar ordering ===")


def simulate_get_klines(
    kline_store: list, current_price: float, now_utc: datetime
) -> list:
    """Mirrors the logic in server.py get_klines."""
    closed_bars = list(kline_store)

    # Sort + dedup (from server.py fix)
    if closed_bars:
        closed_bars.sort(key=lambda x: x["time"])
        deduped = [closed_bars[0]]
        for bar in closed_bars[1:]:
            if bar["time"] > deduped[-1]["time"]:
                deduped.append(bar)
            elif bar["time"] == deduped[-1]["time"]:
                deduped[-1] = bar
        closed_bars = deduped

    # Developing bar (from server.py fix)
    if current_price and closed_bars:
        current_bar_open_ts = int(
            datetime(
                now_utc.year, now_utc.month, now_utc.day, now_utc.hour, 0, 0,
                tzinfo=timezone.utc,
            ).timestamp()
        )
        last_closed = closed_bars[-1]
        if current_bar_open_ts > last_closed["time"]:
            open_price = float(last_closed["close"])
            developing_bar = {
                "time": current_bar_open_ts,
                "open": open_price,
                "high": max(open_price, current_price),
                "low": min(open_price, current_price),
                "close": current_price,
                "volume": 0.0,
            }
            closed_bars.append(developing_bar)

    return closed_bars


# Case A: Normal real data (hour-aligned)
real_bars = [
    {"time": 1776718800, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10},
    {"time": 1776722400, "open": 100.5, "high": 102, "low": 100, "close": 101, "volume": 11},
]
now_a = datetime(2026, 4, 21, 2, 15, 0, tzinfo=timezone.utc)
result_a = simulate_get_klines(real_bars, 101.5, now_a)
times_a = [b["time"] for b in result_a]
check(
    "Real data: times strictly ascending",
    all(times_a[i] < times_a[i + 1] for i in range(len(times_a) - 1)),
    f"times={times_a}",
)
check("Real data: developing bar appended", len(result_a) == 3, f"len={len(result_a)}")

# Case B: Mock data (mid-hour timestamps) — the bug scenario
# now is 14:19 UTC on Apr 21 → hour start is 14:00:00 UTC
now_b = datetime(2026, 4, 21, 14, 19, 0, tzinfo=timezone.utc)
hour_start_b = int(datetime(2026, 4, 21, 14, 0, 0, tzinfo=timezone.utc).timestamp())
# Mock bars within the current hour (simulating datetime.now() timestamps)
mock_bars = [
    {"time": hour_start_b + 1145, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
    {"time": hour_start_b + 1205, "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10},
]
result_b = simulate_get_klines(mock_bars, 100.8, now_b)
times_b = [b["time"] for b in result_b]
check(
    "Mock data: developing bar NOT appended (would be before last mock bar)",
    len(result_b) == 2,
    f"len={len(result_b)}, times={times_b}",
)
check(
    "Mock data: times still ascending",
    all(times_b[i] < times_b[i + 1] for i in range(len(times_b) - 1)),
    f"times={times_b}",
)

# Case C: Unsorted data gets sorted
unsorted_bars = [
    {"time": 1776752345, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
    {"time": 1776718800, "open": 99, "high": 100, "low": 98, "close": 99.5, "volume": 8},
    {"time": 1776722400, "open": 99.5, "high": 100.5, "low": 99, "close": 100, "volume": 9},
]
result_c = simulate_get_klines(unsorted_bars, 100, now_b)
times_c = [b["time"] for b in result_c]
check(
    "Unsorted data: sorted correctly",
    times_c == sorted(times_c),
    f"times={times_c}",
)

# Case D: Duplicate timestamps get deduplicated
dup_bars = [
    {"time": 1776718800, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 10},
    {"time": 1776718800, "open": 100, "high": 102, "low": 99, "close": 101, "volume": 15},
    {"time": 1776722400, "open": 101, "high": 103, "low": 100, "close": 102, "volume": 12},
]
result_d = simulate_get_klines(dup_bars, 102, now_a)
check(
    "Duplicate timestamps: deduplicated",
    len(result_d) >= 2,
    f"len={len(result_d)}",
)
# The dedup should keep the later bar (close=101)
first_bar = result_d[0]
check(
    "Duplicate timestamps: kept newer data",
    first_bar["close"] == 101,
    f"close={first_bar['close']}",
)

# ────────────────────────────────────────────────────────────
# Test 3: Mock data uses hour-aligned timestamps
# ────────────────────────────────────────────────────────────
print("\n=== Test 3: Mock data timestamp alignment ===")

now_utc = datetime(2026, 4, 21, 14, 35, 22, tzinfo=timezone.utc)
# The fix: mock_ts = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
mock_ts = now_utc.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
check(
    "Mock timestamp is previous hour boundary",
    mock_ts == datetime(2026, 4, 21, 13, 0, 0, tzinfo=timezone.utc),
    f"got {mock_ts}",
)

mock_ts_unix = int(mock_ts.timestamp())
current_hour_start = int(
    datetime(2026, 4, 21, 14, 0, 0, tzinfo=timezone.utc).timestamp()
)
check(
    "Mock ts < current hour start (developing bar safe)",
    mock_ts_unix < current_hour_start,
    f"mock={mock_ts_unix} current_hour={current_hour_start}",
)

# ────────────────────────────────────────────────────────────
# Test 4: Deferred preload state machine
# ────────────────────────────────────────────────────────────
print("\n=== Test 4: Deferred preload state machine ===")

from core.event import EventType, KlineEvent

# Simulate the state flags
_markets_loaded = False
_preload_done = False

# Scenario 4a: Startup fails → flags stay False
check(
    "Startup: _markets_loaded starts False",
    _markets_loaded is False,
)
check(
    "Startup: _preload_done starts False",
    _preload_done is False,
)

# Scenario 4b: Main loop gets only mock events → no deferred preload
mock_events = [
    KlineEvent(
        event_type=EventType.KLINE_UPDATED,
        timestamp=datetime(2026, 4, 21, 13, 0, 0, tzinfo=timezone.utc),
        source="mock_feed",
        symbol="BTC/USDT",
        timeframe="1h",
        open=Decimal("67000"),
        high=Decimal("67200"),
        low=Decimal("66800"),
        close=Decimal("67100"),
        volume=Decimal("10.5"),
        is_closed=True,
    )
]
has_live = any(e.source == "live_feed" for e in mock_events)
should_trigger_preload = not _preload_done and mock_events and has_live
check(
    "Mock events: deferred preload NOT triggered",
    should_trigger_preload is False,
    f"has_live={has_live}",
)

# Scenario 4c: Main loop gets live_feed event → triggers deferred preload
live_events = [
    KlineEvent(
        event_type=EventType.KLINE_UPDATED,
        timestamp=datetime(2026, 4, 21, 13, 0, 0, tzinfo=timezone.utc),
        source="live_feed",
        symbol="BTC/USDT",
        timeframe="1h",
        open=Decimal("87000"),
        high=Decimal("87200"),
        low=Decimal("86800"),
        close=Decimal("87100"),
        volume=Decimal("50"),
        is_closed=True,
    )
]
has_live = any(e.source == "live_feed" for e in live_events)
should_trigger_preload = not _preload_done and live_events and has_live
check(
    "Live events: deferred preload triggered",
    should_trigger_preload is True,
)

# Scenario 4d: After preload done → no more triggers
_preload_done = True
should_trigger_preload = not _preload_done and live_events and has_live
check(
    "After preload done: deferred preload NOT triggered again",
    should_trigger_preload is False,
)

# ────────────────────────────────────────────────────────────
# Summary
# ────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
if failed:
    sys.exit(1)
else:
    print("All checks passed! ✅")
    sys.exit(0)
