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
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List

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
        for symbol in symbols:
            try:
                ticker = await loop.run_in_executor(
                    None, trader.gateway.fetch_ticker, symbol
                )
                last = ticker.get('last') or ticker.get('close')
                if last:
                    old = trader._latest_prices.get(symbol, 0)
                    trader._latest_prices[symbol] = float(last)
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
        if last_closed["time"] != current_bar_open_ts:
            # 仅当闭合蜡烛不是当前小时才追加（避免重复）
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
    action: str  # "stop", "reset_circuit", "trigger_circuit_test"


@app.post("/api/v1/control")
async def execute_control(cmd: ControlAction) -> Dict[str, str]:
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
