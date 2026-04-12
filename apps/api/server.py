"""
apps/api/server.py — FastAPI 后端桥接服务

提供给外部桌面客户端（Electron）的 REST 控制信道与 WebSocket 实时推送信道。
"""

import asyncio
import json
import logging
from typing import Any, Dict, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="AI Quant Trader API", version="1.0.0")

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


# ── WebSocket 管理器 ─────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                pass


manager = ConnectionManager()

# Loguru 的 Sink，接收日志并推送到 WS
class WebsocketLogSink:
    def write(self, message):
        # 消息中包含了时间、级别和内容
        # 通过 asyncio.run_coroutine_threadsafe 调用 broadcast
        # 但因为是从同步线程过来，我们需要一个运行中的 event loop 或者巧妙地分发
        # 这里用一种简单方式：直接提取内容后交给后台异步任务
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(manager.broadcast(message))
        except RuntimeError:
            pass  # 如果不在 asyncio loop 中，忽略


# ── REST Endpoints ────────────────────────────────────────────

@app.get("/api/v1/status")
async def get_system_status() -> Dict[str, Any]:
    trader = _global_trader_instance
    if not trader:
        return {"status": "inactive", "message": "Trader engine is not running"}

    # 提取风控状态
    circuit_broken = trader.risk_manager.is_circuit_broken()

    # 提取持仓
    positions = {sym: float(qty) for sym, qty in trader._positions.items() if qty > 0}

    return {
        "status": "running",
        "mode": trader.mode,
        "exchange": trader.gateway.exchange_id,
        "equity": float(trader._current_equity),
        "positions": positions,
        "circuit_broken": circuit_broken,
        "poll_interval_s": trader._poll_interval_s,
    }


class ControlAction(BaseModel):
    action: str  # "stop", "reset_circuit"


@app.post("/api/v1/control")
async def execute_control(cmd: ControlAction) -> Dict[str, str]:
    trader = _global_trader_instance
    if not trader:
        return {"result": "error", "message": "Trader engine is not running"}

    if cmd.action == "stop":
        # 触发优雅退出
        trader._running = False
        return {"result": "ok", "message": "Triggering graceful shutdown"}
        
    elif cmd.action == "reset_circuit":
        trader.risk_manager.reset_circuit_breaker(authorized_by="api_user")
        return {"result": "ok", "message": "Circuit breaker has been reset"}

    return {"result": "error", "message": "Unknown action"}


# ── WebSocket Endpoints ─────────────────────────────────────

@app.websocket("/api/v1/ws/logs")
async def websocket_logs_endpoint(websocket: WebSocket):
    """供前端连接以接收实时终端流输出。"""
    await manager.connect(websocket)
    try:
        while True:
            # 保持心跳连接
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)
