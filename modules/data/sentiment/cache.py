"""
modules/data/sentiment/cache.py — 情绪数据本地缓存与 freshness 判断

设计说明：
- 采集成功后立即写入本地 JSON 缓存，供后续无网时读取
- 缓存结构：{ symbol: { field_name: {"value": float, "collected_at": iso_str} } }
- 每个字段有独立存储时间戳，支持逐字段 freshness 判断
- 线程安全（读写互斥锁）
- 原子写入（写临时文件 → os.replace）防止部分写入
- 存储路径默认 storage/sentiment_cache.json，可通过构造参数覆盖
- 情绪数据更新频率高于链上数据（fear_greed 每日更新，资金费率每 8h 更新）

接口：
    SentimentCache(path)
    .write(symbol, record)           # 写入 SentimentRecord
    .read(symbol) -> dict | None     # 读取指定 symbol 的所有字段
    .read_field(symbol, field) -> (value, collected_at) | (None, None)
    .evaluate_freshness(symbol, config) -> SourceFreshness
    .clear(symbol) -> bool           # 清除指定 symbol 缓存
    .wipe()                          # 清空全部缓存
    .diagnostics() -> dict           # 诊断信息

日志标签：[Sentiment]
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
from modules.data.sentiment.providers import SENTIMENT_FIELDS, SentimentRecord

log = get_logger(__name__)

_DEFAULT_CACHE_PATH = (
    Path(__file__).resolve().parents[3] / "storage" / "sentiment_cache.json"
)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_dt(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    dt = datetime.fromisoformat(str(val))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class SentimentCache:
    """
    情绪数据本地缓存。

    JSON 结构：
    {
      "BTC": {
        "fear_greed_index":       {"value": 52.0, "collected_at": "2024-..."},
        "funding_rate_zscore":    {"value": 0.32, "collected_at": "2024-..."},
        ...
      }
    }

    Args:
        path: 缓存文件路径（默认 storage/sentiment_cache.json）
    """

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self._path = Path(path) if path else _DEFAULT_CACHE_PATH
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        log.info("[Sentiment] SentimentCache 初始化: path={}", self._path)

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
            log.error("[Sentiment] 缓存读取失败，返回空: {}", exc)
            return {}

    def _write_all(self, data: dict[str, Any]) -> None:
        tmp = self._path.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except OSError as exc:
            log.error("[Sentiment] 缓存写入失败: {}", exc)

    # ──────────────────────────────────────────────────────────────
    # 公共接口
    # ──────────────────────────────────────────────────────────────

    def write(self, symbol: str, record: SentimentRecord) -> None:
        """
        写入 SentimentRecord 到缓存。

        None 值字段不会覆盖已有的有效值（保留上次采集成功的数据）。

        Args:
            symbol: 目标资产（如 "BTC"）
            record: SentimentRecord 采集结果
        """
        with self._lock:
            data = self._read_all()
            sym_data = data.get(symbol, {})

            now_iso = _iso(record.fetched_at)
            updated_count = 0
            for field_name, value in record.fields.items():
                if value is None:
                    # 跳过 None 值，保留已有缓存
                    continue
                sym_data[field_name] = {
                    "value": value,
                    "collected_at": now_iso,
                }
                updated_count += 1

            data[symbol] = sym_data
            self._write_all(data)

        log.debug(
            "[Sentiment] 缓存写入: symbol={} updated_fields={} missing={}",
            symbol,
            updated_count,
            record.missing_fields(),
        )

    def read(self, symbol: str) -> Optional[dict[str, Any]]:
        """
        读取指定 symbol 的所有字段缓存。

        Returns:
            { field_name: {"value": float, "collected_at": datetime} } 或 None
        """
        with self._lock:
            data = self._read_all()

        sym_data = data.get(symbol)
        if not sym_data:
            return None

        result: dict[str, Any] = {}
        for field_name, entry in sym_data.items():
            result[field_name] = {
                "value": entry.get("value"),
                "collected_at": _parse_dt(entry.get("collected_at")),
            }
        return result

    def read_field(
        self, symbol: str, field_name: str
    ) -> Tuple[Optional[float], Optional[datetime]]:
        """
        读取单个字段的缓存值和采集时间。

        Returns:
            (value, collected_at) 或 (None, None)（字段不存在时）
        """
        raw = self.read(symbol)
        if raw is None or field_name not in raw:
            return None, None
        entry = raw[field_name]
        return entry.get("value"), entry.get("collected_at")

    def evaluate_freshness(
        self,
        symbol: str,
        config: Optional[FreshnessConfig] = None,
    ) -> SourceFreshness:
        """
        评估指定 symbol 的 freshness 状态。

        基于每个字段的 collected_at 与 TTL 独立判断，
        取最旧字段的 lag 作为全局 lag_sec。

        Args:
            symbol: 目标资产
            config: FreshnessConfig（可覆盖默认 TTL）

        Returns:
            SourceFreshness
        """
        source_name = f"sentiment_{symbol.lower()}"
        raw = self.read(symbol)
        if not raw:
            return SourceFreshness(
                source_name=source_name,
                status=FreshnessStatus.MISSING,
                lag_sec=float("inf"),
                ttl_sec=config.default_ttl_sec if config else 3600,
                collected_at=None,
            )

        cfg = config or FreshnessConfig()
        now = datetime.now(tz=timezone.utc)
        evaluator = FreshnessEvaluator(source_name, cfg)

        # 取所有字段中最旧的 collected_at
        oldest_at: Optional[datetime] = None
        for field_name in SENTIMENT_FIELDS:
            if field_name not in raw:
                continue
            at = raw[field_name].get("collected_at")
            if at is None:
                continue
            if oldest_at is None or at < oldest_at:
                oldest_at = at

        if oldest_at is None:
            return SourceFreshness(
                source_name=source_name,
                status=FreshnessStatus.MISSING,
                lag_sec=float("inf"),
                ttl_sec=cfg.default_ttl_sec,
                collected_at=None,
            )

        # 构建 DataFrame 用于逐字段 freshness 评估
        import pandas as pd

        field_rows: list[dict] = []
        for field_name in SENTIMENT_FIELDS:
            if field_name not in raw:
                continue
            at = raw[field_name].get("collected_at")
            val = raw[field_name].get("value")
            if at is not None and val is not None:
                field_rows.append({field_name: val})

        if not field_rows:
            return evaluator.evaluate(collected_at=None)

        # 合并为单行 DataFrame，用最旧字段时间做全局 evaluate
        merged = {}
        for row in field_rows:
            merged.update(row)
        df = pd.DataFrame([merged])

        return evaluator.evaluate(collected_at=oldest_at, frame=df)

    def clear(self, symbol: str) -> bool:
        """
        清除指定 symbol 的缓存。

        Returns:
            True（找到并删除）/ False（未找到）
        """
        with self._lock:
            data = self._read_all()
            if symbol not in data:
                return False
            del data[symbol]
            self._write_all(data)
        log.debug("[Sentiment] 缓存已清除: symbol={}", symbol)
        return True

    def wipe(self) -> None:
        """清空全部缓存（主要用于测试）。"""
        with self._lock:
            self._write_all({})
        log.info("[Sentiment] 缓存已全部清空")

    def diagnostics(self) -> dict[str, Any]:
        """返回缓存诊断信息。"""
        with self._lock:
            data = self._read_all()
        return {
            "path": str(self._path),
            "exists": self._path.exists(),
            "cached_symbols": list(data.keys()),
            "symbol_field_counts": {
                sym: len(fields) for sym, fields in data.items()
            },
        }
