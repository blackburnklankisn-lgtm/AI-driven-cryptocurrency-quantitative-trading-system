"""
apps/api/server.py — FastAPI 后端桥接服务

提供给外部桌面客户端（Electron）的 REST 控制信道与 WebSocket 实时推送信道。

改进记录：
- v1.1: WebSocket 连接管理器添加死连接自动清理（阶段01 连通性压测）
- v1.1: WebsocketLogSink 添加 asyncio.Queue 背压控制，防止高频日志拥塞（阶段01）
- v1.1: /api/v1/status 扩展返回 circuit_reason 和 risk_state_summary（阶段03）
- v1.1: /api/v1/control 添加 trigger_circuit_test 动作用于测试（阶段03）
- v1.1: 新增 /api/v1/ws/status 通道，服务端主动推送状态变更（阶段04 性能优化）
"""

import asyncio
from dataclasses import asdict, is_dataclass
import json
import math
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from core.logger import get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """应用生命周期管理（替代已废弃的 on_event）。"""
    # 注册主线程 event loop 到 WebsocketLogSink，使子线程日志能跨线程推送
    WebsocketLogSink.set_main_loop(asyncio.get_running_loop())
    asyncio.create_task(_status_push_worker())
    asyncio.create_task(_ticker_refresh_worker())
    yield


app = FastAPI(title="AI Quant Trader API", version="1.1.0", lifespan=lifespan)

# 允许跨域（Electron UI 通常从 localhost/file 启动）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# ── 全局状态注入 ──────────────────────────────────────────────
# 由 main.py 在启动时将 LiveTrader 实例挂载在此
_global_trader_instance = None


def set_trader_instance(trader) -> None:
    global _global_trader_instance
    _global_trader_instance = trader
    log.info("API: Trader instance set. id(trader)={}", id(trader))


# ── WebSocket 连接管理器（带死连接清理） ─────────────────────

class ConnectionManager:
    """
    管理所有活跃的 WebSocket 连接。
    
    改进：
    - broadcast 时自动清理已断开的死连接
    - 支持按通道分组（logs / status）
    """

    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        """广播消息，自动清理发送失败的死连接。"""
        dead_connections = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                # 发送失败说明连接已断开，标记为死连接
                dead_connections.append(connection)
        # 清理死连接
        for dead in dead_connections:
            self.disconnect(dead)

    def connection_count(self) -> int:
        return len(self.active_connections)


# 日志流管理器
log_manager = ConnectionManager()
# 状态推送管理器（独立通道，用于主动推送系统状态）
status_manager = ConnectionManager()
dashboard_manager = ConnectionManager()
risk_manager_ws = ConnectionManager()
evolution_manager_ws = ConnectionManager()
data_health_manager_ws = ConnectionManager()
execution_manager_ws = ConnectionManager()
diagnostics_manager_ws = ConnectionManager()


# ── WebSocket 日志 Sink（带背压控制） ────────────────────────

class WebsocketLogSink:
    """
    Loguru Sink，接收日志并通过 WebSocket 推送到前端。

    修复：使用 run_coroutine_threadsafe 跨线程提交到主线程 event loop。
    交易逻辑运行在子线程，loguru 的 write() 在子线程中被调用，
    必须通过 run_coroutine_threadsafe 将广播任务提交到主线程的 asyncio loop。

    背压控制：内部使用线程安全的 queue.Queue（非 asyncio.Queue），
    避免跨线程访问 asyncio 原语。
    """
    import queue as _queue_module

    # 线程安全队列（maxsize 防止内存溢出）
    _sync_queue: "queue.Queue" = None
    _main_loop: asyncio.AbstractEventLoop = None
    _worker_future = None

    @classmethod
    def set_main_loop(cls, loop: asyncio.AbstractEventLoop):
        """由主线程在 uvicorn 启动后调用，注册主线程 event loop。"""
        cls._main_loop = loop
        import queue
        cls._sync_queue = queue.Queue(maxsize=500)

    def write(self, message: str):
        """同步写入接口（由 loguru 在任意线程调用）。"""
        loop = WebsocketLogSink._main_loop
        q = WebsocketLogSink._sync_queue
        if loop is None or q is None:
            return  # 主线程 loop 尚未注册，忽略
        # 背压控制：队列满时丢弃最旧的消息
        if q.full():
            try:
                q.get_nowait()
            except Exception:
                pass
        try:
            q.put_nowait(message)
        except Exception:
            pass
        # 提交广播任务到主线程 event loop（线程安全）
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(
                _drain_and_broadcast(q), loop
            )

    @staticmethod
    async def _drain_queue(q):
        """保留兼容性，实际由模块级 _drain_and_broadcast 处理。"""
        await _drain_and_broadcast(q)


async def _drain_and_broadcast(q):
    """从同步队列中取出所有待发送消息并广播到 WebSocket。"""
    import queue as _q
    msgs = []
    # 一次性取出所有待发消息
    while True:
        try:
            msg = q.get_nowait()
            msgs.append(msg)
        except _q.Empty:
            break
        except Exception:
            break
    # 批量广播
    if msgs:
        combined = "".join(msgs)
        try:
            await log_manager.broadcast(combined)
            _record_channel_broadcast("logs")
        except Exception as exc:
            _record_channel_broadcast("logs", str(exc))



# ── 状态推送后台任务 ─────────────────────────────────────────

async def _status_push_worker():
    """
    每 3 秒主动向 status WebSocket 通道推送系统状态。
    
    这样前端无需轮询 REST API，减少 HTTP 开销（阶段04 性能优化）。
    """
    while True:
        await asyncio.sleep(3)
        active_channels = {
            key: manager.connection_count()
            for key, manager in _channel_manager_map().items()
        }
        if sum(active_channels.values()) == 0:
            _record_worker_tick("status_push", extra={"active_channels": active_channels})
            continue

        await _broadcast_snapshot(status_manager, "status", _build_status_response)
        await _broadcast_snapshot(dashboard_manager, "dashboard", _build_dashboard_snapshot)
        await _broadcast_snapshot(risk_manager_ws, "risk", _build_risk_matrix_snapshot)
        await _broadcast_snapshot(evolution_manager_ws, "evolution", _build_evolution_snapshot)
        await _broadcast_snapshot(data_health_manager_ws, "data-health", _build_data_fusion_snapshot)
        await _broadcast_snapshot(execution_manager_ws, "execution", _build_execution_snapshot)
        await _broadcast_snapshot(diagnostics_manager_ws, "diagnostics", _build_diagnostics_snapshot)
        _record_worker_tick("status_push", extra={"active_channels": active_channels})


async def _ticker_refresh_worker():
    """
    每 5 秒通过 CCXT fetch_ticker 获取最新实时价格并更新 _latest_prices。

    背景：主循环 (_main_loop_step) 每 60 秒才跑一次，两次轮询之间
    `_latest_prices` 不会变化，导致 ws/status 推送的价格长达 60 秒不更新。
    此 worker 独立运行，在两次主循环之间保持价格新鲜度（约 5 s 延迟）。
    """
    _symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
    cycle = 0
    while True:
        await asyncio.sleep(5)
        trader = _global_trader_instance
        if not trader:
            _record_worker_tick("ticker_refresh", extra={"cycle": cycle, "updated_symbols": [], "error_count": 0})
            continue
        loop = asyncio.get_running_loop()
        symbols = getattr(trader, '_symbols', None) or _symbols
        cycle += 1
        updated = []
        error_count = 0
        current_time = time.time()
        for symbol in symbols:
            try:
                ticker = await loop.run_in_executor(
                    None, trader.gateway.fetch_ticker, symbol
                )
                last = ticker.get('last') or ticker.get('close')
                if last:
                    old = trader._latest_prices.get(symbol, 0)
                    trader._latest_prices[symbol] = float(last)
                    trader._latest_prices_updated_at[symbol] = current_time  # 记录更新时间戳
                    if old != float(last):
                        updated.append(f"{symbol}: {old:.4f}→{float(last):.4f}")
            except Exception as exc:  # noqa: BLE001
                error_count += 1
                log.debug("[Ticker] fetch_ticker 失败: {} {}", symbol, str(exc)[:80])
        if updated:
            log.debug("[Ticker] cycle#{} 价格更新: {}", cycle, " | ".join(updated))
        elif cycle % 12 == 0:  # 每 60s 打印一次"无变化"
            log.debug(
                "[Ticker] cycle#{} 价格无变化: {}",
                cycle,
                {s: f"{v:.4f}" for s, v in trader._latest_prices.items()},
            )
        _record_worker_tick(
            "ticker_refresh",
            extra={
                "cycle": cycle,
                "tracked_symbols": list(symbols),
                "updated_symbols": updated,
                "error_count": error_count,
            },
        )


# ── 状态构建辅助函数 ─────────────────────────────────────────

def _build_status_response() -> Dict[str, Any]:
    """构建系统状态响应（供 REST 和 WebSocket 共用）。"""
    trader = _global_trader_instance
    if not trader:
        return {"status": "inactive", "message": "Trader engine is not running"}

    circuit_broken = trader.risk_manager.is_circuit_broken()
    risk_summary = trader.risk_manager.get_state_summary()
    positions = {sym: float(qty) for sym, qty in trader._positions.items() if qty > 0}
    latest_prices = {sym: float(price) for sym, price in getattr(trader, '_latest_prices', {}).items()}

    return {
        "status": "running" if getattr(trader, "_running", False) else "stopped",
        "mode": getattr(trader, "mode", "unknown"),
        "exchange": getattr(trader.gateway, "exchange_id", "unknown"),
        "equity": float(trader._current_equity),
        "positions": positions,
        "circuit_broken": circuit_broken,
        "circuit_reason": risk_summary.get("circuit_reason", ""),
        "risk_state": risk_summary,
        "poll_interval_s": trader._poll_interval_s,
        "ws_log_connections": log_manager.connection_count(),
        "ws_diagnostics_connections": diagnostics_manager_ws.connection_count(),
        "ai_analysis": getattr(trader, "_last_ai_analysis", "N/A"),
        "latest_prices": latest_prices,
    }


def _safe_getattr(obj: Any, attr: str, default: Any = None) -> Any:
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


