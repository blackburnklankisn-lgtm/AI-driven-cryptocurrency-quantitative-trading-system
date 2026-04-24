"""
modules/evolution/state_store.py — 演进状态原子持久化

设计说明：
- 所有 SelfEvolutionEngine 运行时状态的持久化后端
- 原子写入（tmp file → os.replace），防止半写状态
- 存储：
    * 决策历史（decisions.jsonl — append-only audit log）
    * 淘汰记录（retirements.jsonl — append-only）
    * 最新演进报告（latest_report.json）
    * 调度器状态（scheduler_state.json）
    * 周级参数优化状态（weekly_params_optimizer_state.json）
    * 周级参数优化运行审计（weekly_params_optimizer_runs.jsonl）
- 线程安全（RLock）

日志标签：[Evolution]
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.alpha.contracts.evolution_types import (
    EvolutionReport,
    PromotionDecision,
    RetirementRecord,
)

log = get_logger(__name__)

_DEFAULT_STATE_DIR = "storage/evolution"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


class EvolutionStateStore:
    """
    演进引擎状态持久化存储。

    - decisions.jsonl:       追加式决策审计日志（每行一个 JSON）
    - retirements.jsonl:     追加式淘汰记录
    - latest_report.json:    最新演进报告
    - scheduler_state.json:  调度器状态
    """

    def __init__(self, state_dir: str = _DEFAULT_STATE_DIR) -> None:
        self._dir = state_dir
        self._lock = threading.RLock()
        os.makedirs(state_dir, exist_ok=True)
        log.info("[Evolution] EvolutionStateStore 初始化: dir={}", state_dir)

    # ─────────────────────────────────────────────
    # 决策审计日志（append-only jsonl）
    # ─────────────────────────────────────────────

    def append_decision(self, decision: PromotionDecision) -> None:
        """追加一条晋升/降级决策到审计日志。"""
        path = os.path.join(self._dir, "decisions.jsonl")
        line = json.dumps(asdict(decision), default=_json_default, ensure_ascii=False)
        self._append_line(path, line)
        log.debug("[Evolution] 决策审计: id={} action={} {}→{}",
                  decision.candidate_id, decision.action,
                  decision.from_status, decision.to_status)

    def append_decisions(self, decisions: list[PromotionDecision]) -> None:
        for d in decisions:
            self.append_decision(d)

    def load_decisions(self, limit: int = 500) -> list[dict[str, Any]]:
        """从审计日志加载最近 N 条决策（最新优先）。"""
        path = os.path.join(self._dir, "decisions.jsonl")
        return self._load_jsonl_tail(path, limit)

    # ─────────────────────────────────────────────
    # 淘汰记录（append-only jsonl）
    # ─────────────────────────────────────────────

    def append_retirement(self, record: RetirementRecord) -> None:
        """追加一条淘汰记录。"""
        path = os.path.join(self._dir, "retirements.jsonl")
        line = json.dumps(asdict(record), default=_json_default, ensure_ascii=False)
        self._append_line(path, line)
        log.info("[Retirement] 淘汰记录写入: id={} reasons={}",
                 record.candidate_id, record.reason_codes)

    def load_retirements(self, limit: int = 200) -> list[dict[str, Any]]:
        path = os.path.join(self._dir, "retirements.jsonl")
        return self._load_jsonl_tail(path, limit)

    # ─────────────────────────────────────────────
    # 最新演进报告（原子覆写）
    # ─────────────────────────────────────────────

    def save_report(self, report: EvolutionReport) -> None:
        """原子写入最新演进报告。"""
        path = os.path.join(self._dir, "latest_report.json")
        data = asdict(report)
        self._atomic_write(path, data)
        log.info("[Evolution] 报告已保存: id={}", report.report_id)

    def load_report(self) -> Optional[dict[str, Any]]:
        """加载最新演进报告，不存在时返回 None。"""
        path = os.path.join(self._dir, "latest_report.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log.exception("[Evolution] 报告加载失败: path={}", path)
            return None

    # ─────────────────────────────────────────────
    # 调度器状态（原子覆写）
    # ─────────────────────────────────────────────

    def save_scheduler_state(self, state: dict[str, Any]) -> None:
        path = os.path.join(self._dir, "scheduler_state.json")
        self._atomic_write(path, state)

    def load_scheduler_state(self) -> dict[str, Any]:
        path = os.path.join(self._dir, "scheduler_state.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log.exception("[Evolution] 调度器状态加载失败")
            return {}

    # ─────────────────────────────────────────────
    # 周级参数优化状态与审计
    # ─────────────────────────────────────────────

    def save_weekly_params_optimizer_state(self, state: dict[str, Any]) -> None:
        path = os.path.join(self._dir, "weekly_params_optimizer_state.json")
        self._atomic_write(path, state)

    def load_weekly_params_optimizer_state(self) -> dict[str, Any]:
        path = os.path.join(self._dir, "weekly_params_optimizer_state.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log.exception("[Evolution] 周级参数优化状态加载失败")
            return {}

    def append_weekly_params_optimizer_run(self, record: dict[str, Any]) -> None:
        path = os.path.join(self._dir, "weekly_params_optimizer_runs.jsonl")
        line = json.dumps(record, default=_json_default, ensure_ascii=False)
        self._append_line(path, line)

    def load_weekly_params_optimizer_runs(
        self,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        path = os.path.join(self._dir, "weekly_params_optimizer_runs.jsonl")
        return self._load_jsonl_tail(path, limit)

    # ─────────────────────────────────────────────
    # 通用辅助
    # ─────────────────────────────────────────────

    def _atomic_write(self, path: str, data: Any) -> None:
        tmp = path + ".tmp"
        with self._lock:
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, default=_json_default, ensure_ascii=False)
                os.replace(tmp, path)
            except Exception:
                log.exception("[Evolution] 原子写入失败: path={}", path)
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

    def _append_line(self, path: str, line: str) -> None:
        with self._lock:
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                log.exception("[Evolution] 追加写入失败: path={}", path)

    @staticmethod
    def _load_jsonl_tail(path: str, limit: int) -> list[dict[str, Any]]:
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            # 最新在最后，取最后 limit 行并反转（新→旧）
            tail = lines[-limit:] if len(lines) > limit else lines
            result = []
            for line in reversed(tail):
                line = line.strip()
                if line:
                    result.append(json.loads(line))
            return result
        except Exception:
            log.exception("[Evolution] JSONL 加载失败: path={}", path)
            return []

    def diagnostics(self) -> dict[str, Any]:
        decisions_path = os.path.join(self._dir, "decisions.jsonl")
        retirements_path = os.path.join(self._dir, "retirements.jsonl")
        report_path = os.path.join(self._dir, "latest_report.json")
        weekly_params_state_path = os.path.join(
            self._dir,
            "weekly_params_optimizer_state.json",
        )
        weekly_params_runs_path = os.path.join(
            self._dir,
            "weekly_params_optimizer_runs.jsonl",
        )

        def _line_count(p: str) -> int:
            if not os.path.exists(p):
                return 0
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return sum(1 for _ in f)
            except Exception:
                return -1

        return {
            "state_dir": self._dir,
            "total_decisions": _line_count(decisions_path),
            "total_retirements": _line_count(retirements_path),
            "has_latest_report": os.path.exists(report_path),
            "has_weekly_params_optimizer_state": os.path.exists(
                weekly_params_state_path
            ),
            "total_weekly_params_optimizer_runs": _line_count(
                weekly_params_runs_path
            ),
        }
