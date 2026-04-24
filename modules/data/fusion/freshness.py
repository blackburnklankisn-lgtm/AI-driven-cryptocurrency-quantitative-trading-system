"""
modules/data/fusion/freshness.py — 多源数据 freshness 判断引擎

设计说明：
- 负责对任意数据源的"新鲜度"进行评估，输出结构化 SourceFreshness
- 每个字段可以有独立 TTL（per-field TTL），整体状态为所有字段的综合
- 评估规则：
    * 所有字段都在 TTL 内 → FRESH
    * 所有字段均已过期 → STALE
    * 部分字段过期 → PARTIAL
    * DataFrame 为空 / collected_at=None → MISSING
- FreshnessEvaluator 是无状态的，每次调用都是独立评估
- 支持全局 TTL（默认）+ 逐字段覆盖 TTL

日志标签：[SourceFreshness]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from core.logger import get_logger
from modules.data.fusion.source_contract import FreshnessStatus, SourceFreshness

log = get_logger(__name__)


@dataclass
class FieldTTL:
    """单个字段的 freshness TTL 配置。"""

    field_name: str
    ttl_sec: int            # 该字段的有效 TTL（秒）
    allow_forward_fill: bool = True   # 是否允许在 TTL 内向前填充
    max_fill_periods: int = 24        # 最大前向填充周期数（防止填充到无限远）


@dataclass
class FreshnessConfig:
    """
    FreshnessEvaluator 配置。

    Attributes:
        default_ttl_sec:  所有字段的默认 TTL（秒），可被 field_ttls 覆盖
        field_ttls:       逐字段 TTL 配置（覆盖 default_ttl_sec）
        partial_threshold: 有多少比例字段 FRESH 时仍判定为 PARTIAL（非 STALE）
                           默认 0.0 表示只要有一个字段是 FRESH 就输出 PARTIAL
    """

    default_ttl_sec: int = 3600          # 1 小时
    field_ttls: list[FieldTTL] = field(default_factory=list)
    partial_threshold: float = 0.0       # 超过此比例字段 FRESH → PARTIAL（而非 STALE）


class FreshnessEvaluator:
    """
    数据源 freshness 评估器。

    无状态，每次 evaluate() 独立计算当前时刻的 freshness 状态。

    Args:
        source_name: 数据源名称（用于日志和输出）
        config:      FreshnessConfig 配置对象
    """

    def __init__(
        self,
        source_name: str,
        config: Optional[FreshnessConfig] = None,
    ) -> None:
        self.source_name = source_name
        self.config = config or FreshnessConfig()
        # 构建字段名 -> TTL 查找表
        self._field_ttl_map: dict[str, FieldTTL] = {
            f.field_name: f for f in self.config.field_ttls
        }

    # ──────────────────────────────────────────────────────────────
    # 核心评估接口
    # ──────────────────────────────────────────────────────────────

    def evaluate(
        self,
        collected_at: Optional[datetime],
        frame: Optional[pd.DataFrame] = None,
    ) -> SourceFreshness:
        """
        评估数据源当前的 freshness 状态。

        Args:
            collected_at: 最近一次成功采集的 UTC 时间戳
            frame:        当前 DataFrame（可选，用于逐字段评估）

        Returns:
            SourceFreshness 评估结果
        """
        now = datetime.now(tz=timezone.utc)

        # ── 1. MISSING：从未采集或 frame 为空 ────────────────────
        if collected_at is None:
            return self._make_freshness(
                status=FreshnessStatus.MISSING,
                lag_sec=float("inf"),
                collected_at=None,
                now=now,
                degrade_reason="collected_at=None，数据从未采集",
            )

        if collected_at.tzinfo is None:
            collected_at = collected_at.replace(tzinfo=timezone.utc)

        lag_sec = (now - collected_at).total_seconds()

        if frame is not None and frame.empty:
            return self._make_freshness(
                status=FreshnessStatus.MISSING,
                lag_sec=lag_sec,
                collected_at=collected_at,
                now=now,
                degrade_reason="DataFrame 为空",
            )

        # ── 2. 逐字段评估（如果有 frame）────────────────────────
        field_freshness: dict[str, FreshnessStatus] = {}
        if frame is not None and not frame.empty:
            field_freshness = self._evaluate_fields(frame, lag_sec)

        # ── 3. 无逐字段时：基于全局 TTL 判断 ────────────────────
        if not field_freshness:
            default_ttl = self.config.default_ttl_sec
            if lag_sec <= default_ttl:
                status = FreshnessStatus.FRESH
                reason = None
            else:
                status = FreshnessStatus.STALE
                reason = (
                    f"数据延迟 {lag_sec:.0f}s > TTL {default_ttl}s"
                )
            log.debug(
                "[SourceFreshness] source={} status={} lag={:.0f}s ttl={}s",
                self.source_name,
                status.value,
                lag_sec,
                default_ttl,
            )
            return self._make_freshness(
                status=status,
                lag_sec=lag_sec,
                collected_at=collected_at,
                now=now,
                degrade_reason=reason,
                field_freshness={},
            )

        # ── 4. 汇总逐字段结果 ─────────────────────────────────────
        fresh_count = sum(
            1 for s in field_freshness.values() if s == FreshnessStatus.FRESH
        )
        total = len(field_freshness)
        fresh_ratio = fresh_count / total if total > 0 else 0.0

        if fresh_count == total:
            status = FreshnessStatus.FRESH
            reason = None
        elif fresh_count == 0:
            status = FreshnessStatus.STALE
            reason = f"所有字段 ({total}) 均已过 TTL，lag={lag_sec:.0f}s"
        elif fresh_ratio > self.config.partial_threshold:
            status = FreshnessStatus.PARTIAL
            stale_fields = [
                k for k, v in field_freshness.items()
                if v != FreshnessStatus.FRESH
            ]
            reason = f"部分字段过期: {stale_fields}"
        else:
            status = FreshnessStatus.STALE
            reason = (
                f"FRESH 字段比例 {fresh_ratio:.0%} <= partial_threshold "
                f"{self.config.partial_threshold:.0%}"
            )

        log.debug(
            "[SourceFreshness] source={} status={} fresh={}/{} lag={:.0f}s",
            self.source_name,
            status.value,
            fresh_count,
            total,
            lag_sec,
        )
        return self._make_freshness(
            status=status,
            lag_sec=lag_sec,
            collected_at=collected_at,
            now=now,
            degrade_reason=reason,
            field_freshness=field_freshness,
        )

    # ──────────────────────────────────────────────────────────────
    # 内部辅助
    # ──────────────────────────────────────────────────────────────

    def _evaluate_fields(
        self,
        frame: pd.DataFrame,
        global_lag_sec: float,
    ) -> dict[str, FreshnessStatus]:
        """
        对 DataFrame 的每一列独立评估 freshness。

        对于有逐列 TTL 配置的字段：使用该字段的 TTL 和全局 lag_sec 判断。
        对于无逐列 TTL 配置的字段：使用全局 default_ttl_sec。
        """
        result: dict[str, FreshnessStatus] = {}
        for col in frame.columns:
            if col in self._field_ttl_map:
                ttl = self._field_ttl_map[col].ttl_sec
            else:
                ttl = self.config.default_ttl_sec

            # 检查列是否全空（未填充的字段）
            non_null_count = frame[col].count()
            if non_null_count == 0:
                result[col] = FreshnessStatus.MISSING
            elif global_lag_sec <= ttl:
                result[col] = FreshnessStatus.FRESH
            else:
                result[col] = FreshnessStatus.STALE

        return result

    def _make_freshness(
        self,
        status: FreshnessStatus,
        lag_sec: float,
        collected_at: Optional[datetime],
        now: datetime,
        degrade_reason: Optional[str] = None,
        field_freshness: Optional[dict[str, FreshnessStatus]] = None,
    ) -> SourceFreshness:
        return SourceFreshness(
            source_name=self.source_name,
            status=status,
            lag_sec=lag_sec,
            ttl_sec=self.config.default_ttl_sec,
            collected_at=collected_at,
            evaluated_at=now,
            degrade_reason=degrade_reason,
            field_freshness=field_freshness or {},
        )

    def field_ttl(self, field_name: str) -> int:
        """返回指定字段的 TTL（秒），不存在则返回默认 TTL。"""
        if field_name in self._field_ttl_map:
            return self._field_ttl_map[field_name].ttl_sec
        return self.config.default_ttl_sec
