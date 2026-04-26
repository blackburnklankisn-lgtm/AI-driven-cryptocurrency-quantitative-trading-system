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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
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
        except Exception:
            pass



# ── 状态推送后台任务 ─────────────────────────────────────────

async def _status_push_worker():
    """
    每 3 秒主动向 status WebSocket 通道推送系统状态。
    
    这样前端无需轮询 REST API，减少 HTTP 开销（阶段04 性能优化）。
    """
    while True:
        await asyncio.sleep(3)
        if status_manager.connection_count() == 0:
            continue
        try:
            status_data = _build_status_response()
            await status_manager.broadcast(json.dumps(status_data))
            if dashboard_manager.connection_count() > 0:
                await dashboard_manager.broadcast(json.dumps(_build_dashboard_snapshot()))
            if risk_manager_ws.connection_count() > 0:
                await risk_manager_ws.broadcast(json.dumps(_build_risk_matrix_snapshot()))
            if evolution_manager_ws.connection_count() > 0:
                await evolution_manager_ws.broadcast(json.dumps(_build_evolution_snapshot()))
            if data_health_manager_ws.connection_count() > 0:
                await data_health_manager_ws.broadcast(json.dumps(_build_data_fusion_snapshot()))
            if execution_manager_ws.connection_count() > 0:
                await execution_manager_ws.broadcast(json.dumps(_build_execution_snapshot()))
        except Exception:
            pass


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
            continue
        loop = asyncio.get_running_loop()
        symbols = getattr(trader, '_symbols', None) or _symbols
        cycle += 1
        updated = []
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
                log.debug("[Ticker] fetch_ticker 失败: {} {}", symbol, str(exc)[:80])
        if updated:
            log.debug("[Ticker] cycle#{} 价格更新: {}", cycle, " | ".join(updated))
        elif cycle % 12 == 0:  # 每 60s 打印一次"无变化"
            log.debug(
                "[Ticker] cycle#{} 价格无变化: {}",
                cycle,
                {s: f"{v:.4f}" for s, v in trader._latest_prices.items()},
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
        await websocket.send_text(json.dumps(status_data))
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
        await websocket.send_text(json.dumps(_build_dashboard_snapshot()))
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
        await websocket.send_text(json.dumps(_build_risk_matrix_snapshot()))
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
        await websocket.send_text(json.dumps(_build_evolution_snapshot()))
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
        await websocket.send_text(json.dumps(_build_data_fusion_snapshot()))
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
        await websocket.send_text(json.dumps(_build_execution_snapshot()))
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        execution_manager_ws.disconnect(websocket)
    except Exception:
        execution_manager_ws.disconnect(websocket)
