"""
modules/evolution/scheduler.py — 演进周期调度器

设计说明：
- 管理演进调度周期（上次运行时间、下次运行时间、运行间隔）
- 支持：
    * 周期触发（每 N 秒 / 每天 / 每周）
    * 手动强制触发 (force_run=True)
    * 冷却期（上次运行后必须等待 cooldown_sec）
- 不包含实际业务逻辑，只判断"是否该运行"并记录运行历史
- 与 EvolutionStateStore 配合持久化调度状态

日志标签：[Evolution]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.evolution.state_store import EvolutionStateStore

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class SchedulerConfig:
    """
    调度器配置。

    Attributes:
        interval_sec:        正常调度间隔（秒），默认 7 天（604800s）
        cooldown_sec:        上次运行后最短冷却期（秒），默认 1 小时
        max_history:         保留的运行历史条数
        daily_eval_enabled:  是否开启每日评估（间隔 86400s 以下时有效）
    """

    interval_sec: float = 7 * 24 * 3600.0   # 7 天
    cooldown_sec: float = 3600.0             # 1 小时
    max_history: int = 50
    daily_eval_enabled: bool = True


# ══════════════════════════════════════════════════════════════
# 二、EvolutionScheduler 主体
# ══════════════════════════════════════════════════════════════

class EvolutionScheduler:
    """
    演进调度器（无业务逻辑，只管"什么时候该跑"）。

    使用方式::

        scheduler = EvolutionScheduler(config, state_store)
        if scheduler.should_run():
            engine.run_cycle()
            scheduler.record_run(success=True)
    """

    def __init__(
        self,
        config: Optional[SchedulerConfig] = None,
        state_store: Optional[EvolutionStateStore] = None,
    ) -> None:
        self._config = config or SchedulerConfig()
        self._store = state_store
        self._history: list[dict[str, Any]] = []
        self._last_run_at: Optional[datetime] = None
        self._run_count: int = 0

        self._load_state()
        log.info("[Evolution] EvolutionScheduler 初始化: interval_sec={} cooldown_sec={}",
                 self._config.interval_sec, self._config.cooldown_sec)

    def should_run(self, force: bool = False) -> bool:
        """
        判断当前是否应触发演进周期。

        Args:
            force: 若为 True，忽略冷却期与间隔，强制返回 True

        Returns:
            True = 应运行；False = 还未到时间
        """
        if force:
            log.info("[Evolution] 强制触发演进调度")
            return True

        now = datetime.now(tz=timezone.utc)

        # 冷却期检查
        if self._last_run_at is not None:
            elapsed = (now - self._last_run_at).total_seconds()
            if elapsed < self._config.cooldown_sec:
                log.debug("[Evolution] 冷却期内，跳过: elapsed={:.0f}s cooldown={}s",
                          elapsed, self._config.cooldown_sec)
                return False

        # 间隔检查
        if self._last_run_at is None:
            return True  # 首次运行

        elapsed = (now - self._last_run_at).total_seconds()
        if elapsed >= self._config.interval_sec:
            return True

        log.debug("[Evolution] 间隔未到: elapsed={:.0f}s interval={}s",
                  elapsed, self._config.interval_sec)
        return False

    def record_run(
        self,
        success: bool,
        candidates_evaluated: int = 0,
        promotions: int = 0,
        retirements: int = 0,
        error: Optional[str] = None,
    ) -> None:
        """
        记录一次演进周期的运行结果。

        Args:
            success:               是否成功
            candidates_evaluated:  评估候选数
            promotions:            晋升数
            retirements:           淘汰数
            error:                 失败时的错误信息
        """
        now = datetime.now(tz=timezone.utc)
        self._last_run_at = now
        self._run_count += 1

        entry: dict[str, Any] = {
            "run_id": self._run_count,
            "ran_at": now.isoformat(),
            "success": success,
            "candidates_evaluated": candidates_evaluated,
            "promotions": promotions,
            "retirements": retirements,
        }
        if error:
            entry["error"] = error

        self._history.append(entry)
        # 保留最近 N 条
        if len(self._history) > self._config.max_history:
            self._history = self._history[-self._config.max_history:]

        self._save_state()
        log.info("[Evolution] 调度记录: run_id={} success={} promotions={} retirements={}",
                 self._run_count, success, promotions, retirements)

    def last_run_at(self) -> Optional[datetime]:
        return self._last_run_at

    def run_count(self) -> int:
        return self._run_count

    def next_run_at(self) -> Optional[datetime]:
        if self._last_run_at is None:
            return datetime.now(tz=timezone.utc)
        return self._last_run_at + timedelta(seconds=self._config.interval_sec)

    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "run_count": self._run_count,
            "last_run_at": self._last_run_at.isoformat() if self._last_run_at else None,
            "next_run_at": self.next_run_at().isoformat() if self.next_run_at() else None,
            "interval_sec": self._config.interval_sec,
            "cooldown_sec": self._config.cooldown_sec,
            "history_len": len(self._history),
        }

    # ─────────────────────────────────────────────
    # 状态持久化
    # ─────────────────────────────────────────────

    def _save_state(self) -> None:
        if self._store is None:
            return
        state = {
            "last_run_at": self._last_run_at.isoformat() if self._last_run_at else None,
            "run_count": self._run_count,
            "history": self._history,
        }
        self._store.save_scheduler_state(state)

    def _load_state(self) -> None:
        if self._store is None:
            return
        state = self._store.load_scheduler_state()
        if not state:
            return
        last_run_str = state.get("last_run_at")
        if last_run_str:
            try:
                self._last_run_at = datetime.fromisoformat(last_run_str)
            except ValueError:
                pass
        self._run_count = state.get("run_count", 0)
        self._history = state.get("history", [])
        log.debug("[Evolution] 调度器状态已恢复: run_count={} last_run={}",
                  self._run_count, last_run_str)
