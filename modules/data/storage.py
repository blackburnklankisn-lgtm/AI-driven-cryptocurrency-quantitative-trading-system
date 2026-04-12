"""
modules/data/storage.py — Parquet 本地存储层

设计说明：
- 使用 Parquet（pyarrow）存储历史 K 线数据，列压缩效率高，读取速度快
- 目录结构：{root}/{exchange}/{symbol_safe}/{timeframe}.parquet
  例：storage/binance/BTC_USDT/1h.parquet
- 写入时采用增量追加模式：读取现有数据 → 合并 → 去重 → 排序 → 覆盖写入
- 所有读取操作均返回 UTC 时间戳的 DataFrame（或 None 若文件不存在）
- 不对外暴露文件路径细节，调用者只需提供 symbol 和 timeframe

接口：
    ParquetStorage(root_dir, exchange_id)
    .write(df, symbol, timeframe)    → None
    .read(symbol, timeframe, since, until) → DataFrame | None
    .list_available() → List[dict]   # 列出所有已有数据
    .get_latest_timestamp(symbol, timeframe) → datetime | None
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from core.logger import get_logger

log = get_logger(__name__)


class ParquetStorage:
    """
    基于 Parquet 的本地 K 线数据存储。

    文件路径规则：
        <root_dir>/<exchange_id>/<symbol_safe>/<timeframe>.parquet

    其中 symbol_safe 是 "BTC/USDT" → "BTC_USDT" 的转换（替换 / 为 _）。

    Args:
        root_dir:    本地数据根目录（会自动创建）
        exchange_id: 交易所标识（用于路径分组）
    """

    def __init__(
        self,
        root_dir: str | Path = "./storage",
        exchange_id: str = "binance",
    ) -> None:
        self.root = Path(root_dir)
        self.exchange_id = exchange_id
        self.root.mkdir(parents=True, exist_ok=True)
        log.info("ParquetStorage 初始化: root={}", self.root.resolve())

    # ────────────────────────────────────────────────────────────
    # 公开接口
    # ────────────────────────────────────────────────────────────

    def write(
        self,
        df: pd.DataFrame,
        symbol: str,
        timeframe: str,
    ) -> None:
        """
        增量写入 K 线数据（合并已有数据、去重、排序后覆盖写入）。

        Args:
            df:        新 K 线 DataFrame（必须含 timestamp 列，UTC）
            symbol:    如 "BTC/USDT"
            timeframe: 如 "1h"
        """
        if df.empty:
            log.warning("write() 收到空 DataFrame，跳过: symbol={}", symbol)
            return

        path = self._get_path(symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)

        # 合并现有数据
        existing = self._read_raw(path)
        if existing is not None and not existing.empty:
            combined = pd.concat([existing, df], ignore_index=True)
        else:
            combined = df.copy()

        # 去重 + 排序
        combined = (
            combined
            .drop_duplicates(subset=["timestamp"], keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

        combined.to_parquet(path, index=False, engine="pyarrow")
        log.info(
            "Parquet 写入: {} / {} → {} 条 (路径={})",
            symbol,
            timeframe,
            len(combined),
            path,
        )

    def read(
        self,
        symbol: str,
        timeframe: str,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> Optional[pd.DataFrame]:
        """
        读取历史 K 线数据，支持时间范围过滤。

        Args:
            symbol:    如 "BTC/USDT"
            timeframe: 如 "1h"
            since:     起始时间（UTC，含），None 表示不限
            until:     结束时间（UTC，含），None 表示不限

        Returns:
            DataFrame 或 None（文件不存在时）
        """
        path = self._get_path(symbol, timeframe)
        df = self._read_raw(path)

        if df is None or df.empty:
            return None

        # 时间过滤
        if since is not None:
            since_utc = self._ensure_utc(since)
            df = df[df["timestamp"] >= since_utc]
        if until is not None:
            until_utc = self._ensure_utc(until)
            df = df[df["timestamp"] <= until_utc]

        return df.reset_index(drop=True) if not df.empty else None

    def get_latest_timestamp(
        self,
        symbol: str,
        timeframe: str,
    ) -> Optional[datetime]:
        """
        获取本地已有数据的最新时间戳。

        Returns:
            UTC datetime 或 None（无本地数据时）
        """
        df = self.read(symbol, timeframe)
        if df is None or df.empty:
            return None
        latest: pd.Timestamp = df["timestamp"].max()
        return latest.to_pydatetime()

    def list_available(self) -> List[Dict[str, str]]:
        """
        列出所有本地已有的 K 线数据集。

        Returns:
            [{"exchange": ..., "symbol": ..., "timeframe": ..., "path": ...}]
        """
        results = []
        exchange_dir = self.root / self.exchange_id
        if not exchange_dir.exists():
            return results

        for symbol_dir in sorted(exchange_dir.iterdir()):
            if not symbol_dir.is_dir():
                continue
            symbol = symbol_dir.name.replace("_", "/")
            for parquet_file in sorted(symbol_dir.glob("*.parquet")):
                timeframe = parquet_file.stem
                results.append({
                    "exchange": self.exchange_id,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "path": str(parquet_file),
                })

        return results

    # ────────────────────────────────────────────────────────────
    # 私有工具方法
    # ────────────────────────────────────────────────────────────

    def _get_path(self, symbol: str, timeframe: str) -> Path:
        """构建 Parquet 文件的完整路径。"""
        symbol_safe = symbol.replace("/", "_")
        return self.root / self.exchange_id / symbol_safe / f"{timeframe}.parquet"

    def _read_raw(self, path: Path) -> Optional[pd.DataFrame]:
        """读取 Parquet 文件，若不存在返回 None。"""
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path, engine="pyarrow")
            # 确保时间戳为 UTC-aware
            if "timestamp" in df.columns:
                if df["timestamp"].dt.tz is None:
                    df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
                else:
                    df["timestamp"] = df["timestamp"].dt.tz_convert("UTC")
            return df
        except Exception as exc:  # noqa: BLE001
            log.error("读取 Parquet 文件失败: {} 错误: {}", path, exc)
            return None

    @staticmethod
    def _ensure_utc(dt: datetime) -> datetime:
        """确保 datetime 为 UTC-aware。"""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