_SERVER_STARTED_AT = datetime.now(tz=timezone.utc)
_CHANNEL_REGISTRY: dict[str, dict[str, Any]] = {
    "logs": {
        "label": "audit-logs",
        "path": "/api/v1/ws/logs",
        "last_broadcast_at": None,
        "broadcast_count": 0,
        "last_error": None,
    },
    "status": {
        "label": "system-status",
        "path": "/api/v1/ws/status",
        "last_broadcast_at": None,
        "broadcast_count": 0,
        "last_error": None,
    },
    "dashboard": {
        "label": "dashboard",
        "path": "/api/v2/ws/dashboard",
        "last_broadcast_at": None,
        "broadcast_count": 0,
        "last_error": None,
    },
    "risk": {
        "label": "risk-matrix",
        "path": "/api/v2/ws/risk",
        "last_broadcast_at": None,
        "broadcast_count": 0,
        "last_error": None,
    },
    "evolution": {
        "label": "evolution",
        "path": "/api/v2/ws/evolution",
        "last_broadcast_at": None,
        "broadcast_count": 0,
        "last_error": None,
    },
    "data-health": {
        "label": "data-health",
        "path": "/api/v2/ws/data-health",
        "last_broadcast_at": None,
        "broadcast_count": 0,
        "last_error": None,
    },
    "execution": {
        "label": "execution",
        "path": "/api/v2/ws/execution",
        "last_broadcast_at": None,
        "broadcast_count": 0,
        "last_error": None,
    },
    "diagnostics": {
        "label": "diagnostics",
        "path": "/api/v2/ws/diagnostics",
        "last_broadcast_at": None,
        "broadcast_count": 0,
        "last_error": None,
    },
}
_WORKER_REGISTRY: dict[str, dict[str, Any]] = {
    "status_push": {
        "interval_sec": 3,
        "last_tick_at": None,
        "last_error": None,
        "success_count": 0,
    },
    "ticker_refresh": {
        "interval_sec": 5,
        "last_tick_at": None,
        "last_error": None,
        "success_count": 0,
        "cycle": 0,
        "updated_symbols": [],
        "error_count": 0,
    },
}


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(_sanitize_non_finite(value), default=str, allow_nan=False))
    except Exception:
        return str(value)


