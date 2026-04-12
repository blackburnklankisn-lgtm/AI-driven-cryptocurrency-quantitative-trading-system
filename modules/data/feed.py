"""
modules/data/feed.py — 数据喂入器（DataFeed）

设计说明：
这是数据层与 Alpha 层 / 回测引擎之间的统一接口。

回测模式：
- 从 ParquetStorage 加载历史数据，按时间顺序迭代，
  每次 next() 推进一根 K 线，向事件总线发布 KlineEvent。
- 严格保证时间顺序，禁止乱序访问（防止未来函数）。

实盘模式（接口一致）：
- 从 CCXT WebSocket 或 REST 轮询获取实时数据，同样发布 KlineEvent。
- 内部机制不同，但对上层模块接口完全一致（透明切换）。

核心设计：
- DataFeed 是"时间主控者"：所有模块跟随 DataFeed 的时间节拍推进。
- 回测中不允许策略层"提前看"未来 K 线。

接口：
    DataFeed(storage, symbols, timeframe, since, until, bus)
    .run()      → 同步驱动整个回测事件循环
    .has_next() → bool
    .current_timestamp → datetime  当前最新 K 线时间
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional

import pandas as pd

from core.event import EventBus, EventType, KlineEvent, get_event_bus
from core.exceptions import DataLayerError
from core.logger import get_logger
from modules.data.storage import ParquetStorage

log = get_logger(__name__)


class DataFeed:
    """
    历史数据喂入器（回测专用）。

    将 Parquet 中的历史 K 线逐根以事件形式推送到事件总线，
    驱动上层的 Alpha 引擎、风控层和回测引擎。

    时间一致性保证：
    - 所有 symbols 按相同时间步进推进（对齐多币种回测时序）
    - 某一时刻缺少数据的 symbol 会跳过发布（不填充、不插值）

    Args:
        storage:    ParquetStorage 实例
        symbols:    目标交易对列表
        timeframe:  K 线周期
        since:      回测起始时间（UTC）
        until:      回测结束时间（UTC）
        bus:        事件总线实例（默认使用全局单例）
    """

    def __init__(
        self,
        storage: ParquetStorage,
        symbols: List[str],
        timeframe: str,
        since: datetime,
        until: datetime,
        bus: Optional[EventBus] = None,
    ) -> None:
        self.storage = storage
        self.symbols = symbols
        self.timeframe = timeframe
        self.since = self._ensure_utc(since)
        self.until = self._ensure_utc(until)
        self.bus = bus or get_event_bus()

        self._data: Dict[str, pd.DataFrame] = {}
        self._timestamps: Optional[List[pd.Timestamp]] = None
        self._cursor: int = 0
        self._loaded = False

    # ────────────────────────────────────────────────────────────
    # 初始化
    # ────────────────────────────────────────────────────────────

    def load(self) -> None:
        """
        从存储加载所有 symbol 的历史数据。
        在调用 run() 或 has_next() 前必须先 load()。

        Raises:
            DataLayerError: 任何 symbol 均无本地数据时
        """
        for symbol in self.symbols:
            df = self.storage.read(
                symbol=symbol,
                timeframe=self.timeframe,
                since=self.since,
                until=self.until,
            )
            if df is None or df.empty:
                log.warning("DataFeed: 无本地数据 symbol={} timeframe={}", symbol, self.timeframe)
                continue

            # 索引为 timestamp，便于按时间快速查找
            df = df.set_index("timestamp").sort_index()
            self._data[symbol] = df
            log.info(
                "DataFeed: 加载 symbol={} {} 条 K 线 [{} ~ {}]",
                symbol,
                len(df),
                df.index.min(),
                df.index.max(),
            )

        if not self._data:
            raise DataLayerError(
                f"DataFeed: 所有 symbol 均无本地数据。"
                f"请先运行 KlineDownloader.download() 下载数据。"
            )

        # 合并所有 symbol 的时间戳，取并集，排序
        all_ts: pd.Index = pd.Index([])
        for df in self._data.values():
            all_ts = all_ts.union(df.index)

        self._timestamps = sorted(all_ts)
        self._cursor = 0
        self._loaded = True
        log.info(
            "DataFeed: 加载完成，共 {} 个时间步，{} 个 symbol",
            len(self._timestamps),
            len(self._data),
        )

    # ────────────────────────────────────────────────────────────
    # 事件驱动接口
    # ────────────────────────────────────────────────────────────

    def run(self) -> int:
        """
        同步运行整个回测数据流。

        逐根 K 线推进时间，向事件总线发布 KlineEvent。

        Returns:
            发布的总事件数
        """
        if not self._loaded:
            self.load()

        total_events = 0
        while self.has_next():
            total_events += self._step()

        log.info("DataFeed: 回测数据流结束，共推送 {} 个 KlineEvent", total_events)
        return total_events

    def has_next(self) -> bool:
        """是否还有下一根 K 线。"""
        if not self._loaded:
            return False
        return self._cursor < len(self._timestamps)  # type: ignore[arg-type]

    @property
    def current_timestamp(self) -> Optional[pd.Timestamp]:
        """当前游标指向的时间戳（推进前的最新已处理时间）。"""
        if not self._loaded or self._cursor == 0:
            return None
        return self._timestamps[self._cursor - 1]  # type: ignore[index]

    def iter_events(self) -> Iterator[List[KlineEvent]]:
        """
        生成器接口：每次 yield 当前时间步的所有 KlineEvent 列表。
        适合需要逐步控制回测推进节奏的高级用法。
        """
        if not self._loaded:
            self.load()

        while self.has_next():
            events = self._get_current_events()
            self._cursor += 1
            yield events

    # ────────────────────────────────────────────────────────────
    # 内部方法
    # ────────────────────────────────────────────────────────────

    def _step(self) -> int:
        """推进一个时间步，发布当前时间点的所有 KlineEvent，返回发布数量。"""
        events = self._get_current_events()
        self._cursor += 1

        for event in events:
            self.bus.publish(event)

        return len(events)

    def _get_current_events(self) -> List[KlineEvent]:
        """构建当前时间步的 KlineEvent 列表（所有有数据的 symbol）。"""
        ts = self._timestamps[self._cursor]  # type: ignore[index]
        events = []

        for symbol, df in self._data.items():
            if ts not in df.index:
                continue  # 该 symbol 在此时间步无数据（正常，如节假日）

            row = df.loc[ts]
            event = KlineEvent(
                event_type=EventType.KLINE_UPDATED,
                timestamp=ts.to_pydatetime(),
                source="data_feed",
                symbol=symbol,
                timeframe=self.timeframe,
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                is_closed=True,  # 历史数据均为已收线 K 线
            )
            events.append(event)

        return events

    @staticmethod
    def _ensure_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
