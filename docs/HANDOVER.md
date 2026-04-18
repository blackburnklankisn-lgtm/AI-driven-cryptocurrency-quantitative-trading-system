# 🔄 AI 驱动加密货币现货量化交易系统 — 项目交接文档

> **生成时间**: 2026-04-18 | **分支**: `feature/AIQUANT-01/electron-ui` | **最新 Commit**: `ab56103`

---

## 一、项目概述

这是一个**全栈 AI 驱动的加密货币现货量化交易系统**，支持实盘/模拟盘交易，包含从数据采集、策略引擎、风控管理到 Electron 桌面端 UI 的完整链路。

**技术栈**：
- **后端**: Python 3.9+ / FastAPI / Uvicorn / Loguru / CCXT
- **前端**: Electron 41 + React 19 + Vite 8 + Tailwind CSS 4 + TypeScript
- **监控**: Prometheus + Grafana
- **ML**: LightGBM / RandomForest（Walk-Forward 训练）
- **容器化**: Docker Compose

**仓库地址**: `https://github.com/blackburnklankisn-lgtm/AI-driven-cryptocurrency-quantitative-trading-system.git`

---

## 二、系统架构

```
┌─────────────────────────────────────────────────────┐
│                 Electron Desktop App                 │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────┐ │
│  │ Dashboard    │  │ Live Audit   │  │ Controls   │ │
│  │ (Equity/Pos) │  │ Stream (WS)  │  │ (熔断/停止)│ │
│  └──────┬───────┘  └──────┬───────┘  └─────┬──────┘ │
└─────────┼─────────────────┼────────────────┼────────┘
          │ ws/status       │ ws/logs        │ REST
          ▼                 ▼                ▼
┌─────────────────────────────────────────────────────┐
│            FastAPI Bridge (port 8000)                 │
│  GET /api/v1/status  POST /api/v1/control            │
│  WS  /api/v1/ws/logs  WS /api/v1/ws/status           │
└──────────────────────┬──────────────────────────────┘
                       │ set_trader_instance()
┌──────────────────────▼──────────────────────────────┐
│              LiveTrader (主线程: uvicorn)             │
│              交易子线程: _run_loop()                  │
│  ┌─────────┐ ┌──────────┐ ┌───────────┐ ┌────────┐ │
│  │CCXTGateway│ │RiskManager│ │OrderManager│ │Strategies│
│  │(Binance) │ │(熔断/风控)│ │(订单生命周期)│ │(MA Cross)│
│  └─────────┘ └──────────┘ └───────────┘ └────────┘ │
└─────────────────────────────────────────────────────┘
```

**关键设计决策**：
- FastAPI 占用**主线程**（uvicorn event loop），交易逻辑在**守护子线程**运行
- WebSocket 日志推送使用 `asyncio.run_coroutine_threadsafe()` 跨线程安全提交
- 前端通过 `ws/status` 通道接收状态推送（替代 REST 轮询），`ws/logs` 接收日志流

---

## 三、目录结构

```
├── apps/
│   ├── api/server.py          # FastAPI 后端桥接服务（REST + WebSocket）
│   ├── trader/main.py         # 实盘/模拟盘主控程序入口
│   ├── backtest/              # 回测引擎
│   └── desktop-client/        # Electron + React 前端
│       ├── electron/main.ts   # Electron 主进程（含 Python 进程管理）
│       ├── electron/preload.ts
│       ├── src/App.tsx         # React 主组件
│       └── package.json        # 含 electron-builder 打包配置
├── core/
│   ├── config.py              # Pydantic 配置加载
│   ├── event.py               # 事件总线 + 事件类型定义
│   ├── logger.py              # Loguru 日志工厂（含脱敏过滤器）
│   └── exceptions.py          # 业务异常定义
├── modules/
│   ├── data/                  # 数据层（下载/校验/存储）
│   ├── alpha/                 # 策略层（MA Cross / Momentum / ML）
│   ├── risk/                  # 风控层（熔断器/仓位限制）
│   ├── execution/             # 执行层（CCXT 网关/订单管理）
│   ├── portfolio/             # 组合管理（分配/再平衡）
│   └── monitoring/            # Prometheus 指标
├── configs/system.yaml        # 系统配置（不含密钥）
├── scripts/
│   ├── stress_test_ws.py      # WebSocket 30分钟压测脚本
│   └── run_backtest_demo.py
├── tests/
│   ├── test_security_audit.py # 安全审计测试（20 cases）
│   └── ...                    # 各模块单元测试
├── docker/                    # Docker Compose 部署
├── docs/TEST_CASES.md         # 全面测试用例库
└── .env.example               # 环境变量模板
```

