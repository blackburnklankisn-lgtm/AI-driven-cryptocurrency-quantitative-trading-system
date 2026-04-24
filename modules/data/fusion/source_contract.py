"""
modules/data/fusion/source_contract.py — 多源数据公共契约类型

设计说明：
- 定义所有数据 source 层共享的数据契约
- SourceFrame: 规范化后的外部数据帧（含时间戳、DataFrame、freshness 元数据）
- SourceFreshness: 单个数据源在某一时刻的 freshness 评估结果
- FreshnessStatus: freshness 状态枚举
- 所有 source（onchain / sentiment / future）都必须输出这两种类型

日志标签：[SourceFrame] [SourceFreshness]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import pandas as pd


class FreshnessStatus(str, Enum):
    """数据源 freshness 状态。"""

    FRESH = "fresh"           # 数据新鲜，在 TTL 内
    STALE = "stale"           # 数据过期，超过 TTL，但仍存在
    MISSING = "missing"       # 数据完全缺失（从未采集或缓存为空）
    PARTIAL = "partial"       # 数据部分存在（某些字段缺失或部分 TTL 过期）

    def is_usable(self) -> bool:
        """是否可以用于特征构建（即使降级）。"""
        return self in (FreshnessStatus.FRESH, FreshnessStatus.PARTIAL)

    def is_fresh(self) -> bool:
        """是否完全新鲜。"""
        return self == FreshnessStatus.FRESH


@dataclass(frozen=True)
class SourceFreshness:
    """
    单个数据源在某一时刻的 freshness 评估结果。

    Attributes:
        source_name:      数据源名称（如 "onchain_btc", "sentiment"）
        status:           freshness 状态枚举
        lag_sec:          当前数据距最新采集时刻的秒数（0 = 刚刚采集）
        ttl_sec:          本 source 的有效 TTL（秒）
        collected_at:     最新数据采集时间（UTC），None 表示从未采集
        evaluated_at:     freshness 评估时间（UTC）
        degrade_reason:   降级原因（FRESH 时为 None）
        field_freshness:  逐字段 freshness（字段名 -> FreshnessStatus），可选
    """

    source_name: str
    status: FreshnessStatus
    lag_sec: float
    ttl_sec: int
    collected_at: Optional[datetime]
    evaluated_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    degrade_reason: Optional[str] = None
    field_freshness: dict[str, FreshnessStatus] = field(default_factory=dict)

    def is_fresh(self) -> bool:
        return self.status.is_fresh()

    def is_usable(self) -> bool:
        return self.status.is_usable()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "status": self.status.value,
            "lag_sec": self.lag_sec,
            "ttl_sec": self.ttl_sec,
            "collected_at": self.collected_at.isoformat() if self.collected_at else None,
            "evaluated_at": self.evaluated_at.isoformat(),
            "degrade_reason": self.degrade_reason,
            "field_freshness": {k: v.value for k, v in self.field_freshness.items()},
        }


@dataclass
class SourceFrame:
    """
    规范化后的外部数据帧，包含 DataFrame + freshness 元数据。

    设计要点：
    - frame 的索引必须是 UTC DatetimeIndex，用于与 K 线对齐
    - timestamp_col 用于记录原始时间戳列名（已对齐后可以 = index）
    - 不可变性：frame 内容在构造后不应被修改（使用 copy）

    Attributes:
        source_name:       数据源名称（如 "onchain_btc"）
        frame:             规范化后的 DataFrame（UTC DatetimeIndex）
        freshness:         该帧的 freshness 评估结果
        timestamp_col:     原始时间戳列名（若已设为 index 则为 "__index__"）
        freshness_ttl_sec: 本 source 的默认 TTL（秒），用于对齐层参考
        metadata:          附加元数据（provider 版本、字段数量等）
    """

    source_name: str
    frame: pd.DataFrame
    freshness: SourceFreshness
    timestamp_col: str = "__index__"
    freshness_ttl_sec: int = 3600
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def make_empty(
        cls,
        source_name: str,
        reason: str,
        ttl_sec: int = 3600,
    ) -> "SourceFrame":
        """
        构造一个空 SourceFrame（缺失时使用）。

        Args:
            source_name: 数据源名称
            reason:      缺失原因
            ttl_sec:     TTL（用于 freshness 元数据）
        """
        freshness = SourceFreshness(
            source_name=source_name,
            status=FreshnessStatus.MISSING,
            lag_sec=float("inf"),
            ttl_sec=ttl_sec,
            collected_at=None,
            degrade_reason=reason,
        )
        return cls(
            source_name=source_name,
            frame=pd.DataFrame(),
            freshness=freshness,
            freshness_ttl_sec=ttl_sec,
        )

    @property
    def is_empty(self) -> bool:
        return self.frame.empty

    @property
    def row_count(self) -> int:
        return len(self.frame)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "row_count": self.row_count,
            "is_empty": self.is_empty,
            "columns": list(self.frame.columns) if not self.is_empty else [],
            "freshness": self.freshness.to_dict(),
            "ttl_sec": self.freshness_ttl_sec,
            "metadata": self.metadata,
        }
