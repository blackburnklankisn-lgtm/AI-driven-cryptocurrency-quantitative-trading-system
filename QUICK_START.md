# 🚀 快速启动指南

**应用**: AI Quant Trader with Price Latency Display  
**版本**: 0.0.0  
**发布日期**: 2026-04-26

---

## ⚡ 30 秒快速开始

### 方法 1: 一键安装 (推荐) ⭐

```bash
# 双击运行安装程序
AI Quant Trader Setup 0.0.0.exe

# 按照安装向导完成
# ✅ 应用将自动启动并连接到后端
```

**优点**: 自动配置、桌面快捷方式、一键卸载

---

### 方法 2: 便携版本 (无需安装)

```bash
# 仅需 3 个命令

# 1️⃣  启动后端 API 服务
set TRADING_MODE=paper
dist\backend_trader.exe

# 2️⃣  (在另一个终端) 启动前端应用
apps\desktop-client\release\win-unpacked\AI Quant Trader.exe

# 3️⃣  在应用中打开 "数据融合" 页面
#    看到价格表包含 4 列 ✅
```

---

### 方法 3: 源代码开发模式

```bash
# 适合开发者，支持热重载

# 1️⃣  启动前端开发服务器
cd apps\desktop-client
npm install
npm run dev

# 2️⃣  (在另一个终端) 启动后端
cd ../..
python -m apps.trader.main
```

---

## 📍 关键文件位置

```
项目根目录/
├── 🎯 dist/backend_trader.exe        ← 后端 API 服务
├── 🎯 apps/desktop-client/
│   ├── release/
│   │   ├── AI Quant Trader Setup 0.0.0.exe  ← ⭐ 安装程序
│   │   └── win-unpacked/
│   │       └── AI Quant Trader.exe           ← 便携版本
│   └── src/pages/DataFusionPage.tsx  ← 前端代码
├── 📄 BUILD_REPORT.md                 ← 完整构建报告
├── 📄 .env                            ← 配置文件
└── 📁 configs/                        ← 交易配置
```

---

## 🔍 验证安装

安装完成后，应该看到:

```
✅ 后端 API 运行在 http://localhost:8000
✅ 应用启动并显示仪表板
✅ "数据融合" 页面显示价格表
✅ 价格表有 4 列:
   • 标的 (BTC/USDT, ETH/USDT, SOL/USDT)
   • 最新价 (数值)
   • 最近更新 (时间 HH:MM:SS)
   • 延迟秒数 (颜色编码)
```

### 色彩代表什么？

```
🟢 绿色   = < 10 秒   (数据很新鲜，在线中)
🟡 黄色   = 10-30 秒  (有延迟，但可接受)
🔴 红色   = > 30 秒   (严重延迟，可能离线)
```

---

## ⚙️ 配置后端

创建或编辑 `.env` 文件:

```env
# 交易模式 (paper = 模拟交易, live = 真实交易)
TRADING_MODE=paper

# API 配置
API_HOST=0.0.0.0
API_PORT=8000

# 数据源
EXCHANGE=htx
SYMBOLS=BTC/USDT,ETH/USDT,SOL/USDT

# 日志级别
LOG_LEVEL=INFO
```

---

## 🔧 常见问题

### Q: 应用启动后显示"连接失败"

**A**: 确保后端在运行
```bash
# 终端 1: 启动后端
set TRADING_MODE=paper
dist\backend_trader.exe

# 显示 "Uvicorn running on http://[::]:8000" 时表示成功
```

### Q: 价格表显示为空

**A**: 等待 30 秒让数据加载，然后刷新页面 (F5)

### Q: 延迟秒数显示很大 (> 60 秒)

**A**: 这是正常的，表示数据源可能延迟
- 检查网络连接
- 检查交易所是否在线
- 查看后端日志

### Q: 安装程序无法运行

**A**: 使用便携版本替代
```bash
apps\desktop-client\release\win-unpacked\AI Quant Trader.exe
```

