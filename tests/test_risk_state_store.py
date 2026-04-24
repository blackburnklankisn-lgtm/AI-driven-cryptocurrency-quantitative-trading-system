"""
tests/test_risk_state_store.py — 风控状态持久化存储完整测试

覆盖项：
1. 基本 save/load/delete/keys/wipe
2. 文件不存在时返回空/None
3. 损坏 JSON 文件的容错
4. 原子写入（tmp → replace）
5. datetime 序列化 round-trip
6. 多 key 隔离
7. 并发线程安全
8. diagnostics 字段
9. 自定义路径初始化
10. _iso_to_dt / _dt_to_iso 辅助函数
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from modules.risk.state_store import StateStore, _iso_to_dt, _dt_to_iso


# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def tmp_store(tmp_path: Path) -> StateStore:
    """每个测试使用独立临时目录的 StateStore。"""
    return StateStore(path=tmp_path / "test_risk_state.json")


# ══════════════════════════════════════════════════════════════
# 1. 辅助函数
# ══════════════════════════════════════════════════════════════

class TestIsoHelpers:

    def test_iso_to_dt_none_returns_none(self):
        assert _iso_to_dt(None) is None

    def test_iso_to_dt_aware_string(self):
        s = "2024-01-15T10:30:00+00:00"
        dt = _iso_to_dt(s)
        assert dt.tzinfo is not None
        assert dt.year == 2024
        assert dt.month == 1

    def test_iso_to_dt_naive_string_becomes_utc(self):
        s = "2024-01-15T10:30:00"
        dt = _iso_to_dt(s)
        assert dt.tzinfo is not None
        assert dt.tzinfo == timezone.utc

    def test_iso_to_dt_datetime_passthrough(self):
        now = datetime.now(tz=timezone.utc)
        result = _iso_to_dt(now)
        assert result is now

    def test_dt_to_iso_none_returns_none(self):
        assert _dt_to_iso(None) is None

    def test_dt_to_iso_datetime_to_string(self):
        now = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        s = _dt_to_iso(now)
        assert isinstance(s, str)
        assert "2024-01-15" in s

    def test_dt_to_iso_non_datetime_to_str(self):
        result = _dt_to_iso("already_a_string")
        assert result == "already_a_string"

    def test_round_trip_datetime(self):
        now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        s = _dt_to_iso(now)
        recovered = _iso_to_dt(s)
        assert recovered.year == now.year
        assert recovered.month == now.month
        assert recovered.day == now.day


# ══════════════════════════════════════════════════════════════
# 2. 基本 save / load
# ══════════════════════════════════════════════════════════════

class TestSaveLoad:

    def test_save_and_load_basic_dict(self, tmp_store):
        tmp_store.save("kill_switch", {"active": True, "reason": "test"})
        result = tmp_store.load("kill_switch")
        assert result is not None
        assert result["active"] is True
        assert result["reason"] == "test"

    def test_load_nonexistent_key_returns_none(self, tmp_store):
        assert tmp_store.load("nonexistent_key") is None

    def test_save_overwrites_existing_key(self, tmp_store):
        tmp_store.save("budget", {"used": 100})
        tmp_store.save("budget", {"used": 200})
        result = tmp_store.load("budget")
        assert result["used"] == 200

    def test_save_multiple_keys_isolated(self, tmp_store):
        tmp_store.save("key_a", {"value": "A"})
        tmp_store.save("key_b", {"value": "B"})
        assert tmp_store.load("key_a")["value"] == "A"
        assert tmp_store.load("key_b")["value"] == "B"

    def test_datetime_value_serialized_in_save(self, tmp_store):
        now = datetime(2024, 3, 10, 8, 0, 0, tzinfo=timezone.utc)
        tmp_store.save("test", {"triggered_at": now})
        # Should not raise; datetime is converted to ISO string

    def test_load_datetime_string_can_be_recovered(self, tmp_store):
        now = datetime(2024, 3, 10, 8, 0, 0, tzinfo=timezone.utc)
        tmp_store.save("test", {"triggered_at": now})
        result = tmp_store.load("test")
        recovered = _iso_to_dt(result["triggered_at"])
        assert recovered.year == 2024

    def test_load_file_not_exists_returns_none(self, tmp_path):
        store = StateStore(path=tmp_path / "nonexistent_dir" / "file.json")
        # Load before any save → file doesn't exist
        assert store.load("any_key") is None


# ══════════════════════════════════════════════════════════════
# 3. delete / keys / wipe
# ══════════════════════════════════════════════════════════════

class TestDeleteKeysWipe:

    def test_delete_existing_key_returns_true(self, tmp_store):
        tmp_store.save("ks", {"active": False})
        assert tmp_store.delete("ks") is True
        assert tmp_store.load("ks") is None

    def test_delete_nonexistent_key_returns_false(self, tmp_store):
        assert tmp_store.delete("ghost_key") is False

    def test_keys_returns_all_user_keys(self, tmp_store):
        tmp_store.save("alpha", {"v": 1})
        tmp_store.save("beta", {"v": 2})
        tmp_store.save("gamma", {"v": 3})
        keys = tmp_store.keys()
        assert set(keys) == {"alpha", "beta", "gamma"}

    def test_keys_excludes_internal_metadata(self, tmp_store):
        tmp_store.save("user_key", {"v": 1})
        keys = tmp_store.keys()
        assert "_last_written_at" not in keys

    def test_keys_empty_store_returns_empty_list(self, tmp_store):
        assert tmp_store.keys() == []

    def test_wipe_clears_all_data(self, tmp_store):
        tmp_store.save("k1", {"v": 1})
        tmp_store.save("k2", {"v": 2})
        tmp_store.wipe()
        assert tmp_store.keys() == []
        assert tmp_store.load("k1") is None

    def test_delete_then_key_not_in_keys(self, tmp_store):
        tmp_store.save("target", {"v": 99})
        tmp_store.delete("target")
        assert "target" not in tmp_store.keys()


# ══════════════════════════════════════════════════════════════
# 4. 容错：损坏文件
# ══════════════════════════════════════════════════════════════

class TestCorruptedFile:

    def test_corrupted_json_returns_empty_on_load(self, tmp_path):
        path = tmp_path / "bad_state.json"
        path.write_text("this is not valid JSON!!!", encoding="utf-8")
        store = StateStore(path=path)
        result = store.load("any_key")
        assert result is None

    def test_corrupted_json_keys_returns_empty(self, tmp_path):
        path = tmp_path / "bad_state.json"
        path.write_text("{malformed", encoding="utf-8")
        store = StateStore(path=path)
        assert store.keys() == []

    def test_save_after_corrupt_file_recovers(self, tmp_path):
        path = tmp_path / "bad_state.json"
        path.write_text("CORRUPT", encoding="utf-8")
        store = StateStore(path=path)
        store.save("ok", {"v": 1})
        assert store.load("ok")["v"] == 1


# ══════════════════════════════════════════════════════════════
# 5. diagnostics
# ══════════════════════════════════════════════════════════════

class TestDiagnostics:

    def test_diagnostics_path_and_exists(self, tmp_store, tmp_path):
        diag = tmp_store.diagnostics()
        assert "path" in diag
        assert "exists" in diag

    def test_diagnostics_keys_after_save(self, tmp_store):
        tmp_store.save("monitoring", {"active": True})
        diag = tmp_store.diagnostics()
        assert "monitoring" in diag["keys"]

    def test_diagnostics_last_written_at_populated_after_save(self, tmp_store):
        tmp_store.save("k", {"v": 1})
        diag = tmp_store.diagnostics()
        assert diag["last_written_at"] is not None

    def test_diagnostics_file_not_exists_before_first_save(self, tmp_path):
        store = StateStore(path=tmp_path / "fresh.json")
        diag = store.diagnostics()
        assert diag["exists"] is False


# ══════════════════════════════════════════════════════════════
# 6. 原子写入验证
# ══════════════════════════════════════════════════════════════

class TestAtomicWrite:

    def test_tmp_file_not_left_after_successful_write(self, tmp_store, tmp_path):
        tmp_store.save("test", {"v": 1})
        path = tmp_path / "test_risk_state.json"
        tmp_path_file = path.with_suffix(".tmp")
        # After successful write, .tmp should not exist
        assert not tmp_path_file.exists()

    def test_file_content_is_valid_json_after_save(self, tmp_store, tmp_path):
        tmp_store.save("k", {"value": 42})
        path = tmp_path / "test_risk_state.json"
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["k"]["value"] == 42

    def test_last_written_at_in_file(self, tmp_store, tmp_path):
        tmp_store.save("k", {"v": 1})
        path = tmp_path / "test_risk_state.json"
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        assert "_last_written_at" in data


# ══════════════════════════════════════════════════════════════
# 7. 并发线程安全
# ══════════════════════════════════════════════════════════════

class TestThreadSafety:

    def test_concurrent_saves_do_not_corrupt(self, tmp_store):
        errors = []

        def _writer(i: int):
            try:
                tmp_store.save(f"key_{i}", {"thread_id": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # All keys should be present
        keys = tmp_store.keys()
        assert len(keys) == 20

    def test_concurrent_save_and_load_no_race(self, tmp_store):
        tmp_store.save("shared", {"counter": 0})
        errors = []

        def _save():
            try:
                tmp_store.save("shared", {"counter": 1})
            except Exception as e:
                errors.append(e)

        def _load():
            try:
                tmp_store.load("shared")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=_save if i % 2 == 0 else _load)
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


# ══════════════════════════════════════════════════════════════
# 8. 完整 KillSwitch 状态场景
# ══════════════════════════════════════════════════════════════

class TestKillSwitchScenario:

    def test_kill_switch_activate_and_recover(self, tmp_store):
        now = datetime.now(tz=timezone.utc)
        tmp_store.save("kill_switch", {
            "active": True,
            "reason": "max_drawdown",
            "triggered_at": now,
        })

        result = tmp_store.load("kill_switch")
        assert result["active"] is True
        assert result["reason"] == "max_drawdown"

        # Deactivate
        tmp_store.save("kill_switch", {"active": False, "reason": None})
        result2 = tmp_store.load("kill_switch")
        assert result2["active"] is False

    def test_budget_state_cycle(self, tmp_store):
        tmp_store.save("budget", {
            "daily_used_pct": 0.0,
            "window_start": datetime.now(tz=timezone.utc),
        })
        result = tmp_store.load("budget")
        assert result["daily_used_pct"] == pytest.approx(0.0)

        # After trading
        tmp_store.save("budget", {
            "daily_used_pct": 0.45,
            "window_start": datetime.now(tz=timezone.utc),
        })
        result2 = tmp_store.load("budget")
        assert result2["daily_used_pct"] == pytest.approx(0.45)
