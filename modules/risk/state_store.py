"""
modules/risk/state_store.py — 风控状态持久化存储

设计说明：
- 轻量 JSON 文件持久化，避免重启后隐式解除风险状态
- 支持 KillSwitch / BudgetChecker 状态的跨重启恢复
- 原子写入（写临时文件 → os.replace）防止部分写入导致状态损坏
- 线程安全（内部互斥锁）
- 存储路径默认为 storage/risk_state.json，可通过构造参数覆盖

日志标签：[StateStore]
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.logger import get_logger

log = get_logger(__name__)

# ── 默认存储路径（相对于项目根目录）─────────────────────────────
_DEFAULT_STORE_PATH = Path(__file__).resolve().parents[2] / "storage" / "risk_state.json"


def _iso_to_dt(value: Any) -> Optional[datetime]:
    """将 ISO 8601 字符串还原为 timezone-aware datetime，None 原样返回。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _dt_to_iso(value: Any) -> Optional[str]:
    """将 datetime 序列化为 ISO 8601 字符串，None 原样返回。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class StateStore:
    """
    JSON 文件风控状态存储。

    使用方式：
        store = StateStore()                    # 使用默认路径
        store = StateStore("/custom/path.json") # 使用自定义路径

    读写：
        store.save("kill_switch", {"active": True, "reason": "高回撤"})
        data = store.load("kill_switch")  # dict 或 None
        store.delete("kill_switch")
        store.keys()  # ["kill_switch", "budget"]
    """

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self._path = Path(path) if path else _DEFAULT_STORE_PATH
        self._lock = threading.Lock()
        # 确保目录存在
        self._path.parent.mkdir(parents=True, exist_ok=True)
        log.info("[StateStore] 初始化完成，存储路径: {}", self._path)

    # ──────────────────────────────────────────────────────────────
    # 内部文件 I/O（持有锁内调用）
    # ──────────────────────────────────────────────────────────────

    def _read_all(self) -> dict[str, Any]:
        """读取全量存储数据（不加锁，调用方负责加锁）。"""
        if not self._path.exists():
            return {}
        try:
            with self._path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.error("[StateStore] 读取状态文件失败，返回空状态: {}", exc)
            return {}

    def _write_all(self, data: dict[str, Any]) -> None:
        """原子写入全量数据（不加锁，调用方负责加锁）。"""
        tmp_path = self._path.with_suffix(".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp_path, self._path)  # 原子替换（Windows/POSIX 均支持）
        except OSError as exc:
            log.error("[StateStore] 写入状态文件失败: {}", exc)
            # 清理孤立 tmp 文件
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    # ──────────────────────────────────────────────────────────────
    # 公开接口
    # ──────────────────────────────────────────────────────────────

    def save(self, key: str, data: dict[str, Any]) -> None:
        """
        保存指定 key 的状态数据（会覆盖同名 key）。

        Args:
            key:  状态标识符（如 "kill_switch"、"budget"）
            data: 要保存的字典（datetime 值会自动序列化为 ISO 字符串）
        """
        serialized = {
            k: (_dt_to_iso(v) if isinstance(v, datetime) else v)
            for k, v in data.items()
        }
        with self._lock:
            all_data = self._read_all()
            all_data[key] = serialized
            all_data["_last_written_at"] = datetime.now(tz=timezone.utc).isoformat()
            self._write_all(all_data)
        log.debug("[StateStore] 已保存 key={}", key)

    def load(self, key: str) -> Optional[dict[str, Any]]:
        """
        加载指定 key 的状态数据。

        Returns:
            dict（如果 key 存在），否则 None
        """
        with self._lock:
            all_data = self._read_all()
        return all_data.get(key)

    def delete(self, key: str) -> bool:
        """
        删除指定 key 的状态数据。

        Returns:
            True 如果 key 存在并被删除，False 如果 key 不存在
        """
        with self._lock:
            all_data = self._read_all()
            if key not in all_data:
                return False
            del all_data[key]
            all_data["_last_written_at"] = datetime.now(tz=timezone.utc).isoformat()
            self._write_all(all_data)
        log.debug("[StateStore] 已删除 key={}", key)
        return True

    def keys(self) -> list[str]:
        """返回所有用户存储的 key 列表（不含内部元数据 key）。"""
        with self._lock:
            all_data = self._read_all()
        return [k for k in all_data if not k.startswith("_")]

    def wipe(self) -> None:
        """清空所有存储数据（仅用于测试）。"""
        with self._lock:
            self._write_all({})
        log.warning("[StateStore] 已清空全部风控状态（wipe 调用）")

    def diagnostics(self) -> dict[str, Any]:
        """返回存储诊断信息。"""
        with self._lock:
            all_data = self._read_all()
        return {
            "path": str(self._path),
            "exists": self._path.exists(),
            "keys": [k for k in all_data if not k.startswith("_")],
            "last_written_at": all_data.get("_last_written_at"),
        }
