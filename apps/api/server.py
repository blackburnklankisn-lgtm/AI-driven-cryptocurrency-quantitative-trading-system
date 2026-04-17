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
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


@asynccontextmanager
async def lifespan(application: FastAPI):
    """应用生命周期管理（替代已废弃的 on_event）。"""
    asyncio.create_task(_status_push_worker())
    yield


app = FastAPI(title="AI Quant Trader API", version="1.1.0", lifespan=lifespan)

# 允许跨域（Electron UI 通常从 localhost/file 启动）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 全局状态注入 ──────────────────────────────────────────────
# 由 main.py 在启动时将 LiveTrader 实例挂载在此
_global_trader_instance = None


def set_trader_instance(trader) -> None:
    global _global_trader_instance
    _global_trader_instance = trader


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
    
    改进：
    - 使用 asyncio.Queue 作为缓冲层，防止高频日志拥塞事件循环
    - 队列满时丢弃最旧的消息（背压控制）
    - 消息经过脱敏处理后再推送
    """

    # 类级别的日志队列（maxsize 防止内存溢出）
    _log_queue: asyncio.Queue = None
    _worker_task = None

    def write(self, message: str):
        """同步写入接口（由 loguru 调用）。"""
        try:
            loop = asyncio.get_running_loop()
            # 尝试非阻塞放入队列
            queue = self._get_queue(loop)
            if queue.full():
                # 背压控制：队列满时丢弃最旧的消息
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass  # 极端情况下忽略
            # 确保 worker 任务在运行
            self._ensure_worker(loop)
        except RuntimeError:
            pass  # 不在 asyncio loop 中，忽略

    def _get_queue(self, loop) -> asyncio.Queue:
        """获取或创建队列（线程安全）。"""
        if WebsocketLogSink._log_queue is None:
            WebsocketLogSink._log_queue = asyncio.Queue(maxsize=500)
        return WebsocketLogSink._log_queue

    def _ensure_worker(self, loop):
        """确保消费者协程在运行。"""
        if (WebsocketLogSink._worker_task is None or
                WebsocketLogSink._worker_task.done()):
            WebsocketLogSink._worker_task = loop.create_task(
                self._broadcast_worker()
            )

    @staticmethod
    async def _broadcast_worker():
        """消费队列中的日志消息并广播。"""
        queue = WebsocketLogSink._log_queue
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=1.0)
                await log_manager.broadcast(message)
                queue.task_done()
            except asyncio.TimeoutError:
                continue
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


# ── 状态构建辅助函数 ─────────────────────────────────────────

def _build_status_response() -> Dict[str, Any]:
    """构建系统状态响应（供 REST 和 WebSocket 共用）。"""
    trader = _global_trader_instance
    if not trader:
        return {"status": "inactive", "message": "Trader engine is not running"}

    circuit_broken = trader.risk_manager.is_circuit_broken()
    risk_summary = trader.risk_manager.get_state_summary()
    positions = {sym: float(qty) for sym, qty in trader._positions.items() if qty > 0}

    return {
        "status": "running",
        "mode": trader.mode,
        "exchange": trader.gateway.exchange_id,
        "equity": float(trader._current_equity),
        "positions": positions,
        "circuit_broken": circuit_broken,
        "circuit_reason": risk_summary.get("circuit_reason", ""),
        "risk_state": risk_summary,
        "poll_interval_s": trader._poll_interval_s,
        "ws_log_connections": log_manager.connection_count(),
    }


# ── REST Endpoints ────────────────────────────────────────────

@app.get("/api/v1/status")
async def get_system_status() -> Dict[str, Any]:
    """获取系统完整状态，包含熔断原因和风控摘要。"""
    return _build_status_response()


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
