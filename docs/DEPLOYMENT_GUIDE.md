# AI Quant Trader — 部署安装与使用指南

## 1. 架构概览

安装包是一个**完全自包含的单体桌面应用**，内部包含：

| 组件 | 说明 |
|---|---|
| **Electron 桌面客户端** | 前端 UI（React + TailwindCSS + Lightweight Charts） |
| **backend_trader.exe** | Python 后端（FastAPI + Uvicorn），打包在 `resources/dist/` 内 |
| **system.yaml** | 系统参数配置，打包在 `resources/configs/` 内 |

Electron 启动时会**自动拉起后端进程**，无需手动安装 Python 或启动后端。

---

## 2. 系统要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11（64 位） |
| 内存 | 建议 ≥ 4 GB |
| 磁盘空间 | 安装后约 500 MB |
| 网络 | **必须能访问互联网**（连接 HTX 交易所 API 获取行情数据） |

### 不需要安装的依赖

| 依赖 | 是否需要 | 说明 |
|---|---|---|
| Python | **不需要** | 后端已打包为独立 EXE |
| Node.js | **不需要** | Electron 已自带 |
| 数据库 | **不需要** | 默认使用 SQLite（自动创建） |
| Redis | **不需要** | 当前配置未启用 |

---

## 3. 安装步骤

### Step 1：获取安装包

安装包位置：

```
apps/desktop-client/release/AI Quant Trader Setup 0.0.0.exe   (~243 MB)
```

将此文件拷贝到目标电脑（U 盘 / 网络传输均可）。只需要这一个文件。

### Step 2：运行安装程序

1. 双击 `AI Quant Trader Setup 0.0.0.exe`
2. 若 Windows SmartScreen 弹出提示，点击 **"更多信息" → "仍要运行"**
3. NSIS 安装向导弹出后，可**自定义安装目录**（默认 `C:\Program Files\AI Quant Trader`）
4. 勾选 **"创建桌面快捷方式"**
5. 点击 **"安装"**
6. 安装完成后可勾选 **"运行 AI Quant Trader"**

### Step 3：配置交易所 API 密钥（必须）

> **重要**：即使是模拟盘（paper）模式，也需要 HTX API 密钥来拉取 K 线和 ticker 行情数据。

在**安装目录**（即 `AI Quant Trader.exe` 所在文件夹）下创建 `.env` 文件：

```env
# .env — 放在 AI Quant Trader.exe 同目录下
TRADING_MODE=paper
EXCHANGE_ID=htx

# HTX API 密钥（模拟盘只需读取权限，不需要交易/提币权限）
HTX_API_KEY=your_htx_api_key_here
HTX_SECRET=your_htx_secret_here
```

**如何获取 HTX API Key：**

