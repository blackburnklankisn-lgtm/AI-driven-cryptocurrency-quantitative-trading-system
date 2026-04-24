"""
modules/alpha/regime/cache.py — Regime 历史缓存

设计说明：
- 保存最近 N 次 RegimeState 的历史记录
- 用于检测 regime 切换（RegimeShift）并打印醒目日志
- 提供稳定性判断（短期内切换过于频繁则标记为 unstable）
- 线程安全（使用 deque + 无锁读写，单线程事件循环下无需 Lock）

日志标签：[RegimeCache] [RegimeShift]
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from core.logger import get_logger
from modules.alpha.contracts.regime_types import RegimeName, RegimeState

log = get_logger(__name__)


@dataclass
class RegimeSnapshot:
    """一次 Regime 评分的完整快照（含时间戳）。"""
    regime: RegimeState
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    bar_seq: int = 0    # 对应的 loop_seq


class RegimeCache:
    """
    Regime 历史缓存。

    功能：
        1. store(regime, bar_seq)    — 保存当前 regime，检测切换
        2. latest                   — 最新 RegimeState（快速读取）
        3. is_stable(window)        — 最近 window 个结果是否保持一致
        4. history                  — 只读快照列表（调试用）

    Args:
        maxlen:          缓存保留的最大记录数（超出后自动淘汰最旧记录）
        shift_log_min_conf: 只有置信度超过此阈值的切换才打印 RegimeShift 日志
    """

    def __init__(
        self,
        maxlen: int = 200,
        shift_log_min_conf: float = 0.5,
    ) -> None:
        self._maxlen = maxlen
        self._shift_log_min_conf = shift_log_min_conf
        self._history: deque[RegimeSnapshot] = deque(maxlen=maxlen)
        self._last_dominant: RegimeName = "unknown"

    def store(self, regime: RegimeState, bar_seq: int = 0) -> bool:
        """
        保存新的 Regime 状态，检测是否发生切换。

        Args:
            regime:   新的 RegimeState
            bar_seq:  当前 loop_seq（用于快照记录）

        Returns:
            True 如果发生了 dominant_regime 切换，否则 False
        """
        snapshot = RegimeSnapshot(regime=regime, bar_seq=bar_seq)
        self._history.append(snapshot)

        shifted = regime.dominant_regime != self._last_dominant
        if shifted:
            if (
                regime.confidence >= self._shift_log_min_conf
                or self._last_dominant == "unknown"
            ):
                log.info(
                    "[RegimeShift] 市场环境切换: {} → {} "
                    "(bull={:.3f} bear={:.3f} sideways={:.3f} high_vol={:.3f} conf={:.3f}) "
                    "bar_seq={}",
                    self._last_dominant, regime.dominant_regime,
                    regime.bull_prob, regime.bear_prob,
                    regime.sideways_prob, regime.high_vol_prob,
                    regime.confidence, bar_seq,
                )
            else:
                log.debug(
                    "[RegimeShift] 低置信切换: {} → {} conf={:.3f} bar_seq={}",
                    self._last_dominant, regime.dominant_regime,
                    regime.confidence, bar_seq,
                )
            self._last_dominant = regime.dominant_regime

        return shifted

    @property
    def latest(self) -> Optional[RegimeState]:
        """最新 RegimeState，无记录时返回 None。"""
        if not self._history:
            return None
        return self._history[-1].regime

    @property
    def latest_dominant(self) -> RegimeName:
        """最新 dominant regime 名称（无记录时为 'unknown'）。"""
        if not self._history:
            return "unknown"
        return self._history[-1].regime.dominant_regime

    def is_stable(self, window: int = 5) -> bool:
        """
        判断最近 window 次记录的 dominant_regime 是否完全一致。

        Args:
            window: 检查最近几条记录

        Returns:
            True 如果全部一致（且缓存中至少有 window 条记录）
        """
        recent = list(self._history)[-window:]
        if len(recent) < window:
            return False
        names = {snap.regime.dominant_regime for snap in recent}
        return len(names) == 1

    def regime_counts(self, window: int = 20) -> dict[RegimeName, int]:
        """统计最近 window 条记录中各 regime 出现次数（调试用）。"""
        recent = list(self._history)[-window:]
        counts: dict[str, int] = {"bull": 0, "bear": 0, "sideways": 0, "high_vol": 0, "unknown": 0}
        for snap in recent:
            key = snap.regime.dominant_regime
            counts[key] = counts.get(key, 0) + 1
        return counts  # type: ignore[return-value]

    @property
    def history(self) -> list[RegimeSnapshot]:
        """只读快照列表（调试 / trace 回放用）。"""
        return list(self._history)

    def __len__(self) -> int:
        return len(self._history)

    def diagnostics(self) -> dict:
        """诊断快照（用于 health_snapshot / 日志输出）。"""
        return {
            "cache_size": len(self._history),
            "max_size": self._maxlen,
            "latest_dominant": self.latest_dominant,
            "last_10_dominants": [
                snap.regime.dominant_regime for snap in list(self._history)[-10:]
            ],
            "stable_last_5": self.is_stable(5),
            "regime_counts_last20": self.regime_counts(20),
        }
