"""
modules/monitoring/trace.py — Phase 3 统一 Trace 上下文

职责：
- 为做市 tick、RL step、演进周期生成结构化 trace_id，打通三个决策域的可观测性链路
- 提供全局 Phase3TraceRecorder，将结构化决策事件追加写入
  logs/phase3_trace.jsonl（与 Phase 1 的 phase1_trace.jsonl 分离）
- 线程安全：per-domain 单调序列号由 threading.Lock 保护
- 写入失败只记录 warning，不抛异常（不阻断交易主路径）

trace_id 格式：
    {domain}[-{qualifier}]-{TS_SHORT}-{SEQ:06d}

    domain:    mm  (做市 tick)
               rl  (RL step / predict)
               ev  (演进周期)
               bar (K线循环，Phase 1 已有，此处不重新实现)
    qualifier: 可选（如 symbol，斜杠等特殊字符自动剥离）
    TS_SHORT:  YYYYMMDDTHHMMSSZ (UTC, second-level precision)
    SEQ:       进程内每个 domain 独立的单调递增序号

示例：
    mm-BTCUSDT-20260424T103012Z-000001   # 第 1 次做市 tick（BTC/USDT）
    rl-20260424T103015Z-000005           # 第 5 次 RL 推理
    ev-20260424T030001Z-000001           # 第 1 次演进周期

使用方式：
    from modules.monitoring.trace import generate_trace_id, get_recorder

    trace_id = generate_trace_id("mm", symbol)
    log.debug("[MarketMaking] tick: trace_id={} ...", trace_id, ...)
    get_recorder().record(trace_id, "mm", "TICK_END", {"bid": ..., "ask": ...})
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.logger import get_logger

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、trace_id 生成器
# ══════════════════════════════════════════════════════════════

# per-domain 序列号计数器（线程安全）
_counters: dict[str, int] = {}
_counter_lock = threading.Lock()


def generate_trace_id(domain: str, qualifier: str = "") -> str:
    """
    生成结构化 trace_id。

    Args:
        domain:    决策域（mm / rl / ev）
        qualifier: 可选限定符（如 symbol），斜杠等特殊字符自动剥离

    Returns:
        格式为 "{domain}[-{qualifier}]-{TS_SHORT}-{SEQ:06d}" 的不可变字符串
    """
    ts_short = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with _counter_lock:
        seq = _counters.get(domain, 0) + 1
        _counters[domain] = seq

    parts = [domain]
    if qualifier:
        # 剥离 "/" ":" " " 等特殊字符
        clean = qualifier.replace("/", "").replace(":", "").replace(" ", "")
        if clean:
            parts.append(clean)
    parts.extend([ts_short, f"{seq:06d}"])
    return "-".join(parts)


# ══════════════════════════════════════════════════════════════
# 二、Phase3TraceRecorder — 结构化 JSONL 追加器
# ══════════════════════════════════════════════════════════════

class Phase3TraceRecorder:
    """
    将 Phase 3 关键决策点的 trace 事件追加写入 JSONL 文件。

    每次 record() 写入一行 JSON，包含：
        trace_id, domain, event_type, ts_utc, 以及任意附加 payload

    线程安全：文件写入由 threading.Lock 保护。
    写入失败只记录 warning，不抛异常（不阻断交易主路径）。

    文件路径默认为 logs/phase3_trace.jsonl，与 Phase 1 的
    logs/phase1_trace.jsonl 分离，避免日志混杂。
    """

    def __init__(self, path: str = "logs/phase3_trace.jsonl") -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._enabled: bool = True
        log.debug("[Trace] Phase3TraceRecorder 初始化: path={}", self._path)

    def record(
        self,
        trace_id: str,
        domain: str,
        event_type: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        追加一条结构化 trace 事件。

        Args:
            trace_id:   本次决策的 trace_id（由 generate_trace_id() 生成）
            domain:     决策域（mm / rl / ev）
            event_type: 事件类型（如 TICK_END / RL_PREDICT / CYCLE_START / CYCLE_END）
            payload:    附加结构化字段（任意 JSON-serializable 值）
        """
        if not self._enabled:
            return
        entry: dict[str, Any] = {
            "trace_id": trace_id,
            "domain": domain,
            "event_type": event_type,
            "ts_utc": datetime.now(tz=timezone.utc).isoformat(),
        }
        if payload:
            entry.update(payload)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            try:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                log.warning("[Trace] 写入 trace 文件失败: {}", exc)

    def disable(self) -> None:
        """禁用 JSONL 写入（用于单元测试等场景）。"""
        self._enabled = False

    def enable(self) -> None:
        """重新启用 JSONL 写入。"""
        self._enabled = True

    @property
    def path(self) -> Path:
        """JSONL 文件路径（只读）。"""
        return self._path


# ══════════════════════════════════════════════════════════════
# 三、模块级默认全局 recorder（懒加载单例）
# ══════════════════════════════════════════════════════════════

_default_recorder: Optional[Phase3TraceRecorder] = None
_recorder_init_lock = threading.Lock()


def init_recorder(path: str = "logs/phase3_trace.jsonl") -> Phase3TraceRecorder:
    """
    初始化（或替换）默认全局 recorder，应在程序启动时调用一次。

    Args:
        path: JSONL 文件路径（默认 logs/phase3_trace.jsonl）

    Returns:
        已初始化的 Phase3TraceRecorder 实例
    """
    global _default_recorder  # noqa: PLW0603
    with _recorder_init_lock:
        _default_recorder = Phase3TraceRecorder(path)
    log.info("[Trace] Phase3TraceRecorder 全局实例已初始化: path={}", path)
    return _default_recorder


def get_recorder() -> Phase3TraceRecorder:
    """
    获取默认全局 recorder。

    若 init_recorder() 未曾调用过，则使用默认路径自动懒加载。
    """
    global _default_recorder  # noqa: PLW0603
    if _default_recorder is None:
        with _recorder_init_lock:
            if _default_recorder is None:
                _default_recorder = Phase3TraceRecorder()
    return _default_recorder