def _sanitize_non_finite(value: Any) -> Any:
    """Convert NaN/Infinity to JSON-safe null recursively."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _sanitize_non_finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_non_finite(v) for v in value]
    return value


def _json_dumps_safe(value: Any) -> str:
    """Serialize payload with strict JSON compatibility."""
    return json.dumps(_sanitize_non_finite(value), default=str, allow_nan=False)


def _structured_payload(value: Any) -> Any:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, BaseModel):
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return _json_safe(model_dump())
        dict_fn = getattr(value, "dict", None)
        if callable(dict_fn):
            return _json_safe(dict_fn())
    if isinstance(value, dict):
        return _json_safe(value)
    if isinstance(value, (list, tuple, set)):
        return _json_safe(list(value))
    return _json_safe(value)


def _config_payload(component: Any) -> Dict[str, Any]:
    config = _safe_getattr(component, "config", None)
    if config is None:
        return {}
    if is_dataclass(config):
        return _json_safe(asdict(config))
    if isinstance(config, BaseModel):
        model_dump = getattr(config, "model_dump", None)
        if callable(model_dump):
            return _json_safe(model_dump())
        dict_fn = getattr(config, "dict", None)
        if callable(dict_fn):
            return _json_safe(dict_fn())
    raw = getattr(config, "__dict__", None)
    return _json_safe(raw if isinstance(raw, dict) else str(config))


def _component_descriptor(component: Any) -> Dict[str, Any]:
    return {
        "available": component is not None,
        "class_name": component.__class__.__name__ if component is not None else None,
        "config": _config_payload(component) if component is not None else {},
    }


def _invoke_component_method(component: Any, method_name: str, default: Any) -> Any:
    if component is None:
        return default
    method = _safe_getattr(component, method_name, None)
    if not callable(method):
        return default
    try:
        return _structured_payload(method())
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "component": component.__class__.__name__,
            "method": method_name,
            "last_error": str(exc),
        }


def _summarize_table_like(table: Any) -> Dict[str, Any]:
    if table is None:
        return {"rows": 0, "columns": [], "last_index": None}

    try:
        columns = [str(column) for column in list(_safe_getattr(table, "columns", []))]
    except Exception:
        columns = []

    rows = 0
    last_index = None
    is_empty = bool(_safe_getattr(table, "empty", True))
    if not is_empty:
        try:
            rows = int(len(table))
        except Exception:
            rows = 0
        index = _safe_getattr(table, "index", None)
        try:
            if index is not None and len(index) > 0:
                last_index = str(index[-1])
        except Exception:
            last_index = None

    return {
        "rows": rows,
        "columns": columns,
        "last_index": last_index,
    }


def _table_records(table: Any, limit: int = 5) -> List[Dict[str, Any]]:
    if table is None or bool(_safe_getattr(table, "empty", True)):
        return []
    try:
        head = _safe_getattr(table, "head", None)
        limited = head(limit) if callable(head) else table
        to_dict = _safe_getattr(limited, "to_dict", None)
        if callable(to_dict):
            return _structured_payload(to_dict(orient="records"))
    except Exception:
        return []
    return []


def _summarize_feature_views(feature_views: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for key, table in (feature_views or {}).items():
        summary[key] = _summarize_table_like(table)
    return summary


def _summarize_runtime_state_map(runtime_state_map: Dict[str, Any], *, limit: int = 20) -> Dict[str, Any]:
    items = list((runtime_state_map or {}).items())
    keys_summary: Dict[str, Any] = {}
    for candidate_id, state in items[:limit]:
        if isinstance(state, dict):
            keys_summary[candidate_id] = {
                "keys": sorted(str(key) for key in state.keys()),
            }
        else:
            keys_summary[candidate_id] = {
                "type": type(state).__name__,
            }
    return {
        "count": len(items),
        "sample": keys_summary,
    }


def _channel_manager_map() -> dict[str, ConnectionManager]:
    return {
        "logs": log_manager,
        "status": status_manager,
        "dashboard": dashboard_manager,
        "risk": risk_manager_ws,
        "evolution": evolution_manager_ws,
        "data-health": data_health_manager_ws,
        "execution": execution_manager_ws,
        "diagnostics": diagnostics_manager_ws,
    }


def _record_channel_broadcast(channel_key: str, error: Optional[str] = None) -> None:
    state = _CHANNEL_REGISTRY.get(channel_key)
    if state is None:
        return
    state["last_broadcast_at"] = _iso_now()
    if error is None:
        state["broadcast_count"] = int(state.get("broadcast_count", 0)) + 1
        state["last_error"] = None
    else:
        state["last_error"] = error[:500]


def _record_worker_tick(
    worker_key: str,
    *,
    error: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    state = _WORKER_REGISTRY.get(worker_key)
    if state is None:
        return
    state["last_tick_at"] = _iso_now()
    if error is None:
        state["success_count"] = int(state.get("success_count", 0)) + 1
        state["last_error"] = None
    else:
        state["last_error"] = error[:500]
    if extra:
        state.update(extra)


async def _broadcast_snapshot(
    manager: ConnectionManager,
    channel_key: str,
    payload_builder: Callable[[], Dict[str, Any]],
) -> None:
    if manager.connection_count() == 0:
        return
    try:
        await manager.broadcast(_json_dumps_safe(payload_builder()))
        _record_channel_broadcast(channel_key)
    except Exception as exc:  # noqa: BLE001
        _record_channel_broadcast(channel_key, str(exc))


def _workspace_health_summary(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    generated_at = payload.get("generated_at", _iso_now())
    if name == "overview":
        feed_health = _safe_getattr(payload.get("feed_health", {}), "get", lambda *_a, **_k: "unknown")("health", "unknown")
        return {
            "status": feed_health if payload.get("status") == "running" else payload.get("status", "unknown"),
            "generated_at": generated_at,
            "alert_count": len(payload.get("alerts", []) or []),
            "detail": f"feed={feed_health} alerts={len(payload.get('alerts', []) or [])}",
        }
    if name == "alpha_brain":
        dominant_regime = payload.get("dominant_regime", "unknown")
        is_stable = bool(payload.get("is_regime_stable", False))
        return {
            "status": "healthy" if dominant_regime != "unknown" and is_stable else ("partial" if dominant_regime != "unknown" else "warning"),
            "generated_at": generated_at,
            "alert_count": len(payload.get("orchestrator", {}).get("block_reasons", []) or []),
            "detail": f"regime={dominant_regime} stable={is_stable}",
        }
    if name == "evolution":
        return {
            "status": payload.get("status", "healthy") or "healthy",
            "generated_at": generated_at,
            "alert_count": len(payload.get("latest_rollbacks", []) or []),
            "detail": f"active={len(payload.get('active_candidates', []) or [])} rollbacks={len(payload.get('latest_rollbacks', []) or [])}",
        }
    if name == "risk_matrix":
        circuit_broken = bool(payload.get("circuit_broken", False))
        return {
            "status": "critical" if circuit_broken else "healthy",
            "generated_at": generated_at,
            "alert_count": 1 if payload.get("circuit_reason") else 0,
            "detail": f"circuit={circuit_broken} cooldown={payload.get('circuit_cooldown_remaining_sec', 0)}s",
        }
    if name == "data_fusion":
        freshness_status = payload.get("freshness_summary", {}).get("status", payload.get("status", "unknown"))
        return {
            "status": freshness_status,
            "generated_at": generated_at,
            "alert_count": len(payload.get("stale_fields", []) or []),
            "detail": f"stale_fields={len(payload.get('stale_fields', []) or [])}",
        }
    if name == "execution":
        return {
            "status": payload.get("status", "healthy") or "healthy",
            "generated_at": generated_at,
            "alert_count": 0,
            "detail": f"open_orders={len(payload.get('open_orders', []) or [])} fills={len(payload.get('recent_fills', []) or [])}",
        }
    return {
        "status": payload.get("status", "unknown"),
        "generated_at": generated_at,
        "alert_count": 0,
        "detail": "",
    }


def _collect_recent_errors(
    overview: Dict[str, Any],
    data_fusion: Dict[str, Any],
    latest_order_rejection: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    recent_errors: List[Dict[str, Any]] = []
    for channel_key, state in _CHANNEL_REGISTRY.items():
        if state.get("last_error"):
            recent_errors.append({
                "source": f"transport:{channel_key}",
                "severity": "warning",
                "message": state.get("last_error"),
                "occurred_at": state.get("last_broadcast_at") or _iso_now(),
            })
    for worker_key, state in _WORKER_REGISTRY.items():
        if state.get("last_error"):
            recent_errors.append({
                "source": f"worker:{worker_key}",
                "severity": "warning",
                "message": state.get("last_error"),
                "occurred_at": state.get("last_tick_at") or _iso_now(),
            })
    if isinstance(latest_order_rejection, dict) and latest_order_rejection:
        recent_errors.append({
            "source": "execution:order_rejection",
            "severity": "warning",
            "message": latest_order_rejection.get("reason", "unknown"),
            "occurred_at": latest_order_rejection.get("timestamp", _iso_now()),
            "details": latest_order_rejection,
        })
    for field_name in ("onchain_health", "sentiment_health"):
        payload = data_fusion.get(field_name, {}) or {}
        degrade_reason = payload.get("degrade_reason") or payload.get("reason")
        if degrade_reason:
            recent_errors.append({
                "source": f"data:{field_name}",
                "severity": "warning",
                "message": degrade_reason,
                "occurred_at": data_fusion.get("generated_at", _iso_now()),
            })
    for alert in overview.get("alerts", []) or []:
        if isinstance(alert, dict) and alert.get("severity") in {"warning", "critical"}:
            recent_errors.append({
                "source": f"alert:{alert.get('source', 'unknown')}",
                "severity": alert.get("severity", "warning"),
                "message": alert.get("message", "unknown"),
                "occurred_at": alert.get("occurred_at", _iso_now()),
                "details": alert.get("details", {}),
            })
    return recent_errors[-20:]


def _summarize_positions(trader: Any) -> Dict[str, Any]:
    positions = {sym: float(qty) for sym, qty in _safe_getattr(trader, "_positions", {}).items() if qty > 0}
    latest_prices = {sym: float(price) for sym, price in _safe_getattr(trader, "_latest_prices", {}).items()}
    entry_prices = {sym: float(price) for sym, price in _safe_getattr(trader, "_entry_prices", {}).items()}
    total_notional = 0.0
    enriched = []
    for sym, qty in positions.items():
        px = latest_prices.get(sym, 0.0)
        entry_price = entry_prices.get(sym, 0.0)
        notional = qty * px
        unrealized_pnl = (px - entry_price) * qty if entry_price > 0 and px > 0 else None
        total_notional += notional
        enriched.append({
            "symbol": sym,
            "quantity": qty,
            "last_price": px,
            "notional": round(notional, 4),
            "entry_price": round(entry_price, 4) if entry_price > 0 else None,
            "unrealized_pnl": round(unrealized_pnl, 4) if unrealized_pnl is not None else None,
        })
    return {
        "count": len(enriched),
        "total_notional": round(total_notional, 4),
        "items": enriched,
    }


def _build_overview_snapshot() -> Dict[str, Any]:
    trader = _global_trader_instance
    if not trader:
        return {
            "generated_at": _iso_now(),
            "status": "inactive",
            "message": "Trader engine is not running",
        }

    risk_summary = trader.risk_manager.get_state_summary()
    equity = float(_safe_getattr(trader, "_current_equity", 0.0))
    peak_equity = float(risk_summary.get("peak_equity", equity or 0.0))
    drawdown_pct = 0.0
    if peak_equity > 0 and equity >= 0:
        drawdown_pct = max(0.0, (peak_equity - equity) / peak_equity)

    regime = _safe_getattr(trader, "_latest_regime_state", None) or _safe_getattr(trader, "_current_regime", None)
    orchestrator_decision = _safe_getattr(trader, "_latest_orchestration_decision", None)
    subscription_manager = (
        _safe_getattr(trader, "_phase3_subscription_manager", None)
        or _safe_getattr(trader, "_subscription_manager", None)
    )
    feed_health = _safe_getattr(subscription_manager, "diagnostics", None)
    if callable(feed_health):
        try:
            feed_health = feed_health()
        except Exception:
            feed_health = {}
    else:
        feed_health = {}

    circuit_broken = trader.risk_manager.is_circuit_broken()
    risk_level = "critical" if circuit_broken else ("elevated" if drawdown_pct >= 0.1 else "normal")
    latest_rejection = _safe_getattr(trader, "_latest_order_rejection", None)
    generated_at = _iso_now()

    alerts: List[Dict[str, Any]] = []

    if circuit_broken:
        alerts.append(
            {
                "code": "circuit_broken",
                "severity": "critical",
                "source": "risk",
                "message": risk_summary.get("circuit_reason", "Circuit breaker active"),
                "occurred_at": generated_at,
                "details": {
                    "drawdown_pct": round(drawdown_pct, 6),
                    "risk_level": risk_level,
                },
            }
        )

    feed_state = str(feed_health.get("health", "unknown"))
    if feed_state in {"degraded", "stopped", "unknown"}:
        severity = "critical" if feed_state == "stopped" else ("warning" if feed_state == "degraded" else "info")
        alerts.append(
            {
                "code": f"feed_{feed_state}",
                "severity": severity,
                "source": "data-feed",
                "message": f"数据源状态异常: {feed_state}",
                "occurred_at": generated_at,
                "details": {
                    "exchange": feed_health.get("exchange"),
                    "reconnect_count": feed_health.get("reconnect_count", 0),
                },
            }
        )

    regime_name = _safe_getattr(regime, "dominant_regime", "unknown")
    regime_confidence = float(_safe_getattr(regime, "confidence", 0.0) or 0.0)
    if regime_name == "unknown":
        alerts.append(
            {
                "code": "regime_unknown",
                "severity": "warning",
                "source": "alpha-brain",
                "message": "当前市场状态为 unknown，编排可能降级或阻断",
                "occurred_at": generated_at,
                "details": {
                    "confidence": regime_confidence,
                },
            }
        )
    elif regime_confidence < 0.2:
        alerts.append(
            {
                "code": "regime_low_confidence",
                "severity": "info",
                "source": "alpha-brain",
                "message": "市场状态置信度较低，仓位可能被压缩",
                "occurred_at": generated_at,
                "details": {
                    "dominant_regime": regime_name,
                    "confidence": regime_confidence,
                },
            }
        )

    if not bool(_safe_getattr(trader, "_latest_regime_stable", False)):
        alerts.append(
            {
                "code": "regime_unstable",
                "severity": "info",
                "source": "alpha-brain",
                "message": "市场状态最近不稳定，编排可能进入 REDUCE",
                "occurred_at": generated_at,
                "details": {
                    "dominant_regime": regime_name,
                    "confidence": regime_confidence,
                },
            }
        )

    if isinstance(latest_rejection, dict) and latest_rejection:
        alerts.append(
            {
                "code": "latest_order_rejection",
                "severity": "warning",
                "source": "execution",
                "message": f"最近一次拒单: {latest_rejection.get('reason', 'unknown')}",
                "occurred_at": latest_rejection.get("timestamp", generated_at),
                "details": {
                    "stage": latest_rejection.get("stage", "unknown"),
                    "strategy_id": latest_rejection.get("strategy_id", "unknown"),
                    "symbol": latest_rejection.get("symbol", "unknown"),
                    "side": latest_rejection.get("side", "unknown"),
                    "quantity": latest_rejection.get("quantity", "0"),
                },
            }
        )

    snapshot = {
        "generated_at": generated_at,
        "status": "running" if _safe_getattr(trader, "_running", False) else "stopped",
        "mode": _safe_getattr(trader, "mode", "unknown"),
        "exchange": _safe_getattr(_safe_getattr(trader, "gateway", None), "exchange_id", "unknown"),
        "equity": equity,
        "daily_pnl": float(risk_summary.get("daily_pnl", 0.0)),
        "peak_equity": peak_equity,
        "drawdown_pct": round(drawdown_pct, 6),
        "positions_summary": _summarize_positions(trader),
        "dominant_regime": _safe_getattr(regime, "dominant_regime", "unknown"),
        "regime_confidence": float(_safe_getattr(regime, "confidence", 0.0) or 0.0),
        "is_regime_stable": bool(_safe_getattr(trader, "_latest_regime_stable", False)),
        "risk_level": risk_level,
        "feed_health": {
            "health": feed_health.get("health", "unknown"),
            "exchange": feed_health.get("exchange", _safe_getattr(_safe_getattr(trader, "gateway", None), "exchange_id", "unknown")),
            "reconnect_count": feed_health.get("reconnect_count", 0),
        },
        "strategy_weight_summary": _safe_getattr(orchestrator_decision, "weights", {}) or {},
        "alerts": alerts,
        "latest_order_rejection": latest_rejection if isinstance(latest_rejection, dict) and latest_rejection else None,
    }
    log.debug("[APIv2] Overview snapshot built: status={} mode={} regime={} risk={}", snapshot["status"], snapshot["mode"], snapshot["dominant_regime"], snapshot["risk_level"])
    return snapshot


def _build_alpha_brain_snapshot() -> Dict[str, Any]:
    trader = _global_trader_instance
    if not trader:
        return {"generated_at": _iso_now(), "status": "inactive"}

    regime = _safe_getattr(trader, "_latest_regime_state", None) or _safe_getattr(trader, "_current_regime", None)
    orchestrator_decision = _safe_getattr(trader, "_latest_orchestration_decision", None)
    continuous_learners = _safe_getattr(trader, "_continuous_learners", {}) or {}
    latest_decision_chain = _safe_getattr(trader, "_latest_decision_chain", "unknown")

    learner_items = []
    active_learner_summary = None
    for key, learner in continuous_learners.items():
        try:
            version_info = learner.get_model_version_info()
            thresholds = learner.get_optimal_thresholds()
            active_version = next((item for item in version_info if item.get("is_active")), version_info[-1] if version_info else None)
            runtime_artifacts_loader = _safe_getattr(trader, "_load_strategy_ml_runtime_artifacts", None)
            runtime_artifacts = runtime_artifacts_loader(key) if callable(runtime_artifacts_loader) else {}
            strategy_finder = _safe_getattr(trader, "_find_strategy_by_id", None)
            strategy = strategy_finder(key) if callable(strategy_finder) else None
            learner_summary = {
                "id": key,
                "active_version": active_version.get("version_id") if active_version else None,
                "last_retrain_at": active_version.get("trained_at") if active_version else None,
                "model_type": (
                    runtime_artifacts.get("trainer_model_type")
                    or _safe_getattr(_safe_getattr(strategy, "model", None), "model_type", None)
                    or "unknown"
                ),
                "model_path": active_version.get("model_path") if active_version else None,
                "threshold_source": runtime_artifacts.get("threshold_source"),
                "thresholds": {
                    "buy": float(thresholds[0]),
                    "sell": float(thresholds[1]),
                },
                "versions": [item.get("version_id") for item in version_info[-5:] if item.get("version_id")],
            }
            learner_items.append(learner_summary)
            if active_learner_summary is None:
                active_learner_summary = learner_summary
        except Exception as exc:
            learner_items.append({"id": key, "error": str(exc)})

    snapshot = {
        "generated_at": _iso_now(),
        "dominant_regime": _safe_getattr(regime, "dominant_regime", "unknown"),
        "confidence": float(_safe_getattr(regime, "confidence", 0.0) or 0.0),
        "regime_probs": {
            "bull": float(_safe_getattr(regime, "bull_prob", 0.0) or 0.0),
            "bear": float(_safe_getattr(regime, "bear_prob", 0.0) or 0.0),
            "sideways": float(_safe_getattr(regime, "sideways_prob", 0.0) or 0.0),
            "high_vol": float(_safe_getattr(regime, "high_vol_prob", 0.0) or 0.0),
        },
        "is_regime_stable": bool(_safe_getattr(trader, "_latest_regime_stable", False)),
        "orchestrator": {
            "decision_chain": latest_decision_chain,
            "gating_action": _safe_getattr(_safe_getattr(orchestrator_decision, "gating", None), "action", None).value if _safe_getattr(_safe_getattr(orchestrator_decision, "gating", None), "action", None) else "unknown",
            "weights": _safe_getattr(orchestrator_decision, "weights", {}) or {},
                "weight_basis": "regime_affinity",
            "block_reasons": _safe_getattr(orchestrator_decision, "block_reasons", []) or [],
            "selected_results": [
                {
                    "strategy_id": _safe_getattr(item, "strategy_id", "unknown"),
                    "symbol": _safe_getattr(item, "symbol", "unknown"),
                    "action": _safe_getattr(item, "action", "HOLD"),
                    "confidence": float(_safe_getattr(item, "confidence", 0.0) or 0.0),
                }
                for item in (_safe_getattr(orchestrator_decision, "selected_results", []) or [])
            ],
        },
        "continuous_learner": {
            "count": len(learner_items),
            "active_version": _safe_getattr(active_learner_summary, "get", lambda *_: None)("active_version") if active_learner_summary else None,
            "model_type": _safe_getattr(active_learner_summary, "get", lambda *_: None)("model_type") if active_learner_summary else None,
            "model_path": _safe_getattr(active_learner_summary, "get", lambda *_: None)("model_path") if active_learner_summary else None,
            "threshold_source": _safe_getattr(active_learner_summary, "get", lambda *_: None)("threshold_source") if active_learner_summary else None,
            "thresholds": _safe_getattr(active_learner_summary, "get", lambda *_: {})("thresholds") if active_learner_summary else {},
            "last_retrain_at": _safe_getattr(active_learner_summary, "get", lambda *_: None)("last_retrain_at") if active_learner_summary else None,
            "items": learner_items,
        },
        "ai_analysis": _safe_getattr(trader, "_last_ai_analysis", "N/A"),
    }
    log.debug("[APIv2] Alpha brain snapshot built: regime={} learners={} block_reasons={}", snapshot["dominant_regime"], snapshot["continuous_learner"]["count"], len(snapshot["orchestrator"]["block_reasons"]))
    return snapshot


def _candidate_to_summary(candidate: Any) -> Dict[str, Any]:
    metadata = _safe_getattr(candidate, "metadata", {}) or {}
    return {
        "candidate_id": _safe_getattr(candidate, "candidate_id", "unknown"),
        "owner": _safe_getattr(candidate, "owner", "unknown"),
        "family_key": _safe_getattr(metadata, "get", lambda *_: None)("family_key") or _safe_getattr(candidate, "owner", "unknown"),
        "strategy_id": _safe_getattr(metadata, "get", lambda *_: None)("strategy_id"),
        "version": _safe_getattr(candidate, "version", "unknown"),
        "status": _safe_getattr(_safe_getattr(candidate, "status", None), "value", _safe_getattr(candidate, "status", "unknown")),
        "candidate_type": _safe_getattr(_safe_getattr(candidate, "candidate_type", None), "value", _safe_getattr(candidate, "candidate_type", "unknown")),
        "sharpe_30d": _safe_getattr(candidate, "sharpe_30d", None),
        "max_drawdown_30d": _safe_getattr(candidate, "max_drawdown_30d", None),
        "win_rate_30d": _safe_getattr(candidate, "win_rate_30d", None),
        "ab_lift": _safe_getattr(candidate, "ab_lift", None),
    }


def _get_evolution_engine(trader: Any) -> Any:
    return (
        _safe_getattr(trader, "_phase3_evolution", None)
        or _safe_getattr(trader, "_self_evolution_engine", None)
        or _safe_getattr(trader, "self_evolution_engine", None)
    )


def _build_evolution_snapshot() -> Dict[str, Any]:
    trader = _global_trader_instance
    if not trader:
        return {"generated_at": _iso_now(), "status": "inactive"}

    evolution = _get_evolution_engine(trader)
    if evolution is None:
        return {
            "generated_at": _iso_now(),
            "status": "unavailable",
            "message": "SelfEvolutionEngine not attached to trader",
        }

    registry = _safe_getattr(evolution, "_registry", None)
    state_store = _safe_getattr(evolution, "_state_store", None)
    candidates = []
    if registry is not None:
        for attr in ("list_all", "all", "get_all"):
            func = _safe_getattr(registry, attr, None)
            if callable(func):
                try:
                    result = func()
                    if isinstance(result, list):
                        candidates = result
                        break
                except Exception:
                    continue
        if not candidates:
            for attr in ("_candidates", "candidates"):
                raw = _safe_getattr(registry, attr, None)
                if isinstance(raw, dict):
                    candidates = list(raw.values())
                    break

    counts: Dict[str, int] = {}
    candidate_summaries = [_candidate_to_summary(c) for c in candidates]
    for item in candidate_summaries:
        counts[item["status"]] = counts.get(item["status"], 0) + 1

    decisions = []
    retirements = []
    weekly_runs = []
    if state_store is not None:
        try:
            decisions = state_store.load_decisions(limit=10)
        except Exception:
            decisions = []
        try:
            retirements = state_store.load_retirements(limit=10)
        except Exception:
            retirements = []
        try:
            weekly_runs = state_store.load_weekly_params_optimizer_runs(limit=10)
        except Exception:
            weekly_runs = []

    ab_manager = _safe_getattr(evolution, "_ab_manager", None)
    active_experiment_items = []
    completed_experiment_items = []
    if ab_manager is not None:
        try:
            for experiment_id in _safe_getattr(ab_manager, "list_active_experiments", lambda: [])() or []:
                status = _safe_getattr(ab_manager, "get_experiment_status", lambda *_: None)(experiment_id)
                if isinstance(status, dict):
                    status["status"] = "active"
                    active_experiment_items.append(status)
        except Exception:
            active_experiment_items = []
        try:
            completed_results = _safe_getattr(ab_manager, "completed_results", lambda: [])() or []
            for item in completed_results[-10:]:
                if is_dataclass(item):
                    completed_experiment_items.append(asdict(item))
                elif isinstance(item, dict):
                    completed_experiment_items.append(item)
        except Exception:
            completed_experiment_items = []

    weekly_state_loader = _safe_getattr(state_store, "load_weekly_params_optimizer_state", None)
    weekly_state = weekly_state_loader() if callable(weekly_state_loader) else {}
    if not isinstance(weekly_state, dict) or not weekly_state:
        weekly_state = {
            "status": "idle",
            "reason": "not_triggered_yet",
        }

    optimization_targets = []
    target_loader = _safe_getattr(trader, "_collect_phase3_param_optimization_targets", None)
    if callable(target_loader):
        try:
            optimization_targets = target_loader() or []
        except Exception:
            optimization_targets = []

    snapshot = {
        "generated_at": _iso_now(),
        "candidate_counts_by_status": counts,
        "active_candidates": [c for c in candidate_summaries if c["status"] == "active"],
        "candidates": candidate_summaries,
        "latest_promotions": decisions,
        "latest_retirements": retirements,
        "latest_rollbacks": [
            d for d in decisions
            if d.get("action") == "ROLLBACK" or (d.get("metadata") or {}).get("rollback_to")
        ],
        "ab_experiments": {
            "summary": _safe_getattr(ab_manager, "diagnostics", lambda: {})(),
            "active": active_experiment_items,
            "completed": completed_experiment_items,
        },
        "weekly_params_optimizer": {
            "cron": _safe_getattr(_safe_getattr(evolution, "config", None), "weekly_params_optimizer_cron", ""),
            "is_running": bool(_safe_getattr(trader, "_phase3_params_optimizer_running", False)),
            "target_count": len(optimization_targets),
            "targets": optimization_targets,
            "runs": weekly_runs,
            "state": weekly_state,
        },
        "last_report_meta": _safe_getattr(_safe_getattr(evolution, "_report_builder", None), "__class__", type("X", (), {})).__name__,
    }
    log.debug("[APIv2] Evolution snapshot built: candidates={} active={} decisions={} retirements={}", len(candidate_summaries), len(snapshot["active_candidates"]), len(decisions), len(retirements))
    return snapshot


def _build_risk_matrix_snapshot() -> Dict[str, Any]:
    trader = _global_trader_instance
    if not trader:
        return {"generated_at": _iso_now(), "status": "inactive"}

    risk_summary = trader.risk_manager.get_state_summary()
    manager_state = _safe_getattr(trader, "risk_manager", None)
    circuit_broken_at = _safe_getattr(_safe_getattr(manager_state, "_state", None), "circuit_broken_at", None)
    cooldown_minutes = _safe_getattr(_safe_getattr(manager_state, "config", None), "circuit_breaker_cooldown_minutes", 0)
    cooldown_remaining_sec = 0
    if circuit_broken_at is not None and cooldown_minutes:
        elapsed = (datetime.now(tz=timezone.utc) - circuit_broken_at).total_seconds()
        cooldown_remaining_sec = max(0, int(cooldown_minutes * 60 - elapsed))

    budget_checker = _safe_getattr(trader, "_budget_checker", None)
    kill_switch = _safe_getattr(trader, "_kill_switch", None)
    cooldown_manager = _safe_getattr(trader, "_cooldown_manager", None)
    adaptive_risk = _safe_getattr(trader, "_adaptive_risk", None)
    dca_engine = _safe_getattr(trader, "_dca_engine", None)
    exit_planner = _safe_getattr(trader, "_exit_planner", None)
    position_sizer = _safe_getattr(trader, "position_sizer", None)

    budget_remaining_pct = _safe_getattr(budget_checker, "remaining_budget_pct", None)
    if budget_remaining_pct is None and budget_checker is not None:
        budget_remaining_pct = _safe_getattr(
            _safe_getattr(budget_checker, "snapshot", lambda: {})(),
            "get",
            lambda *_args, **_kwargs: None,
        )("remaining_budget_pct", None)

    kill_switch_payload = _safe_getattr(
        kill_switch,
        "health_snapshot",
        lambda: {"status": "unavailable"},
    )()

    cooldown_payload = _safe_getattr(
        cooldown_manager,
        "diagnostics",
        lambda: {"status": "unavailable"},
    )()
    if cooldown_payload.get("status") == "unavailable" and adaptive_risk is not None:
        adaptive_diag = _safe_getattr(adaptive_risk, "health_snapshot", lambda: {})()
        cooldown_payload = _safe_getattr(
            adaptive_diag,
            "get",
            lambda *_args, **_kwargs: {"status": "unavailable"},
        )("cooldown", {"status": "unavailable"})

    if dca_engine is None and adaptive_risk is not None:
        dca_engine = _safe_getattr(adaptive_risk, "_dca_engine", None)
    if exit_planner is None and adaptive_risk is not None:
        exit_planner = _safe_getattr(adaptive_risk, "_exit_planner", None)

    position_sizing_mode = _safe_getattr(_safe_getattr(position_sizer, "config", None), "method", None)
    if not position_sizing_mode:
        # 当前实现采用 PositionSizer + 动态波动率目标法，未暴露独立 config.method。
        position_sizing_mode = "dynamic"

    snapshot = {
        "generated_at": _iso_now(),
        "circuit_broken": trader.risk_manager.is_circuit_broken(),
        "circuit_reason": risk_summary.get("circuit_reason", ""),
        "circuit_cooldown_remaining_sec": cooldown_remaining_sec,
        "daily_pnl": float(risk_summary.get("daily_pnl", 0.0)),
        "consecutive_losses": int(risk_summary.get("consecutive_losses", 0)),
        "peak_equity": float(risk_summary.get("peak_equity", 0.0)),
        "budget_remaining_pct": budget_remaining_pct,
        "kill_switch": kill_switch_payload,
        "cooldown": cooldown_payload,
        "dca_plan": {
            "config": _safe_getattr(_safe_getattr(dca_engine, "config", None), "__dict__", {}) if dca_engine else {},
        },
        "exit_plan": {
            "config": _safe_getattr(_safe_getattr(exit_planner, "config", None), "__dict__", {}) if exit_planner else {},
        },
        "position_sizing_mode": position_sizing_mode,
        "risk_state": risk_summary,
    }
    log.debug("[APIv2] Risk matrix snapshot built: circuit={} cooldown_remaining={} daily_pnl={}", snapshot["circuit_broken"], snapshot["circuit_cooldown_remaining_sec"], snapshot["daily_pnl"])
    return snapshot


def _build_data_fusion_snapshot() -> Dict[str, Any]:
    trader = _global_trader_instance
    if not trader:
        return {"generated_at": _iso_now(), "status": "inactive"}

    subscription_manager = (
        _safe_getattr(trader, "_phase3_subscription_manager", None)
        or _safe_getattr(trader, "_subscription_manager", None)
    )
    sub_diag = _safe_getattr(subscription_manager, "diagnostics", lambda: {"health": "unavailable"})()
    
    # 构建最新价格结构，包含 price, updated_at, age_sec
    latest_prices_raw = _safe_getattr(trader, "_latest_prices", {})
    latest_prices_ts = _safe_getattr(trader, "_latest_prices_updated_at", {})
    current_time = time.time()
    
    latest_prices = {}
    for sym, price in latest_prices_raw.items():
        ts = latest_prices_ts.get(sym, current_time)
        age_sec = current_time - ts
        updated_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        if not updated_at.endswith('Z'):
            updated_at = updated_at.replace('+00:00', 'Z')
        latest_prices[sym] = {
            "price": float(price),
            "updated_at": updated_at,
            "age_sec": round(age_sec, 2)
        }

    def _normalize_status(raw: Any) -> str:
        return str(raw or "unknown").strip().lower()

    def _status_from_depth_registry() -> Dict[str, Any]:
        registry = _safe_getattr(trader, "_phase3_depth_registry", None)
        if registry is None:
            return {"status": "unknown", "reason": "depth_registry_unavailable"}

        diagnostics = _safe_getattr(registry, "diagnostics", lambda: {})()
        if not isinstance(diagnostics, dict) or not diagnostics:
            return {"status": "unknown", "reason": "no_orderbook_symbol_data"}

        total = len(diagnostics)
        healthy = 0
        stale = 0
        for item in diagnostics.values():
            gap_ok = _normalize_status(_safe_getattr(item, "get", lambda *_a, **_k: "")("gap_status", "")) in {"ok", "healthy"}
            has_snapshot = bool(_safe_getattr(item, "get", lambda *_a, **_k: False)("has_snapshot", False))
            if gap_ok and has_snapshot:
                healthy += 1
            else:
                stale += 1

        status = "healthy" if healthy == total else ("partial" if healthy > 0 else "degraded")
        return {
            "status": status,
            "active_symbols": total,
            "healthy_symbols": healthy,
            "stale_symbols": stale,
            "details": diagnostics,
        }

    def _status_from_trade_registry() -> Dict[str, Any]:
        registry = _safe_getattr(trader, "_phase3_trade_registry", None)
        if registry is None:
            return {"status": "unknown", "reason": "trade_registry_unavailable"}

        diagnostics = _safe_getattr(registry, "diagnostics", lambda: {})()
        if not isinstance(diagnostics, dict) or not diagnostics:
            return {"status": "unknown", "reason": "no_trade_symbol_data"}

        total = len(diagnostics)
        active = 0
        inactive = 0
        for item in diagnostics.values():
            trade_count = int(_safe_getattr(item, "get", lambda *_a, **_k: 0)("trade_count", 0) or 0)
            if trade_count > 0:
                active += 1
            else:
                inactive += 1

        status = "healthy" if active == total else ("partial" if active > 0 else "degraded")
        return {
            "status": status,
            "active_symbols": total,
            "symbols_with_trades": active,
            "symbols_without_trades": inactive,
            "details": diagnostics,
        }

    def _status_from_external_collector(collector_attr: str, source_name: str) -> Dict[str, Any]:
        collector = _safe_getattr(trader, collector_attr, None)
        if collector is None:
            return {"status": "unknown", "reason": f"{source_name}_collector_unavailable"}

        diagnostics = _safe_getattr(collector, "diagnostics", lambda: {})()
        if not isinstance(diagnostics, dict):
            diagnostics = {}

        symbols = _safe_getattr(_safe_getattr(trader, "sys_config", None), "data", None)
        default_symbols = _safe_getattr(symbols, "default_symbols", []) or []
        probe_symbol = "BTC/USDT"
        if isinstance(default_symbols, list) and default_symbols:
            probe_symbol = str(default_symbols[0])

        cache = _safe_getattr(collector, "cache", None)
        eval_freshness = _safe_getattr(cache, "evaluate_freshness", None)
        if callable(eval_freshness):
            try:
                freshness = eval_freshness(
                    probe_symbol,
                    _safe_getattr(_safe_getattr(collector, "config", None), "freshness_config", None),
                )
                freshness_status = _normalize_status(_safe_getattr(_safe_getattr(freshness, "status", None), "value", None))
                status = freshness_status if freshness_status else "unknown"
                return {
                    "status": status,
                    "probe_symbol": probe_symbol,
                    "lag_sec": float(_safe_getattr(freshness, "lag_sec", 0.0) or 0.0),
                    "ttl_sec": int(_safe_getattr(freshness, "ttl_sec", 0) or 0),
                    "degrade_reason": _safe_getattr(freshness, "degrade_reason", None),
                    "diagnostics": diagnostics,
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    "status": "unknown",
                    "probe_symbol": probe_symbol,
                    "reason": f"freshness_eval_failed:{exc}",
                    "diagnostics": diagnostics,
                }

        return {"status": "unknown", "reason": "collector_cache_unavailable", "diagnostics": diagnostics}

    orderbook_health = _status_from_depth_registry()
    trade_feed_health = _status_from_trade_registry()
    onchain_health = _status_from_external_collector("_onchain_collector", "onchain")
    sentiment_health = _status_from_external_collector("_sentiment_collector", "sentiment")

    price_feed_health = "healthy" if latest_prices else "degraded"
    stale_fields = [] if latest_prices else ["latest_prices"]

    for field_name, payload in {
        "subscription_manager": sub_diag,
        "orderbook_health": orderbook_health,
        "trade_feed_health": trade_feed_health,
        "onchain_health": onchain_health,
        "sentiment_health": sentiment_health,
    }.items():
        status = _normalize_status(_safe_getattr(payload, "get", lambda *_a, **_k: "unknown")("status", "unknown"))
        if status in {"stale", "degraded", "missing", "error", "failed", "fail"}:
            stale_fields.append(field_name)

    freshness_status = "fresh"
    if stale_fields:
        freshness_status = "stale"
    elif any(
        _normalize_status(_safe_getattr(payload, "get", lambda *_a, **_k: "")("status", "")) in {"partial", "unknown", "unavailable"}
        for payload in (orderbook_health, trade_feed_health, onchain_health, sentiment_health)
    ):
        freshness_status = "partial"

    snapshot = {
        "generated_at": _iso_now(),
        "price_feed_health": price_feed_health,
        "subscription_manager": sub_diag,
        "orderbook_health": orderbook_health,
        "trade_feed_health": trade_feed_health,
        "onchain_health": onchain_health,
        "sentiment_health": sentiment_health,
        "freshness_summary": {
            "status": freshness_status,
            "field_count": len(latest_prices),
            "source_count": 5,
        },
        "stale_fields": sorted(set(stale_fields)),
        "latest_prices": latest_prices,
    }
    log.debug("[APIv2] Data fusion snapshot built: price_feed={} stale_fields={} reconnect_count={}", snapshot["price_feed_health"], len(snapshot["stale_fields"]), sub_diag.get("reconnect_count", 0))
    return snapshot


def _build_execution_snapshot() -> Dict[str, Any]:
    trader = _global_trader_instance
    if not trader:
        return {"generated_at": _iso_now(), "status": "inactive"}

    orders = _safe_getattr(trader, "_orders", []) or []
    fills = _safe_getattr(trader, "_fills", []) or []
    positions = _summarize_positions(trader)
    snapshot = {
        "generated_at": _iso_now(),
        "open_orders": orders[-20:] if isinstance(orders, list) else [],
        "recent_fills": fills[-20:] if isinstance(fills, list) else [],
        "paper_summary": {
            "mode": _safe_getattr(trader, "mode", "unknown"),
            "slippage_bps": 10 if _safe_getattr(trader, "mode", "unknown") == "paper" else None,
            "fee_bps": 10 if _safe_getattr(trader, "mode", "unknown") == "paper" else None,
        },
        "positions": positions,
        "control_actions": [
            {"action": "stop", "enabled": True},
            {"action": "reset_circuit", "enabled": True},
            {"action": "trigger_circuit_test", "enabled": _safe_getattr(trader, "mode", "unknown") == "paper"},
        ],
    }
    log.debug("[APIv2] Execution snapshot built: open_orders={} fills={} positions={}", len(snapshot["open_orders"]), len(snapshot["recent_fills"]), positions["count"])
    return snapshot


def _build_diagnostics_snapshot() -> Dict[str, Any]:
    generated_at = _iso_now()
    trader = _global_trader_instance

    status_response = _build_status_response()
    overview = _build_overview_snapshot()
    alpha_brain = _build_alpha_brain_snapshot()
    evolution = _build_evolution_snapshot()
    risk_matrix = _build_risk_matrix_snapshot()
    data_fusion = _build_data_fusion_snapshot()
    execution = _build_execution_snapshot()

    alpha_runtime = _safe_getattr(trader, "_alpha_runtime", None)
    strategy_registry = _safe_getattr(trader, "_strategy_registry", None)
    orchestrator = _safe_getattr(trader, "_phase1_orchestrator", None)
    budget_checker = _safe_getattr(trader, "_budget_checker", None)
    kill_switch = _safe_getattr(trader, "_kill_switch", None)
    adaptive_risk = _safe_getattr(trader, "_adaptive_risk", None)
    phase2_state_store = _safe_getattr(trader, "_phase2_state_store", None)
    source_aligner = _safe_getattr(trader, "_phase2_source_aligner", None)
    evolution_engine = _get_evolution_engine(trader)
    subscription_manager = (
        _safe_getattr(trader, "_phase3_subscription_manager", None)
        or _safe_getattr(trader, "_subscription_manager", None)
    )
    ws_client = _safe_getattr(trader, "_phase3_ws_client", None)
    depth_registry = _safe_getattr(trader, "_phase3_depth_registry", None)
    trade_registry = _safe_getattr(trader, "_phase3_trade_registry", None)
    onchain_collector = _safe_getattr(trader, "_onchain_collector", None)
    onchain_feature_builder = _safe_getattr(trader, "_onchain_feature_builder", None)
    sentiment_collector = _safe_getattr(trader, "_sentiment_collector", None)
    sentiment_feature_builder = _safe_getattr(trader, "_sentiment_feature_builder", None)
    phase3_mm = _safe_getattr(trader, "_phase3_mm", None)
    phase3_ppo = _safe_getattr(trader, "_phase3_ppo", None)
    micro_builder = _safe_getattr(trader, "_phase3_micro_builder", None)
    action_adapter = _safe_getattr(trader, "_phase3_action_adapter", None)
    obs_builder = _safe_getattr(trader, "_phase3_obs_builder", None)
    attributor = _safe_getattr(trader, "attributor", None)

    regime_detector_diags = {}
    for symbol, detector in ((_safe_getattr(trader, "_phase1_regime_detectors", {}) or {}) if trader else {}).items():
        diag_fn = _safe_getattr(detector, "health_snapshot", None)
        regime_detector_diags[symbol] = _json_safe(diag_fn() if callable(diag_fn) else {})

    data_kitchen_diags = {}
    for symbol, kitchen in ((_safe_getattr(trader, "_phase1_data_kitchens", {}) or {}) if trader else {}).items():
        diag_fn = _safe_getattr(kitchen, "diagnostics", None)
        data_kitchen_diags[symbol] = _json_safe(diag_fn() if callable(diag_fn) else {})

    continuous_learner_diags = {}
    for key, learner in ((_safe_getattr(trader, "_continuous_learners", {}) or {}) if trader else {}).items():
        versions_fn = _safe_getattr(learner, "get_model_version_info", None)
        thresholds_fn = _safe_getattr(learner, "get_optimal_thresholds", None)
        thresholds = thresholds_fn() if callable(thresholds_fn) else ()
        continuous_learner_diags[key] = _json_safe({
            "versions": versions_fn() if callable(versions_fn) else [],
            "thresholds": {
                "buy": thresholds[0] if len(thresholds) > 0 else None,
                "sell": thresholds[1] if len(thresholds) > 1 else None,
            },
            "buffer_size": len(_safe_getattr(learner, "_ohlcv_buffer", []) or []),
            "bars_since_retrain": _safe_getattr(learner, "_bars_since_retrain", None),
        })

    feature_view_summaries = {}
    for symbol, feature_views in ((_safe_getattr(trader, "_phase1_feature_views", {}) or {}) if trader else {}).items():
        feature_view_summaries[symbol] = _summarize_feature_views(feature_views if isinstance(feature_views, dict) else {})

    symbol_regimes = {}
    for symbol, regime in ((_safe_getattr(trader, "_symbol_regimes", {}) or {}) if trader else {}).items():
        symbol_regimes[symbol] = _structured_payload(regime)

    symbol_risk_plans = {}
    for symbol, plan in ((_safe_getattr(trader, "_symbol_risk_plans", {}) or {}) if trader else {}).items():
        plan_payload = _structured_payload(plan)
        symbol_risk_plans[symbol] = plan_payload if isinstance(plan_payload, dict) else {"value": plan_payload}

    latest_prices_updated_at = _safe_getattr(trader, "_latest_prices_updated_at", {}) if trader else {}
    price_ages_sec = {
        symbol: round(time.time() - timestamp, 2)
        for symbol, timestamp in latest_prices_updated_at.items()
    }

    performance_summary = _invoke_component_method(attributor, "get_summary_metrics", {})
    strategy_attribution = []
    asset_attribution = []
    if attributor is not None:
        try:
            strategy_attribution = _table_records(attributor.get_strategy_attribution(), limit=10)
        except Exception:
            strategy_attribution = []
        try:
            asset_attribution = _table_records(attributor.get_asset_attribution(), limit=10)
        except Exception:
            asset_attribution = []

    phase3_strategy_candidates = _safe_getattr(trader, "_phase3_strategy_candidates", {}) if trader else {}
    phase3_strategy_candidate_bindings = _safe_getattr(trader, "_phase3_strategy_candidate_bindings", {}) if trader else {}
    phase3_strategy_metric_bindings = _safe_getattr(trader, "_phase3_strategy_metric_bindings", {}) if trader else {}
    phase3_candidate_experiments = _safe_getattr(trader, "_phase3_candidate_experiments", {}) if trader else {}
    phase3_candidate_runtime_state = _safe_getattr(trader, "_phase3_candidate_runtime_state", {}) if trader else {}
    phase3_mm_realized_trade_records = _safe_getattr(trader, "_phase3_mm_realized_trade_records", {}) if trader else {}
    phase3_mm_last_realized_pnl = _safe_getattr(trader, "_phase3_mm_last_realized_pnl", {}) if trader else {}
    phase3_mm_last_halt_reason = _safe_getattr(trader, "_phase3_mm_last_halt_reason", {}) if trader else {}

    evolution_registry = _safe_getattr(evolution_engine, "_registry", None)
    evolution_state_store = _safe_getattr(evolution_engine, "_state_store", None)
    evolution_scheduler = _safe_getattr(evolution_engine, "_scheduler", None)
    evolution_ab_manager = _safe_getattr(evolution_engine, "_ab_manager", None)

    transport_channels = {}
    for key, manager in _channel_manager_map().items():
        meta = _CHANNEL_REGISTRY.get(key, {})
        transport_channels[key] = {
            "label": meta.get("label", key),
            "path": meta.get("path", ""),
            "active_connections": manager.connection_count(),
            "last_broadcast_at": meta.get("last_broadcast_at"),
            "broadcast_count": meta.get("broadcast_count", 0),
            "last_error": meta.get("last_error"),
        }

    alerts = _json_safe(overview.get("alerts", []) or [])
    latest_order_rejection = overview.get("latest_order_rejection")
    recent_errors = _collect_recent_errors(overview, data_fusion, latest_order_rejection)

    overall_status = "healthy"
    if overview.get("status") == "inactive" or status_response.get("status") == "inactive":
        overall_status = "inactive"
    elif any(alert.get("severity") == "critical" for alert in (overview.get("alerts", []) or []) if isinstance(alert, dict)):
        overall_status = "critical"
    elif recent_errors:
        overall_status = "warning"

    snapshot = {
        "generated_at": generated_at,
        "status": overall_status,
        "system": {
            "api_version": app.version,
            "server_started_at": _SERVER_STARTED_AT.isoformat(),
            "uptime_sec": round((datetime.now(tz=timezone.utc) - _SERVER_STARTED_AT).total_seconds(), 2),
            "status_response": status_response,
            "trader_runtime": {
                "running": bool(_safe_getattr(trader, "_running", False)) if trader else False,
                "mode": _safe_getattr(trader, "mode", "unknown") if trader else "unknown",
                "symbols": list(_safe_getattr(_safe_getattr(_safe_getattr(trader, "sys_config", None), "data", None), "default_symbols", []) or []),
                "poll_interval_sec": _safe_getattr(trader, "_poll_interval_s", None) if trader else None,
                "markets_loaded": bool(_safe_getattr(trader, "_markets_loaded", False)) if trader else False,
                "preload_done": bool(_safe_getattr(trader, "_preload_done", False)) if trader else False,
                "phase2_external_enabled": bool(_safe_getattr(trader, "_phase2_external_enabled", False)) if trader else False,
                "phase3_enabled": bool(_safe_getattr(trader, "_phase3_enabled", False)) if trader else False,
                "phase3_realtime_enabled": bool(_safe_getattr(trader, "_phase3_realtime_enabled", False)) if trader else False,
            },
            "workers": _json_safe(_WORKER_REGISTRY),
            "queue_depths": {
                "log_queue": _safe_getattr(_safe_getattr(WebsocketLogSink, "_sync_queue", None), "qsize", lambda: 0)(),
            },
        },
        "transport": {
            "channels": transport_channels,
            "status_push_interval_sec": _WORKER_REGISTRY.get("status_push", {}).get("interval_sec", 3),
            "ticker_refresh_interval_sec": _WORKER_REGISTRY.get("ticker_refresh", {}).get("interval_sec", 5),
        },
        "workspace_health": {
            "overview": _workspace_health_summary("overview", overview),
            "alpha_brain": _workspace_health_summary("alpha_brain", alpha_brain),
            "evolution": _workspace_health_summary("evolution", evolution),
            "risk_matrix": _workspace_health_summary("risk_matrix", risk_matrix),
            "data_fusion": _workspace_health_summary("data_fusion", data_fusion),
            "execution": _workspace_health_summary("execution", execution),
        },
        "alpha_brain_diag": {
            "snapshot": alpha_brain,
            "runtime": {
                "available": alpha_runtime is not None,
                "loop_seq": _safe_getattr(alpha_runtime, "loop_seq", None),
                "debug_enabled": bool(_safe_getattr(alpha_runtime, "debug_enabled", False)) if alpha_runtime is not None else False,
                "context_builder": _component_descriptor(_safe_getattr(alpha_runtime, "context_builder", None)),
                "signal_pipeline": _component_descriptor(_safe_getattr(alpha_runtime, "signal_pipeline", None)),
                "trace_recorder": {
                    **_component_descriptor(_safe_getattr(alpha_runtime, "trace_recorder", None)),
                    "enabled": bool(_safe_getattr(_safe_getattr(alpha_runtime, "trace_recorder", None), "enabled", False)),
                },
                "registered_strategy_count": len(strategy_registry) if strategy_registry is not None else 0,
            },
            "strategy_registry": _json_safe(_safe_getattr(strategy_registry, "health_snapshot", lambda: {})()),
            "orchestrator": _json_safe(_safe_getattr(orchestrator, "health_snapshot", lambda: {})()),
            "regime_detectors": regime_detector_diags,
            "data_kitchens": data_kitchen_diags,
            "feature_views": feature_view_summaries,
            "symbol_regimes": symbol_regimes,
            "last_trace_ids": _json_safe(_safe_getattr(trader, "_last_trace_ids", {}) if trader else {}),
            "continuous_learners": continuous_learner_diags,
            "gemini": {
                "configured": bool(_safe_getattr(trader, "_gemini_api_key", None)) if trader else False,
                "model_name": _safe_getattr(trader, "_gemini_model_name", None) if trader else None,
                "pending_refresh": bool(_safe_getattr(trader, "_pending_ai_analysis_refresh", False)) if trader else False,
                "analysis_available": bool(_safe_getattr(trader, "_last_ai_analysis", "")) if trader else False,
            },
        },
        "risk_diag": {
            "snapshot": risk_matrix,
            "budget_checker": _json_safe(_safe_getattr(budget_checker, "snapshot", lambda: {})()),
            "kill_switch": _json_safe(_safe_getattr(kill_switch, "health_snapshot", lambda: {})()),
            "adaptive_risk": _json_safe(_safe_getattr(adaptive_risk, "health_snapshot", lambda: {})()),
            "state_store": _json_safe(_safe_getattr(phase2_state_store, "diagnostics", lambda: {})()),
            "symbol_risk_plans": symbol_risk_plans,
            "recent_order_rejections": _json_safe(_safe_getattr(trader, "_recent_order_rejections", []) if trader else []),
        },
        "data_sources": {
            "snapshot": data_fusion,
            "subscription_manager": _json_safe(_safe_getattr(subscription_manager, "diagnostics", lambda: {})()),
            "ws_client": _json_safe(_safe_getattr(ws_client, "diagnostics", lambda: {})()),
            "depth_registry": _json_safe(_safe_getattr(depth_registry, "diagnostics", lambda: {})()),
            "trade_registry": _json_safe(_safe_getattr(trade_registry, "diagnostics", lambda: {})()),
            "onchain_collector": _json_safe(_safe_getattr(onchain_collector, "diagnostics", lambda: {})()),
            "sentiment_collector": _json_safe(_safe_getattr(sentiment_collector, "diagnostics", lambda: {})()),
            "phase2_pipeline": {
                "external_sources_enabled": bool(_safe_getattr(trader, "_phase2_external_enabled", False)) if trader else False,
                "source_aligner": {
                    **_component_descriptor(source_aligner),
                    "config": _json_safe(_safe_getattr(_safe_getattr(source_aligner, "config", None), "__dict__", {})) if source_aligner is not None else {},
                },
                "onchain_feature_builder": _component_descriptor(onchain_feature_builder),
                "sentiment_feature_builder": _component_descriptor(sentiment_feature_builder),
            },
            "price_ages_sec": price_ages_sec,
        },
        "execution_diag": {
            "snapshot": execution,
            "audit_log_stream": {
                "active_connections": log_manager.connection_count(),
                "queue_depth": _safe_getattr(_safe_getattr(WebsocketLogSink, "_sync_queue", None), "qsize", lambda: 0)(),
            },
            "orders_count": len(_safe_getattr(trader, "_orders", []) or []) if trader else 0,
            "fills_count": len(_safe_getattr(trader, "_fills", []) or []) if trader else 0,
            "performance_attribution": {
                "summary": performance_summary,
                "strategy_attribution": strategy_attribution,
                "asset_attribution": asset_attribution,
            },
            "recent_order_rejections": _json_safe(_safe_getattr(trader, "_recent_order_rejections", []) if trader else []),
        },
        "evolution_diag": {
            "snapshot": evolution,
            "engine": _json_safe(_safe_getattr(evolution_engine, "diagnostics", lambda: {})()),
            "registry": _invoke_component_method(evolution_registry, "diagnostics", {}),
            "state_store": _invoke_component_method(evolution_state_store, "diagnostics", {}),
            "scheduler": _invoke_component_method(evolution_scheduler, "diagnostics", {}),
            "ab_manager": _invoke_component_method(evolution_ab_manager, "diagnostics", {}),
        },
        "phase3_diag": {
            "enabled": bool(_safe_getattr(trader, "_phase3_enabled", False)) if trader else False,
            "realtime_enabled": bool(_safe_getattr(trader, "_phase3_realtime_enabled", False)) if trader else False,
            "subscription_manager": _json_safe(_safe_getattr(subscription_manager, "diagnostics", lambda: {})()),
            "ws_client": _json_safe(_safe_getattr(ws_client, "diagnostics", lambda: {})()),
            "market_making": _json_safe(_safe_getattr(phase3_mm, "diagnostics", lambda: {})()),
            "rl_agent": _json_safe(_safe_getattr(phase3_ppo, "diagnostics", lambda: {})()),
            "depth_registry": _json_safe(_safe_getattr(depth_registry, "diagnostics", lambda: {})()),
            "trade_registry": _json_safe(_safe_getattr(trade_registry, "diagnostics", lambda: {})()),
            "runtime_wiring": {
                "policy_mode": _safe_getattr(trader, "_phase3_rl_policy_mode", "unknown") if trader else "unknown",
                "ws_client": _component_descriptor(ws_client),
                "subscription_manager": _component_descriptor(subscription_manager),
                "micro_feature_builder": _component_descriptor(micro_builder),
                "action_adapter": _component_descriptor(action_adapter),
                "observation_builder": _component_descriptor(obs_builder),
            },
            "params_optimizer": {
                "is_running": bool(_safe_getattr(trader, "_phase3_params_optimizer_running", False)) if trader else False,
                "thread_alive": bool(_safe_getattr(_safe_getattr(trader, "_phase3_params_optimizer_thread", None), "is_alive", lambda: False)()) if trader else False,
                "state": _structured_payload(_safe_getattr(trader, "_phase3_params_optimizer_state", {}) if trader else {}),
            },
            "candidate_bindings": {
                "strategy_candidates": _structured_payload(phase3_strategy_candidates),
                "candidate_bindings": _structured_payload(phase3_strategy_candidate_bindings),
                "metric_bindings": _structured_payload(phase3_strategy_metric_bindings),
                "candidate_experiments": _structured_payload(phase3_candidate_experiments),
                "runtime_state": _summarize_runtime_state_map(phase3_candidate_runtime_state),
            },
            "market_making_feedback": {
                "realized_trade_record_counts": {
                    strategy_id: len(records) if isinstance(records, list) else 0
                    for strategy_id, records in (phase3_mm_realized_trade_records or {}).items()
                },
                "last_realized_pnl": _structured_payload(phase3_mm_last_realized_pnl),
                "last_halt_reason": _structured_payload(phase3_mm_last_halt_reason),
            },
        },
        "alerts": alerts,
        "recent_errors": _json_safe(recent_errors),
    }
    log.debug(
        "[APIv2] Diagnostics snapshot built: status={} channels={} errors={}",
        snapshot["status"],
        len(snapshot["transport"]["channels"]),
        len(snapshot["recent_errors"]),
    )
    return snapshot


def _build_dashboard_snapshot() -> Dict[str, Any]:
    snapshot = {
        "generated_at": _iso_now(),
        "overview": _build_overview_snapshot(),
        "alpha_brain": _build_alpha_brain_snapshot(),
        "evolution": _build_evolution_snapshot(),
        "risk_matrix": _build_risk_matrix_snapshot(),
        "data_fusion": _build_data_fusion_snapshot(),
        "execution": _build_execution_snapshot(),
    }
    log.debug("[APIv2] Full dashboard snapshot built")
    return snapshot



# ── REST Endpoints ────────────────────────────────────────────

@app.get("/api/v1/status")
async def get_system_status() -> Dict[str, Any]:
    """获取系统完整状态，包含熔断原因和风控摘要。"""
    try:
        return _build_status_response()
    except Exception as e:
        log.error("API Error (Status): {}", e)
        return {"status": "error", "message": str(e)}


@app.get("/api/v1/klines")
async def get_klines(symbol: str = "BTC/USDT") -> List[Dict[str, Any]]:
    """获取缓存的历史 K 线数据供前端绘图，末尾附加当前正在形成中的蜡烛。"""
    trader = _global_trader_instance
    if not trader:
        log.warning("API: Request for klines but trader instance is None")
        return []

    closed_bars: List[Dict[str, Any]] = list(trader._kline_store.get(symbol, []))

    # ── 排序 + 去重：防止 mock 数据或乱序缓存导致 lightweight-charts 报错 ──
    if closed_bars:
        closed_bars.sort(key=lambda x: x["time"])
        deduped: List[Dict[str, Any]] = [closed_bars[0]]
        for bar in closed_bars[1:]:
            if bar["time"] > deduped[-1]["time"]:
                deduped.append(bar)
            elif bar["time"] == deduped[-1]["time"]:
                deduped[-1] = bar  # 同时间戳取最新
        closed_bars = deduped

    log.info("API: Returning {} closed klines for {}", len(closed_bars), symbol)

    # 附加当前正在形成中的蜡烛（open bar），让图表实时反映最新价格
    current_price = trader._latest_prices.get(symbol)
    if current_price and closed_bars:
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        # 当前 1h 蜡烛的开盘时间
        current_bar_open_ts = int(
            _dt.datetime(now.year, now.month, now.day, now.hour, 0, 0,
                         tzinfo=_dt.timezone.utc).timestamp()
        )
        last_closed = closed_bars[-1]
        # 仅当 developing bar 时间严格大于最后一根闭合蜡烛时才追加
        # （用 > 而非 !=，防止 mock 数据时间戳在当前小时内导致倒序）
        if current_bar_open_ts > last_closed["time"]:
            open_price = float(last_closed["close"])
            developing_bar = {
                "time": current_bar_open_ts,
                "open": open_price,
                "high": max(open_price, float(current_price)),
                "low":  min(open_price, float(current_price)),
                "close": float(current_price),
                "volume": 0.0,
            }
            closed_bars.append(developing_bar)
            log.debug("API: Appended developing bar for {} at ts={} price={:.4f}",
                      symbol, current_bar_open_ts, current_price)

    return closed_bars



class ControlAction(BaseModel):
    action: str  # "stop", "reset_circuit", "trigger_circuit_test", "rollback_evolution", "trigger_weekly_optimizer"
    family_key: Optional[str] = None
    candidate_id: Optional[str] = None
    rollback_to_candidate_id: Optional[str] = None


@app.post("/api/v1/control")
async def execute_control(cmd: ControlAction) -> Dict[str, Any]:
    trader = _global_trader_instance
    if not trader:
        return {"result": "error", "message": "Trader engine is not running"}

    if cmd.action == "stop":
        trader._running = False
        return {"result": "ok", "message": "Triggering graceful shutdown"}

    elif cmd.action == "reset_circuit":
        trader.risk_manager.reset_circuit_breaker(authorized_by="api_user")
        return {"result": "ok", "message": "Circuit breaker has been reset"}

    elif cmd.action == "trigger_circuit_test":
        # 仅 paper 模式允许手动触发熔断（用于测试）
        if trader.mode != "paper":
            return {"result": "error", "message": "trigger_circuit_test only allowed in paper mode"}
        trader.risk_manager._trigger_circuit_breaker("手动测试触发熔断 [trigger_circuit_test]")
        return {"result": "ok", "message": "Circuit breaker triggered for testing"}

    elif cmd.action == "rollback_evolution":
        rollback_result = _safe_getattr(
            trader,
            "manual_rollback_evolution",
            lambda **_: {"ok": False, "message": "Rollback handler unavailable"},
        )(
            family_key=cmd.family_key,
            current_candidate_id=cmd.candidate_id,
            rollback_to_candidate_id=cmd.rollback_to_candidate_id,
        )
        if rollback_result.get("ok"):
            message = rollback_result.get("message", "Evolution rollback applied")
            rollback_from = rollback_result.get("rollback_from")
            rollback_to = rollback_result.get("rollback_to")
            if rollback_from and rollback_to:
                message = f"{message}: {rollback_from} -> {rollback_to}"
            return {"result": "ok", "message": message}
        return {
            "result": "error",
            "error_code": str(rollback_result.get("error_code", "ROLLBACK_FAILED")),
            "message": str(rollback_result.get("message", "No rollback target is available")),
        }

    elif cmd.action == "trigger_weekly_optimizer":
        trigger_result = _safe_getattr(trader, "trigger_weekly_ml_params_optimization", lambda: {"ok": False, "message": "Weekly optimizer handler unavailable"})()
        if trigger_result.get("ok"):
            slot_id = trigger_result.get("slot_id")
            message = trigger_result.get("message", "Weekly params optimizer triggered")
            if slot_id:
                message = f"{message}: {slot_id}"
            return {"result": "ok", "message": message}
        return {"result": "error", "message": str(trigger_result.get("message", "Failed to trigger weekly params optimizer"))}

    return {"result": "error", "message": f"Unknown action: {cmd.action}"}


@app.get("/api/v1/health")
async def health_check() -> Dict[str, Any]:
    """健康检查端点，用于 Electron 主进程探活。"""
    return {
        "status": "ok",
        "version": "1.1.0",
        "ws_log_connections": log_manager.connection_count(),
        "ws_status_connections": status_manager.connection_count(),
    }


@app.get("/api/v2/dashboard/overview")
async def get_dashboard_overview() -> Dict[str, Any]:
    return _build_overview_snapshot()


@app.get("/api/v2/dashboard/alpha-brain")
async def get_dashboard_alpha_brain() -> Dict[str, Any]:
    return _build_alpha_brain_snapshot()


@app.get("/api/v2/dashboard/evolution")
async def get_dashboard_evolution() -> Dict[str, Any]:
    return _build_evolution_snapshot()


@app.get("/api/v2/dashboard/risk-matrix")
async def get_dashboard_risk_matrix() -> Dict[str, Any]:
    return _build_risk_matrix_snapshot()


@app.get("/api/v2/dashboard/data-fusion")
async def get_dashboard_data_fusion() -> Dict[str, Any]:
    return _build_data_fusion_snapshot()


@app.get("/api/v2/dashboard/execution")
async def get_dashboard_execution() -> Dict[str, Any]:
    return _build_execution_snapshot()


@app.get("/api/v2/dashboard/snapshot")
async def get_dashboard_snapshot() -> Dict[str, Any]:
    return _build_dashboard_snapshot()


@app.get("/api/v2/diagnostics")
async def get_diagnostics_snapshot() -> Dict[str, Any]:
    return _build_diagnostics_snapshot()


@app.get("/api/v2/evolution/reports")
async def get_evolution_reports(limit: int = 50) -> Dict[str, Any]:
    trader = _global_trader_instance
    evolution = _get_evolution_engine(trader)
    store = _safe_getattr(evolution, "_state_store", None)
    reports = []
    if store is not None:
        try:
            history_loader = _safe_getattr(store, "load_reports", None)
            if callable(history_loader):
                reports = history_loader(limit=max(1, min(limit, 500)))
            else:
                report = store.load_report()
                reports = [report] if report else []
        except Exception:
            reports = []
    return {"generated_at": _iso_now(), "reports": reports}


@app.get("/api/v2/evolution/decisions")
async def get_evolution_decisions(limit: int = 50) -> Dict[str, Any]:
    trader = _global_trader_instance
    evolution = _get_evolution_engine(trader)
    store = _safe_getattr(evolution, "_state_store", None)
    decisions = []
    if store is not None:
        try:
            decisions = store.load_decisions(limit=limit)
        except Exception:
            decisions = []
    return {"generated_at": _iso_now(), "items": decisions}


@app.get("/api/v2/evolution/retirements")
async def get_evolution_retirements(limit: int = 50) -> Dict[str, Any]:
    trader = _global_trader_instance
    evolution = _get_evolution_engine(trader)
    store = _safe_getattr(evolution, "_state_store", None)
    retirements = []
    if store is not None:
        try:
            retirements = store.load_retirements(limit=limit)
        except Exception:
            retirements = []
    return {"generated_at": _iso_now(), "items": retirements}


@app.get("/api/v2/risk/events")
async def get_risk_events() -> Dict[str, Any]:
    snapshot = _build_risk_matrix_snapshot()
    events = []
    if snapshot.get("circuit_reason"):
        events.append({
            "event_id": f"circuit-{snapshot.get('generated_at', _iso_now())}",
            "timestamp": snapshot.get("generated_at", _iso_now()),
            "event_type": "circuit_breaker",
            "reason": snapshot["circuit_reason"],
            "details": {
                "circuit_broken": bool(snapshot.get("circuit_broken", False)),
                "consecutive_losses": int(snapshot.get("consecutive_losses", 0)),
                "daily_pnl": float(snapshot.get("daily_pnl", 0.0)),
            },
        })
    return {"generated_at": _iso_now(), "items": events}


@app.get("/api/v2/execution/fills")
async def get_execution_fills() -> Dict[str, Any]:
    snapshot = _build_execution_snapshot()
    return {"generated_at": _iso_now(), "items": snapshot.get("recent_fills", [])}


@app.get("/api/v2/execution/orders")
async def get_execution_orders() -> Dict[str, Any]:
    snapshot = _build_execution_snapshot()
    return {"generated_at": _iso_now(), "items": snapshot.get("open_orders", [])}


@app.get("/api/v2/data/freshness")
async def get_data_freshness() -> Dict[str, Any]:
    snapshot = _build_data_fusion_snapshot()
    return {
        "generated_at": _iso_now(),
        "freshness_summary": snapshot.get("freshness_summary", {}),
        "stale_fields": snapshot.get("stale_fields", []),
    }


# ── WebSocket Endpoints ─────────────────────────────────────

@app.websocket("/api/v1/ws/logs")
async def websocket_logs_endpoint(websocket: WebSocket):
    """供前端连接以接收实时终端流输出。支持心跳 ping/pong。"""
    await log_manager.connect(websocket)
    # 连接成功后立即发送欢迎/确认消息
    try:
        await websocket.send_text(f"[{datetime.now().strftime('%H:%M:%S')}] system | Successfully connected to live audit stream.\n")
    except Exception:
        pass
    
    try:

        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        log_manager.disconnect(websocket)
    except Exception:
        log_manager.disconnect(websocket)


@app.websocket("/api/v1/ws/status")
async def websocket_status_endpoint(websocket: WebSocket):
    """
    供前端连接以接收实时系统状态推送（每 3 秒一次）。
    
    前端可订阅此通道替代轮询 /api/v1/status，降低 HTTP 开销。
    """
    await status_manager.connect(websocket)
    # 连接后立即推送一次当前状态
    try:
        status_data = _build_status_response()
        await websocket.send_text(_json_dumps_safe(status_data))
    except Exception:
        pass

    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        status_manager.disconnect(websocket)
    except Exception:
        status_manager.disconnect(websocket)


@app.websocket("/api/v2/ws/dashboard")
async def websocket_dashboard_endpoint(websocket: WebSocket):
    await dashboard_manager.connect(websocket)
    try:
        await websocket.send_text(_json_dumps_safe(_build_dashboard_snapshot()))
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        dashboard_manager.disconnect(websocket)
    except Exception:
        dashboard_manager.disconnect(websocket)


@app.websocket("/api/v2/ws/risk")
async def websocket_risk_endpoint(websocket: WebSocket):
    await risk_manager_ws.connect(websocket)
    try:
        await websocket.send_text(_json_dumps_safe(_build_risk_matrix_snapshot()))
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        risk_manager_ws.disconnect(websocket)
    except Exception:
        risk_manager_ws.disconnect(websocket)


@app.websocket("/api/v2/ws/evolution")
async def websocket_evolution_endpoint(websocket: WebSocket):
    await evolution_manager_ws.connect(websocket)
    try:
        await websocket.send_text(_json_dumps_safe(_build_evolution_snapshot()))
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        evolution_manager_ws.disconnect(websocket)
    except Exception:
        evolution_manager_ws.disconnect(websocket)


@app.websocket("/api/v2/ws/data-health")
async def websocket_data_health_endpoint(websocket: WebSocket):
    await data_health_manager_ws.connect(websocket)
    try:
        await websocket.send_text(_json_dumps_safe(_build_data_fusion_snapshot()))
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        data_health_manager_ws.disconnect(websocket)
    except Exception:
        data_health_manager_ws.disconnect(websocket)


@app.websocket("/api/v2/ws/execution")
async def websocket_execution_endpoint(websocket: WebSocket):
    await execution_manager_ws.connect(websocket)
    try:
        await websocket.send_text(_json_dumps_safe(_build_execution_snapshot()))
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        execution_manager_ws.disconnect(websocket)
    except Exception:
        execution_manager_ws.disconnect(websocket)


@app.websocket("/api/v2/ws/diagnostics")
async def websocket_diagnostics_endpoint(websocket: WebSocket):
    await diagnostics_manager_ws.connect(websocket)
    try:
        await websocket.send_text(_json_dumps_safe(_build_diagnostics_snapshot()))
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        diagnostics_manager_ws.disconnect(websocket)
    except Exception:
        diagnostics_manager_ws.disconnect(websocket)
