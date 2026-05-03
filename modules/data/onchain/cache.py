"""
modules/data/onchain/cache.py — 链上数据本地缓存与 freshness 判断

设计说明：
- 采集成功后立即写入本地 JSON 缓存，供后续无网时读取
- 缓存结构：{ symbol: { field_name: {"value": float, "collected_at": iso_str} } }
- 每个字段有独立存储时间戳，支持逐字段 freshness 判断
- 线程安全（读写互斥锁）
- 原子写入（写临时文件 → os.replace）防止部分写入
- 存储路径默认 storage/onchain_cache.json，可通过构造参数覆盖

接口：
    OnChainCache(path)
    .write(symbol, record)           # 写入 OnChainRecord
    .read(symbol) -> dict | None     # 读取指定 symbol 的所有字段
    .read_field(symbol, field) -> (value, collected_at) | (None, None)
    .evaluate_freshness(symbol, config) -> SourceFreshness
    .clear(symbol)                   # 清除指定 symbol 缓存（测试用）
    .wipe()                          # 清空全部缓存（测试用）

日志标签：[OnChain]
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

from core.logger import get_logger
from modules.data.fusion.freshness import FreshnessConfig, FreshnessEvaluator
from modules.data.fusion.source_contract import FreshnessStatus, SourceFreshness
from modules.data.onchain.providers import ONCHAIN_FIELDS, OnChainRecord

log = get_logger(__name__)

_DEFAULT_CACHE_PATH = (
    Path(os.getenv("USER_DATA_DIR", "")).joinpath("storage", "onchain_cache.json")
    if os.getenv("USER_DATA_DIR")
    else (Path(__file__).resolve().parents[3] / "storage" / "onchain_cache.json")
)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_dt(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    dt = datetime.fromisoformat(str(val))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _normalize_symbol(symbol: str) -> str:
    token = (symbol or "BTC").split(":", 1)[0]
    token = token.split("/", 1)[0]
    token = token.split("-", 1)[0]
    token = token.strip().upper()
    return token or "BTC"


class OnChainCache:
    """
    链上数据本地缓存。

    JSON 结构：
    {
      "BTC": {
        "active_addresses_change": {"value": 0.012, "collected_at": "2024-..."},
        "exchange_inflow_ratio":   {"value": 0.05,  "collected_at": "2024-..."},
        ...
      }
    }

    Args:
        path: 缓存文件路径（默认 storage/onchain_cache.json）
    """

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self._path = Path(path) if path else _DEFAULT_CACHE_PATH
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        log.info("[OnChain] OnChainCache 初始化: path={}", self._path)

    # ──────────────────────────────────────────────────────────────
    # 文件 I/O
    # ──────────────────────────────────────────────────────────────

    def _read_all(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            with self._path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.error("[OnChain] 缓存读取失败，返回空: {}", exc)
            return {}

    def _write_all(self, data: dict[str, Any]) -> None:
        tmp = self._path.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            log.error("[OnChain] 缓存写入失败: {}", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # ──────────────────────────────────────────────────────────────
    # 写入接口
    # ──────────────────────────────────────────────────────────────

    def write(self, symbol: str, record: OnChainRecord) -> None:
        """
        将 OnChainRecord 写入缓存。

        每个字段独立存储，附带 collected_at 时间戳。
        """
        symbol = _normalize_symbol(symbol)
        with self._lock:
            data = self._read_all()
            sym_data = data.get(symbol, {})
            for field_name, value in record.fields.items():
                if value is not None:
                    sym_data[field_name] = {
                        "value": value,
                        "collected_at": _iso(record.fetched_at),
                    }
                # None 值不覆盖缓存（保留上次有效采集）
            data[symbol] = sym_data
            data["_written_at"] = _iso(datetime.now(tz=timezone.utc))
            self._write_all(data)
        log.debug(
            "[OnChain] 缓存写入: symbol={} fields={} missing={}",
            symbol,
            [k for k, v in record.fields.items() if v is not None],
            record.missing_fields(),
        )

    # ──────────────────────────────────────────────────────────────
    # 读取接口
    # ──────────────────────────────────────────────────────────────

    def read(self, symbol: str) -> Optional[dict[str, Any]]:
        """
        读取指定 symbol 的全部缓存字段。

        Returns:
            dict（字段名 -> {"value": float, "collected_at": datetime}），
            如果 symbol 不存在则返回 None
        """
        symbol = _normalize_symbol(symbol)
        with self._lock:
            data = self._read_all()
        raw = data.get(symbol)
        if raw is None:
            return None
        result: dict[str, Any] = {}
        for field_name, entry in raw.items():
            result[field_name] = {
                "value": entry.get("value"),
                "collected_at": _parse_dt(entry.get("collected_at")),
            }
        return result

    def read_field(
        self,
        symbol: str,
        field_name: str,
    ) -> Tuple[Optional[float], Optional[datetime]]:
        """
        读取单个字段的值和采集时间。

        Returns:
            (value, collected_at)，若不存在则 (None, None)
        """
        cached = self.read(symbol)
        if cached is None or field_name not in cached:
            return None, None
        entry = cached[field_name]
        return entry.get("value"), entry.get("collected_at")

    # ──────────────────────────────────────────────────────────────
    # Freshness 评估
    # ──────────────────────────────────────────────────────────────

    def evaluate_freshness(
        self,
        symbol: str,
        config: Optional[FreshnessConfig] = None,
    ) -> SourceFreshness:
        """
        评估指定 symbol 的缓存数据 freshness。

        使用所有字段的 collected_at 中的最旧值作为全局 lag_sec 参考。
        逐字段独立评估（使用 FreshnessEvaluator 的 per-field TTL）。

        Args:
            symbol: 目标资产
            config: FreshnessConfig（可含逐字段 TTL），None 时使用默认配置

        Returns:
            SourceFreshness 评估结果
        """
        symbol = _normalize_symbol(symbol)
        evaluator = FreshnessEvaluator(
            source_name=f"onchain_{symbol.lower()}",
            config=config,
        )
        cached = self.read(symbol)

        if not cached:
            return evaluator.evaluate(collected_at=None)

        # 找出最旧的 collected_at（作为全局 lag 参考）
        oldest_at: Optional[datetime] = None
        for field_name in ONCHAIN_FIELDS:
            if field_name in cached:
                at = cached[field_name].get("collected_at")
                if at is not None:
                    if oldest_at is None or at < oldest_at:
                        oldest_at = at

        # 逐字段构建 freshness（基于各字段 collected_at）
        now = datetime.now(tz=timezone.utc)
        field_freshness: dict[str, FreshnessStatus] = {}
        cfg = config or FreshnessConfig()

        for field_name in ONCHAIN_FIELDS:
            if field_name not in cached:
                field_freshness[field_name] = FreshnessStatus.MISSING
                continue
            at = cached[field_name].get("collected_at")
            if at is None:
                field_freshness[field_name] = FreshnessStatus.MISSING
                continue
            field_ttl = cfg.default_ttl_sec
            for ft in cfg.field_ttls:
                if ft.field_name == field_name:
                    field_ttl = ft.ttl_sec
                    break
            lag = (now - at).total_seconds()
            field_freshness[field_name] = (
                FreshnessStatus.FRESH if lag <= field_ttl else FreshnessStatus.STALE
            )

        fresh_count = sum(
            1 for s in field_freshness.values() if s == FreshnessStatus.FRESH
        )
        total = len(ONCHAIN_FIELDS)
        lag_sec = (now - oldest_at).total_seconds() if oldest_at else float("inf")

        if fresh_count == total:
            status = FreshnessStatus.FRESH
            reason = None
        elif fresh_count == 0:
            status = FreshnessStatus.STALE
            reason = f"所有链上字段均已过 TTL"
        else:
            status = FreshnessStatus.PARTIAL
            stale = [k for k, v in field_freshness.items() if v != FreshnessStatus.FRESH]
            reason = f"部分链上字段过期: {stale}"

        log.debug(
            "[SourceFreshness] onchain_{} status={} fresh={}/{} lag={:.0f}s",
            symbol.lower(),
            status.value,
            fresh_count,
            total,
            lag_sec,
        )

        return SourceFreshness(
            source_name=f"onchain_{symbol.lower()}",
            status=status,
            lag_sec=lag_sec,
            ttl_sec=cfg.default_ttl_sec,
            collected_at=oldest_at,
            degrade_reason=reason,
            field_freshness=field_freshness,
        )

    # ──────────────────────────────────────────────────────────────
    # 管理接口
    # ──────────────────────────────────────────────────────────────

    def clear(self, symbol: str) -> bool:
        """删除指定 symbol 的缓存数据。"""
        symbol = _normalize_symbol(symbol)
        with self._lock:
            data = self._read_all()
            if symbol not in data:
                return False
            del data[symbol]
            self._write_all(data)
        return True

    def wipe(self) -> None:
        """清空全部缓存（仅测试用）。"""
        with self._lock:
            self._write_all({})
        log.warning("[OnChain] 链上缓存已清空（wipe 调用）")

    def diagnostics(self) -> dict[str, Any]:
        """返回缓存诊断信息。"""
        with self._lock:
            data = self._read_all()
        symbols = [k for k in data if not k.startswith("_")]
        return {
            "path": str(self._path),
            "exists": self._path.exists(),
            "cached_symbols": symbols,
            "written_at": data.get("_written_at"),
        }