1. 登录 [HTX 官网](https://www.htx.com)
2. 进入 **API 管理** 页面
3. 创建新的 API Key
4. **权限设置：只勾选"读取"权限**（不要勾选交易、提币等权限）
5. 完成安全验证后，复制 API Key 和 Secret Key
6. 将密钥填入 `.env` 文件

> ⚠️ **安全提醒**：模拟盘强烈建议创建 **只读权限** 的 API Key，这样即使密钥泄露也不会有资金风险。

### Step 4：启动应用

双击桌面快捷方式 **"AI Quant Trader"**。

启动流程（自动执行）：

```
Electron 启动
  → 在 resources/dist/ 找到 backend_trader.exe
  → 启动后端进程（自动设置 TRADING_MODE=paper）
  → 轮询健康检查 http://localhost:8000/api/v1/health（最多 30 秒）
  → 后端就绪 → 打开主窗口
  → 前端加载 → WebSocket 连接后端
  → 开始显示实时数据
```

首次启动需要等待 **10-30 秒**（后端需要从交易所拉取历史 K 线数据）。

---

## 4. 界面功能说明

| 区域 | 功能 |
|---|---|
| **K 线图** | BTC/USDT 1 小时 K 线图，每 60 秒自动刷新 |
| **实时价格** | BTC / ETH / SOL 价格通过 WebSocket 每 3 秒推送更新 |
| **状态面板** | 显示运行模式（paper）、交易所（htx）、权益（equity） |
| **连接指示器** | WebSocket 连接状态（绿色 = 已连接） |
| **日志面板** | 实时后端日志流（策略信号、订单执行、风控决策等） |
| **控制按钮** | 发送控制指令（暂停/恢复等） |

---

## 5. 查看运行状态

### 方式 1：界面直接查看

- 顶部显示 **equity（权益）** 实时更新
- 价格面板显示三个交易对的实时价格
- 底部日志面板显示策略运行状态、交易信号、订单执行等

### 方式 2：日志文件

日志文件位于安装目录的 `logs/` 子目录下，每天自动轮转：

| 日志文件 | 内容 |
|---|---|
| `system_YYYY-MM-DD.log` | 系统运行日志（策略计算、信号生成、错误信息） |
| `audit_YYYY-MM-DD.log` | 审计日志（订单提交、风控决策、资金变动） |
| `trades_YYYY-MM-DD.log` | 交易记录（每笔成交 FILL、止损 STOP_LOSS、拒绝 REJECTED） |

> 日志保留 365 天，自动清理过期文件。

### 方式 3：浏览器访问 API

后端在本地 `localhost:8000` 运行，可在浏览器或工具中访问：

| 接口 | 说明 |
|---|---|
| `GET http://localhost:8000/api/v1/health` | 健康检查 |
| `GET http://localhost:8000/api/v1/klines?symbol=BTC/USDT&timeframe=1h&limit=500` | K 线数据 |
| `WS ws://localhost:8000/api/v1/ws/status` | 实时状态推送（权益、价格、模式） |
| `WS ws://localhost:8000/api/v1/ws/logs` | 实时日志推送 |

---

## 6. 关键文件位置

安装完成后，安装目录结构如下：

```
AI Quant Trader.exe              ← Electron 主程序
.env                             ← 手动创建的密钥配置文件
resources/
  dist/backend_trader.exe        ← Python 后端（Electron 自动启动）
  configs/system.yaml            ← 系统参数配置
logs/                            ← 运行日志（自动创建）
  system_YYYY-MM-DD.log
  audit_YYYY-MM-DD.log
  trades_YYYY-MM-DD.log
storage/                         ← 状态持久化（自动创建）
  trader_state.json              ← 模拟盘仓位/资金快照
```

---

## 7. 运行参数说明

### 模拟盘默认参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| 初始资金 | 5,000 USDT | 虚拟资金 |
| 交易对 | BTC/USDT, ETH/USDT, SOL/USDT | 三个主流币种 |
| K 线周期 | 1h | 1 小时线 |
| 策略数量 | 7 | 3×MACross + 3×Momentum + 1×ML |
| 再平衡周期 | 24 根 K 线（约 1 天） | 定时再平衡 |
| 漂移阈值 | 5% | 权重偏离超过此值触发再平衡 |
| 最大单币仓位 | 20% | 风控硬约束 |
| 最大回撤熔断 | 10% | 超过则触发全仓止损 |
| 模拟手续费 | 0.1% | 每笔成交收取 |
| 模拟滑点 | 0.1% | 模拟市场冲击 |

### 配置修改

如需修改运行参数，编辑安装目录下的 `resources/configs/system.yaml` 文件。修改后需**重启应用**生效。

---

## 8. 注意事项

### 网络相关

- **防火墙**：首次启动时 Windows 防火墙可能弹窗请求放行 `backend_trader.exe` 的网络访问，请点击 **"允许"**（它需要连接 HTX API 拉取行情数据）
- **代理/VPN**：如果您的网络环境需要代理才能访问 HTX，可能需要在系统层面配置代理

### 端口相关

- 后端固定监听 **localhost:8000**，确保该端口未被其他程序占用
- 如端口冲突，关闭占用端口的程序后重新启动

### 运行相关

- **不要同时运行多个实例**：会导致端口 8000 冲突
- **关闭应用**：直接关闭窗口即可，Electron 会自动停止后端进程
- **状态持久化**：关闭应用再重启，模拟盘的仓位和资金会从 `trader_state.json` 自动恢复
- **ML 模型重训**：启动后积累 500 根 K 线数据后会自动触发机器学习模型重训练（约需 2 秒）

### 故障排查

| 现象 | 可能原因 | 解决方案 |
|---|---|---|
| 启动后白屏 30 秒以上 | 后端启动超时 | 检查 `.env` 中 API 密钥是否正确；检查网络是否能访问 HTX |
| 界面显示 "Connecting..." | WebSocket 未连接 | 等待几秒后端就绪；检查端口 8000 是否被占用 |
| 价格不更新 | 网络问题或 API 限流 | 检查网络连接；等待 1-2 分钟自动恢复 |
| 安装时被杀毒软件拦截 | PyInstaller 打包的 EXE 可能被误报 | 将安装目录加入杀毒软件白名单 |
| 界面没有 K 线图 | 后端尚未拉取到历史数据 | 首次启动需 10-30 秒加载历史 K 线 |

### 安全相关

- **密钥安全**：`.env` 文件包含 API 密钥，请勿分享给他人
- **只读 API**：模拟盘务必使用只读权限的 API Key
- **本地运行**：所有数据处理在本地完成，不会上传到任何第三方服务器

---

## 9. 重新构建安装包（开发者）

如果修改了源代码，需要重新构建安装包：

```powershell
# 进入项目根目录
cd D:\recording\AI_tool\AI-driven-cryptocurrency-quantitative-trading-system

# 1. 重新打包后端 EXE
pyinstaller backend_trader.spec --noconfirm --clean

# 2. 构建前端 + 生成安装包
cd apps\desktop-client
npm run build
```

构建完成后，新的安装包位于：

```
apps/desktop-client/release/AI Quant Trader Setup 0.0.0.exe
```