---

## 四、开发进度（已完成 6 个阶段）

| 阶段 | 状态 | 说明 |
|------|------|------|
| Phase 1: 核心基础设施 | ✅ | EventBus、Config、Logger、Exceptions |
| Phase 2: 数据层 | ✅ | CCXT 下载器、Parquet 存储、数据校验 |
| Phase 3: Alpha 策略 + 风控 | ✅ | MA Cross、Momentum、RiskManager 熔断器 |
| Phase 4: 实盘执行 + 监控 | ✅ | CCXT Gateway、OrderManager、Prometheus |
| Phase 5: ML Alpha + 组合管理 | ✅ | LightGBM 预测、Walk-Forward、Portfolio |
| Phase 6: Electron UI + 部署测试 | ✅ | FastAPI 桥接、React UI、打包、安全审计 |

---

## 五、当前待验证/待优化事项

### 🔴 需要立即验证（在可访问 Binance 的网络环境下）
1. **Live Audit Stream 日志流验证** — WebSocket 跨线程推送已修复（`run_coroutine_threadsafe`），但因公司防火墙拦截 Binance API 未能在 UI 上完整验证
2. **熔断-恢复闭环测试** — `POST /api/v1/control {"action":"trigger_circuit_test"}` 触发熔断 → UI 显示红色 CIRCUIT BROKEN + 原因 → 点击 Reset Circuit 恢复
3. **30 分钟 WebSocket 压测** — `python scripts/stress_test_ws.py --duration 1800`

### 🟡 可优化项
4. **Electron 打包** — `npm run build` 生成 `.exe`，验证独立运行（需 Python 环境或 PyInstaller 打包后端）
5. **前端虚拟滚动** — 当前日志列表用 `map` 渲染，超过 1000 行时可考虑 `react-window`
6. **WebSocket 心跳超时检测** — 前端目前只发 ping，未检测 pong 超时

---

## 六、快速启动指南

### 环境准备
```bash
# Python 3.9+
pip install -e .

# Node.js 18+ (前端)
cd apps/desktop-client && npm install && cd ../..

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入 Binance API Key（paper 模式不需要真实 Key）
```

### 启动系统
```bash
# 终端1: 启动后端
export TRADING_MODE=paper  # Linux/Mac
# set TRADING_MODE=paper   # Windows CMD
# $env:TRADING_MODE="paper" # Windows PowerShell
python -m apps.trader.main

# 终端2: 启动前端
cd apps/desktop-client
npm run dev
```

### 运行测试
```bash
# 安全审计测试 (20 cases)
python -m pytest tests/test_security_audit.py -v

# 全部测试
python -m pytest tests/ -v
```

---

## 七、关键 API 接口

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/v1/health` | GET | 健康检查 |
| `/api/v1/status` | GET | 系统状态（含 risk_state、circuit_reason） |
| `/api/v1/control` | POST | 控制操作：`stop` / `reset_circuit` / `trigger_circuit_test` |
| `/api/v1/ws/logs` | WS | 实时日志流（loguru → WebSocket） |
| `/api/v1/ws/status` | WS | 系统状态推送（每 3 秒） |

---

## 八、已知问题

1. **公司网络防火墙拦截 Binance API** — SIA-Gateway 返回 403，需在可访问 Binance 的网络环境下测试
2. **`dist-electron/main.js` 被提交到 Git** — 建议添加到 `.gitignore`
3. **Prometheus 端口 8001 硬编码** — 可考虑配置化
