"""
modules/alpha/orchestration/performance_store.py — 策略近期表现存储

设计说明：
- 保存每个 strategy_id 最近 N 次 StrategyResult 的执行记录
- 计算近期命中率（hit rate）、信号频率、平均置信度
- 不依赖具体策略对象，只消费结构化的 StrategyResult
- 线程安全：单线程事件循环下 deque 操作无锁安全

日志标签：[PerfStore]
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

from core.logger import get_logger
from modules.alpha.contracts.strategy_result import StrategyAction, StrategyResult

log = get_logger(__name__)


@dataclass
class ResultRecord:
    """单次策略输出记录（轻量，不持有完整 StrategyResult 以节省内存）。"""
    strategy_id: str
    symbol: str
    action: StrategyAction
    confidence: float
    score: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    bar_seq: int = 0


@dataclass
class StrategyPerformance:
    """单个策略的聚合表现指标快照（只读）。"""
    strategy_id: str
    sample_count: int           # 统计窗口内样本数
    hit_rate_buy: float         # BUY 信号频率
    hit_rate_sell: float        # SELL 信号频率
    avg_confidence: float       # 平均置信度
    avg_score: float            # 平均 score
    last_action: StrategyAction # 最近一次动作
    last_confidence: float      # 最近一次置信度


class PerformanceStore:
    """
    策略近期表现存储器。

    使用示例：
        store = PerformanceStore(window=50)
        store.record(strategy_result, bar_seq=loop_seq)

        # 查询聚合表现
        perf = store.get_performance("my_strategy_id")
        all_perfs = store.all_performances()

    Args:
        window:    每个策略保留的最大历史记录数
        min_count: 计算有效统计所需的最少样本数（不足时返回 None）
    """

    def __init__(self, window: int = 50, min_count: int = 5) -> None:
        self._window = window
        self._min_count = min_count
        # strategy_id -> deque[ResultRecord]
        self._store: Dict[str, deque] = {}

    # ────────────────────────────────────────────────────────────
    # 写入
    # ────────────────────────────────────────────────────────────

    def record(self, result: StrategyResult, bar_seq: int = 0) -> None:
        """
        记录一次策略输出。

        Args:
            result:  StrategyResult 实例
            bar_seq: 当前 loop_seq（用于记录时序）
        """
        sid = result.strategy_id
        if sid not in self._store:
            self._store[sid] = deque(maxlen=self._window)

        rec = ResultRecord(
            strategy_id=sid,
            symbol=result.symbol,
            action=result.action,
            confidence=result.confidence,
            score=result.score,
            bar_seq=bar_seq,
        )
        self._store[sid].append(rec)

        log.debug(
            "[PerfStore] 记录: strategy={} action={} conf={:.3f} score={:.3f} bar_seq={}",
            sid, result.action, result.confidence, result.score, bar_seq,
        )

    # ────────────────────────────────────────────────────────────
    # 读取
    # ────────────────────────────────────────────────────────────

    def get_performance(self, strategy_id: str) -> Optional[StrategyPerformance]:
        """
        查询指定策略的聚合表现。

        Args:
            strategy_id: 策略 ID

        Returns:
            StrategyPerformance 或 None（样本不足时）
        """
        records = list(self._store.get(strategy_id, []))
        if len(records) < self._min_count:
            return None

        n = len(records)
        buy_count = sum(1 for r in records if r.action == "BUY")
        sell_count = sum(1 for r in records if r.action == "SELL")
        avg_conf = sum(r.confidence for r in records) / n
        avg_score = sum(r.score for r in records) / n
        last = records[-1]

        return StrategyPerformance(
            strategy_id=strategy_id,
            sample_count=n,
            hit_rate_buy=buy_count / n,
            hit_rate_sell=sell_count / n,
            avg_confidence=avg_conf,
            avg_score=avg_score,
            last_action=last.action,
            last_confidence=last.confidence,
        )

    def all_performances(self) -> Dict[str, Optional[StrategyPerformance]]:
        """返回所有已记录策略的聚合表现。"""
        return {sid: self.get_performance(sid) for sid in self._store}

    def known_strategy_ids(self) -> list[str]:
        """返回所有已记录的策略 ID 列表。"""
        return list(self._store.keys())

    def sample_count(self, strategy_id: str) -> int:
        """返回指定策略的历史记录数量。"""
        return len(self._store.get(strategy_id, []))

    def diagnostics(self) -> dict:
        """诊断快照。"""
        return {
            "strategies_tracked": len(self._store),
            "window": self._window,
            "min_count": self._min_count,
            "sample_counts": {sid: len(q) for sid, q in self._store.items()},
        }