---

## 📊 实时监控

应用启动后，可以实时观察:

| 指标 | 显示位置 | 含义 |
|------|---------|------|
| 价格 | DataFusion 页面 | 最新交易价格 |
| 延迟 | 颜色编码 | 数据多久前更新 |
| 更新时间 | HH:MM:SS | 上次更新的准确时刻 |
| 风控状态 | Risk Matrix | 电路断路器是否触发 |
| 持仓 | Execution | 当前持仓和盈亏 |

---

## 🚀 下一步操作

### 第 1 步: 确认部署 (2 分钟)

```bash
# 启动应用，验证 DataFusion 页面显示价格表
# 观察延迟秒数在 0-10 之间且为绿色
```

### 第 2 步: 配置交易参数 (5 分钟)

编辑 `configs/system.yaml`:
```yaml
trading:
  mode: paper  # 模拟交易
  symbols:
    - BTC/USDT
    - ETH/USDT
    - SOL/USDT
  
risk_control:
  max_drawdown: 0.1    # 10% 最大回撤
  daily_loss_limit: 100  # 日亏损限制
```

### 第 3 步: 运行回测 (10 分钟)

```bash
# 评估策略性能
python -m apps.backtest.main --config configs/system.yaml
```

### 第 4 步: 启动实时交易

```bash
# 在 Paper 模式中运行 (虚拟交易)
$env:TRADING_MODE='paper'
dist\backend_trader.exe

# 观察应用中的交易执行
```

---

## 📞 技术支持

### 日志文件位置

```
logs/
├── openalgo_2026-04-26.log    ← 应用日志
├── errors.jsonl               ← 错误日志
└── metrics_2026-04-26.log     ← 性能指标
```

### 调试模式

```bash
# 启用详细日志
set LOG_LEVEL=DEBUG
dist\backend_trader.exe
```

### API 文档

启动后端后访问:
```
http://localhost:8000/api/docs
```

查看所有可用的 API 端点和参数

---

## ✅ 成功标志

应用成功运行的标志:

- ✅ 应用窗口打开，无错误提示
- ✅ 导航栏显示所有页面 (Dashboard, DataFusion, Risk Matrix 等)
- ✅ DataFusion 页面显示价格表，4 列清晰可见
- ✅ 延迟秒数在 0-10 秒，显示为绿色
- ✅ 后端终端显示 "Uvicorn running on http://[::]:8000"
- ✅ 可以查看持仓、订单、风控信息

---

## 🎯 主要新增功能

### 价格延迟实时显示 ⭐

**新增内容**:
- 🕐 每个品种显示最后更新时间
- ⏱️  延迟秒数自动计算
- 🎨 颜色编码指示数据新鲜度
- 📊 支持多品种实时监控

**使用场景**:
- 判断数据是否实时
- 发现连接问题
- 评估交易延迟
- 监控数据源健康度

### 示例

```
BTC/USDT  78099.79  16:39:27  0.5s  🟢
ETH/USDT  2330.81   16:39:27  0.8s  🟢
SOL/USDT  86.6245   16:39:27  0.6s  🟢
```

---

## 📦 版本信息

- **应用版本**: 0.0.0
- **构建时间**: 2026-04-26 16:48 UTC
- **平台**: Windows x64 (Windows 10/11)
- **后端**: Python 3.12 + FastAPI
- **前端**: React 19 + Vite + Electron
- **打包工具**: PyInstaller + electron-builder

---

## 🔐 安全性

- ✅ 所有可执行文件使用 signtool 签名
- ✅ 仅支持本地网络通信
- ✅ API 端点需要 API 密钥验证
- ✅ 敏感信息不在日志中显示
- ✅ 定期清理临时文件

---

**准备好了吗？** 现在就开始! 🚀

运行: `AI Quant Trader Setup 0.0.0.exe`

或者从便携版本启动: `apps\desktop-client\release\win-unpacked\AI Quant Trader.exe`

祝交易顺利! 📈
