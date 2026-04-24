"""
modules/data/fusion/alignment.py — 多源数据时间对齐引擎

设计说明：
- 负责将外部数据源（链上/情绪）的低频 DataFrame 对齐到 K 线时间索引
- 对齐规则（有意对抗未来数据泄漏）：
    * 使用 merge_asof（向后看）—— 每根 K 线只能使用"其时间点之前"已知的外部数据
    * 禁止 forward fill 到无限远（受 max_fill_periods 约束）
    * 填充超出范围的点用 NaN，不伪造数据
- SourceAligner 是无状态的，每次 align() 都是独立操作
- 输出带有明确 missing_ratio 诊断的对齐结果

日志标签：[SourceAlign]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd

from core.logger import get_logger
from modules.data.fusion.source_contract import SourceFrame

log = get_logger(__name__)


@dataclass
class AlignmentConfig:
    """
    SourceAligner 对齐配置。

    Attributes:
        max_fill_periods:   最大前向填充步数（防止陈旧数据无限延伸）
                            如 K 线 1h，max_fill_periods=24 → 最多填充 24 小时
        timestamp_tolerance_sec: 时间戳对齐容差（秒），超过此偏差的行不对齐
                            0 = 不限制（使用 merge_asof 自然匹配）
        drop_empty_rows:    是否丢弃外部数据全为 NaN 的行（默认 False，保留用于诊断）
    """

    max_fill_periods: int = 24
    timestamp_tolerance_sec: int = 0   # 0 = 不限制
    drop_empty_rows: bool = False


@dataclass
class AlignmentResult:
    """
    对齐操作的输出结果。

    Attributes:
        aligned_frame:      对齐后的 DataFrame（行索引与 kline_index 一致）
        missing_ratio:      外部数据缺失行占总 K 线数的比例（0~1）
        fill_ratio:         前向填充行占总 K 线数的比例（0~1）
        source_row_count:   对齐前外部数据的行数
        kline_row_count:    K 线时间索引的行数
        diagnostics:        详细诊断信息
    """

    aligned_frame: pd.DataFrame
    missing_ratio: float
    fill_ratio: float
    source_row_count: int
    kline_row_count: int

    @property
    def is_usable(self) -> bool:
        """缺失比例 < 100% 时认为可用（即使部分缺失）。"""
        return self.missing_ratio < 1.0

    def diagnostics(self) -> dict[str, Any]:
        return {
            "source_row_count": self.source_row_count,
            "kline_row_count": self.kline_row_count,
            "missing_ratio": round(self.missing_ratio, 4),
            "fill_ratio": round(self.fill_ratio, 4),
            "is_usable": self.is_usable,
            "output_columns": list(self.aligned_frame.columns),
        }


class SourceAligner:
    """
    多源数据时间对齐器。

    将外部数据源（SourceFrame）对齐到给定的 K 线时间索引。

    Args:
        config: AlignmentConfig 对齐配置
    """

    def __init__(self, config: Optional[AlignmentConfig] = None) -> None:
        self.config = config or AlignmentConfig()

    def align(
        self,
        source: SourceFrame,
        kline_index: pd.DatetimeIndex,
    ) -> AlignmentResult:
        """
        将 SourceFrame 对齐到 K 线时间索引。

        Args:
            source:       外部数据源帧（frame.index 必须是 UTC DatetimeIndex）
            kline_index:  K 线时间索引（UTC DatetimeIndex），作为对齐基准

        Returns:
            AlignmentResult 对齐结果
        """
        kline_row_count = len(kline_index)
        source_row_count = len(source.frame)

        # ── 特殊情况：source 为空 ─────────────────────────────────
        if source.is_empty:
            log.warning(
                "[SourceAlign] source={} 数据帧为空，返回全 NaN 对齐结果",
                source.source_name,
            )
            empty = pd.DataFrame(index=kline_index)
            return AlignmentResult(
                aligned_frame=empty,
                missing_ratio=1.0,
                fill_ratio=0.0,
                source_row_count=0,
                kline_row_count=kline_row_count,
            )

        # ── 特殊情况：kline_index 为空 ────────────────────────────
        if kline_row_count == 0:
            return AlignmentResult(
                aligned_frame=pd.DataFrame(),
                missing_ratio=0.0,
                fill_ratio=0.0,
                source_row_count=source_row_count,
                kline_row_count=0,
            )

        src_df = source.frame.copy()

        # 确保索引是 DatetimeIndex 且 UTC-aware
        if not isinstance(src_df.index, pd.DatetimeIndex):
            raise ValueError(
                f"[SourceAlign] source={source.source_name} 的 frame.index "
                f"必须是 DatetimeIndex，实际类型: {type(src_df.index)}"
            )
        if src_df.index.tz is None:
            src_df.index = src_df.index.tz_localize("UTC")
        else:
            src_df.index = src_df.index.tz_convert("UTC")

        kline_idx_utc = kline_index
        if kline_idx_utc.tz is None:
            kline_idx_utc = kline_idx_utc.tz_localize("UTC")
        else:
            kline_idx_utc = kline_idx_utc.tz_convert("UTC")

        # 构造 K 线骨架 DataFrame（用于 merge_asof）
        kline_frame = pd.DataFrame(index=kline_idx_utc)
        kline_frame["__kline_ts__"] = kline_frame.index

        src_df = src_df.sort_index()
        src_df["__src_ts__"] = src_df.index

        # ── merge_asof：向后看对齐（不引入未来数据）────────────────
        # direction="backward" 表示：K 线时刻只匹配 <= 该时刻的最新外部数据点
        tolerance = None
        if self.config.timestamp_tolerance_sec > 0:
            tolerance = pd.Timedelta(seconds=self.config.timestamp_tolerance_sec)

        merged = pd.merge_asof(
            kline_frame.reset_index(),
            src_df.reset_index(drop=True),
            left_on="__kline_ts__",
            right_on="__src_ts__",
            direction="backward",
            tolerance=tolerance,
        ).set_index("index")

        # 删除辅助列（先保留 __src_ts__ 用于 gap 计算）
        data_cols = [
            c for c in merged.columns
            if c not in ("__kline_ts__", "__src_ts__")
        ]

        # ── 期间数量限制：mask 超出 max_fill_periods 的匹配行 ─────
        # merge_asof 默认对所有行做无限后向匹配；用 max_fill_periods 约束
        # 超出范围的匹配行置 NaN（防止陈旧数据无限传播）
        if self.config.max_fill_periods >= 0 and "__src_ts__" in merged.columns:
            if len(kline_idx_utc) >= 2:
                period_dur = (kline_idx_utc[-1] - kline_idx_utc[0]) / max(
                    len(kline_idx_utc) - 1, 1
                )
            else:
                period_dur = pd.Timedelta(hours=1)
            max_gap = period_dur * self.config.max_fill_periods
            gap = merged["__kline_ts__"] - merged["__src_ts__"]
            too_far_mask = gap.isna() | (gap > max_gap)
            if data_cols and too_far_mask.any():
                merged.loc[too_far_mask, data_cols] = float("nan")

        merged = merged.drop(columns=["__kline_ts__", "__src_ts__"], errors="ignore")
        # 还原 UTC 索引
        merged.index = kline_idx_utc

        n_nan_after = (
            merged[data_cols].isna().any(axis=1).sum() if data_cols else kline_row_count
        )
        missing_ratio = n_nan_after / kline_row_count if kline_row_count > 0 else 0.0
        fill_ratio = 1.0 - missing_ratio

        if self.config.drop_empty_rows and data_cols:
            merged = merged.dropna(how="all", subset=data_cols)

        log.debug(
            "[SourceAlign] source={} aligned: klines={} src_rows={} "
            "missing={:.1%} filled={:.1%}",
            source.source_name,
            kline_row_count,
            source_row_count,
            missing_ratio,
            fill_ratio,
        )

        return AlignmentResult(
            aligned_frame=merged,
            missing_ratio=missing_ratio,
            fill_ratio=fill_ratio,
            source_row_count=source_row_count,
            kline_row_count=kline_row_count,
        )

    def align_multiple(
        self,
        sources: list[SourceFrame],
        kline_index: pd.DatetimeIndex,
    ) -> dict[str, AlignmentResult]:
        """
        对多个 SourceFrame 批量对齐到同一 K 线索引。

        Returns:
            dict: source_name -> AlignmentResult
        """
        results: dict[str, AlignmentResult] = {}
        for src in sources:
            results[src.source_name] = self.align(src, kline_index)
        return results
