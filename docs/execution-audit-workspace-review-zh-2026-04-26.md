# "执行与审计"(Execution & Audit) 工作区详解

**文档生成时间**: 2026-04-26  
**分析系统**: AI-driven Cryptocurrency Quantitative Trading System  
**工作区**: EXECUTION WS (执行与审计工作区)  

---

## 目录

1. [概述](#概述)
2. [参数解释](#参数解释)
   - [slippage_bps 和 fee_bps](#slippage_bps-和-fee_bps)
3. [执行链路分析](#执行链路分析)
   - [成交机制](#成交机制)
   - [为什么没有成交](#为什么没有成交)
   - [触发成交的条件](#触发成交的条件)
4. [仓位敞口详解](#仓位敞口详解)
   - [参数含义](#参数含义)
   - [初始持仓来源](#初始持仓来源)
   - [未实现盈亏计算](#未实现盈亏计算)
5. [审计日志机制](#审计日志机制)
   - [工作原理](#工作原理)
   - [审计频率](#审计频率)
   - [日志类型](#日志类型)
6. [代码位置参考](#代码位置参考)

---

## 概述

"执行与审计"工作区展示系统的**实时交易执行状态**和**完整审计日志流**。主要功能包括：

- **订单管理**: 显示未完成订单、最近成交
- **头寸管理**: 显示当前持仓、盈亏状况
- **模拟参数**: 展示 Paper 模式的滑点和手续费设置
- **风险控制**: 显示可用的控制动作（停止、重置熔断、触发熔断测试）
- **审计流**: 实时显示所有关键业务事件的审计日志

---

## 参数解释

### slippage_bps 和 fee_bps

在工作区的"模拟盘执行汇总"部分显示：

```json
{
  "mode": "paper",
  "slippage_bps": 10,
  "fee_bps": 10
}
```

#### 参数含义

| 参数 | 含义 | 单位 | 换算 | 用途 |
|------|------|------|------|------|
| **slippage_bps** | 滑点 | 基点 | 10 bps = 0.1% | 市价单执行价与最新价的偏离 |
| **fee_bps** | 手续费率 | 基点 | 10 bps = 0.1% | 每笔交易的成本 |

#### 详细解释

**Bps (Basis Points) = 基点**
- 1 基点 = 0.01%
- 10 基点 = 0.1% = 0.001
- 100 基点 = 1%

##### 滑点 (Slippage) 的含义

在模拟盘(Paper Mode)中，滑点代表市价单的价格偏差：

**买入订单**:
```
实际成交价 = 最新市场价 × (1 + slippage_rate)
            = 最新价 × (1 + 0.001)
            = 最新价 × 1.001
```
- 例如：最新价 = 50,000 USDT
- 买入成交价 = 50,000 × 1.001 = 50,050 USDT（多付 50 USDT）

**卖出订单**:
```
实际成交价 = 最新市场价 × (1 - slippage_rate)
            = 最新价 × (1 - 0.001)
            = 最新价 × 0.999
```
- 例如：最新价 = 50,000 USDT
- 卖出成交价 = 50,000 × 0.999 = 49,950 USDT（少收 50 USDT）

**作用**: 模拟现实交易环境中的流动性成本，使模拟盘更接近实盘交易体验

##### 手续费 (Fee) 的含义

在模拟盘中，手续费按交易名义金额的百分比收取：

```
手续费 = 名义价值 × fee_rate
       = (数量 × 成交价) × 0.001
```

**例如**:
- 买入 0.01 BTC，成交价 50,050 USDT（包含滑点）
- 名义价值 = 0.01 × 50,050 = 500.5 USDT
- 手续费 = 500.5 × 0.001 = 0.5005 USDT
- **总成本 = 500.5 + 0.5005 = 501.0005 USDT**

**作用**: 反映交易所或做市商的交易成本，影响收益计算

#### 代码位置

| 位置 | 含义 |
|------|------|
| `modules/execution/gateway.py` L112 | `self._paper_slippage_rate = Decimal("0.001")` |
| `modules/execution/gateway.py` L111 | `self._paper_fee_rate = Decimal("0.001")` |
| `apps/api/server.py` L1002-1003 | API 返回的硬编码值 |

---

## 执行链路分析

### 成交机制

#### Paper 模式下的自动成交

在 `modules/execution/gateway.py` 的 `_paper_submit()` 方法中实现：

**成交流程**:

```
提交订单 (submit_order)
    ↓
判断订单类型
    ├─ 市价单 (market)
    │   ├─ 查询最新价格 (_paper_latest_prices)
    │   ├─ 如果无行情数据 → 拒绝订单 (OrderSubmissionError)
    │   ├─ 计算成交价: 
    │   │   ├─ 买入: price × (1 + slippage_rate)
    │   │   └─ 卖出: price × (1 - slippage_rate)
    │   ↓
    └─ 限价单 (limit)
        └─ 使用指定价格成交

确定成交价后 → 计算手续费 → 检查资金/头寸
    ├─ 买入: 检查现金是否足够（包含手续费）
    ├─ 卖出: 检查该品种持仓是否足够
    ↓
执行成交 → 更新 paper_cash 和 paper_positions
    ↓
记录到审计日志 (PAPER_FILL)
    ↓
返回订单 ID (paper_xxxxxx)
```

**代码参考**: `modules/execution/gateway.py` L323-460

### 为什么没有成交

根据当前工作区显示"最近成交 (0)"，这可能由以下几个原因造成：

#### 原因 1: 没有订单被提交 ❌

**症状**: 
- 未完成订单数为 0
- 最近成交为 0

**可能的原因**:
1. **策略没有生成交易信号**
   - Alpha Brain/StrategyOrchestrator 未生成 OrderRequestEvent
   - 检查策略配置是否启用
   
2. **风控拦截了订单**
   - RiskManager 的 approve_order() 返回 False
   - 可能原因: 电路断路、头寸过大、每日亏损超限等
   - 查看"风险矩阵"工作区的熔断状态

3. **订单路由服务故障**
   - OrderRouterService 无法提交订单到网关
   - 检查日志中的异常信息

#### 原因 2: 订单被拒绝 ❌

在 `modules/execution/gateway.py` L340-405 中，订单被拒绝的常见原因：

| 拒绝原因 | 代码位置 | 解决方法 |
|---------|---------|---------|
| 无行情数据 | L347-350 | 检查 `update_paper_price()` 是否被调用 |
| 现金不足 | L371-383 | 增加模拟资金或减少订单规模 |
| 持仓不足 | L389-401 | 先买入才能卖出 |

#### 原因 3: 订单成交但未显示 ⚠️

虽然订单已成交，但前端未正确刷新：
- WebSocket 连接断开
- ExecutionSnapshot 未被推送到前端
- 前端缓存未更新

**检查方法**:
```bash
# 查看后端日志
curl http://localhost:8000/api/v2/dashboard/execution

# 检查最近成交记录
# 应该在 JSON 响应的 "recent_fills" 字段中
```

### 触发成交的条件

要在 Paper 模式下成功成交，**必须同时满足**以下条件：

#### 条件 1: 有足够的行情数据 ✅

```python
# 必须通过以下任一方式更新行情
gateway.update_paper_price(symbol, price)
```

**在 Trader 中的调用点**:

| 调用位置 | 频率 | 来源 |
|---------|------|------|
| `_fetch_latest_klines()` | ~60s | CCXT REST 轮询 |
| `_ticker_refresh_worker()` | ~5s | FastAPI 异步 Worker |
| 策略市场数据 | 变动 | 实时数据源 |

**检查命令**:
```bash
# 查看最新价格
curl http://localhost:8000/api/v2/dashboard/data-fusion | jq .latest_prices
```

#### 条件 2: 有足够的资金 ✅

```python
# 买入条件: cash >= quantity × price × (1 + slippage) + fee
# Paper 初始现金 = 5000 USDT

# 例如要买 0.1 BTC @ 50,000 USDT:
notional = 0.1 × 50,000 × 1.001 = 5,000.5 USDT (包含滑点)
fee = 5,000.5 × 0.001 = 5.0 USDT
total_cost = 5,000.5 + 5.0 = 5,005.5 USDT
# 需要 cash >= 5,005.5 USDT
```

**检查命令**:
```bash
# 查看 Paper 现金余额
curl http://localhost:8000/api/v2/dashboard/execution | jq .paper_summary
# 或查看持仓
curl http://localhost:8000/api/v2/dashboard/execution | jq .positions
```

#### 条件 3: 有足够的持仓 (卖出时) ✅

```python
# 卖出条件: positions[symbol] >= quantity

# 例如要卖出 0.01 BTC:
# 需要 BTC/USDT 持仓 >= 0.01
```

#### 条件 4: 风控审批通过 ✅

```python
# 风控检查项目:
1. 电路断路器未触发 (circuit_broken = False)
2. 单笔订单不超过最大头寸 (max_position_pct)
3. 当日亏损未超限 (max_portfolio_drawdown)
4. 连续亏损次数未超限 (max_consecutive_losses)
```

**检查命令**:
```bash
# 查看风控状态
curl http://localhost:8000/api/v2/dashboard/risk-matrix | jq .circuit_broken
```

#### 条件 5: 有交易信号 ✅

```python
# 策略需要生成 OrderRequestEvent:
# 1. Alpha Brain 的某个策略触发信号
# 2. 该信号通过 StrategyOrchestrator 转化为 OrderRequestEvent
# 3. OrderRouterService 接收并提交
```

**调试步骤**:
1. 检查策略配置是否启用
2. 查看 AlphaBrain 工作区是否有信号
3. 检查 Trainer 日志中的 "ORDER_REQUEST_GENERATED" 事件

---

## 仓位敞口详解

### 参数含义

当前仓位显示表格的每一列含义：

```
当前持仓
┌─────────────┬────────┬──────────┬──────────┬──────────────┬────────────┐
│ 标的        │ 数量   │ 开仓价   │ 最新价   │ 未实现盈亏   │ 名义价值   │
├─────────────┼────────┼──────────┼──────────┼──────────────┼────────────┤
│ BTC/USDT    │0.01908 │77560.54  │77809.85  │4.76          │1484.47     │
│ ETH/USDT    │0.86272 │2314.10   │2319.19   │4.39          │2000.82     │
│ SOL/USDT    │15.1003 │86.30     │86.44     │2.24          │1305.32     │
└─────────────┴────────┴──────────┴──────────┴──────────────┴────────────┘
```

#### 各列参数详解

| 列名 | 中文名 | 含义 | 计算公式 | 例子 |
|------|--------|------|---------|------|
| **标的** | 交易对 | 交易的币种对 | - | BTC/USDT |
| **数量** | 持仓数量 | 当前拥有该币种的数量 | - | 0.01908 BTC |
| **开仓价** | 平均开仓价 | 该持仓的加权平均买入价 | Σ(qty × price) / Σqty | 77,560.54 USDT/BTC |
| **最新价** | 当前市场价 | 实时行情价格 | 来自 _latest_prices | 77,809.85 USDT/BTC |
| **未实现盈亏** | 浮动盈亏 | 如果现在平仓的盈亏(USDT) | (最新价 - 开仓价) × 数量 | (77,809.85 - 77,560.54) × 0.01908 = 4.76 USDT |
| **名义价值** | 头寸规模 | 该持仓的当前市场价值 | 数量 × 最新价 | 0.01908 × 77,809.85 = 1,484.47 USDT |

### 初始持仓来源

当前工作区显示的持仓数据来自以下几个来源：

#### 来源 1: 历史状态文件恢复 (最常见) 📄

系统启动时，通过 `_load_state()` 方法从本地 JSON 文件恢复持仓：

**状态文件位置**:
- Windows: `%APPDATA%/ai-quant-trader/trader_state.json`
- Linux/Mac: `~/.config/ai-quant-trader/trader_state.json`

**状态文件结构**:
```json
{
  "positions": {
    "BTC/USDT": 0.01908,
    "ETH/USDT": 0.86272,
    "SOL/USDT": 15.10028
  },
  "entry_prices": {
    "BTC/USDT": 77560.54,
    "ETH/USDT": 2314.10,
    "SOL/USDT": 86.30
  },
  "current_equity": 10000.0,
  "latest_prices": { ... },
  "paper_cash": 3000.0,
  "risk_peak_equity": 12000.0,
  "risk_daily_start_equity": 10000.0,
  "risk_consecutive_losses": 0
}
```

**代码位置**: `apps/trader/main.py` L4884-4960

**恢复逻辑**:
```python
def _load_state(self) -> None:
    # 1. 读取 JSON 文件
    state = json.loads(state_path.read_text())
    
    # 2. 恢复本地状态
    self._positions = {sym: Decimal(qty) for sym, qty in state["positions"].items()}
    self._entry_prices = state["entry_prices"]
    
    # 3. 恢复 Paper 模式的网关状态
    if self.mode == "paper":
        self.gateway.set_paper_positions(self._positions)
        self.gateway.set_paper_cash(state["paper_cash"])
```

#### 来源 2: 实时成交更新 📈

当有新的成交时，系统会自动更新持仓：

**更新点**: `apps/trader/main.py` L4428-4470 (处理 FillResult)

```python
# 成交时的持仓更新逻辑:
if rec.side == "buy":
    # 买入增加持仓
    new_qty = old_qty + fill_qty
    new_entry_price = (old_qty × old_entry + fill_qty × fill_price) / new_qty
    
elif rec.side == "sell":
    # 卖出减少持仓
    new_qty = old_qty - fill_qty
    # 已平仓部分产生的已实现盈亏
    realized_pnl = (fill_price - entry_price) × fill_qty - fee
```

#### 来源 3: 首次启动 (没有历史文件) 🆕

如果没有历史状态文件，系统从空白状态启动：
- `_positions = {}`
- `_entry_prices = {}`
- Paper 现金 = 5000 USDT
- 无初始持仓

### 未实现盈亏计算

#### 计算公式

```
未实现盈亏 (P&L) = (最新价 - 开仓价) × 数量
```

#### 逐行详解

| 标的 | 数量 | 开仓价 | 最新价 | 差价 | 未实现盈亏 |
|------|------|--------|--------|------|-----------|
| BTC/USDT | 0.01908 | 77,560.54 | 77,809.85 | 249.31 | 4.76 USDT |
| ETH/USDT | 0.86272 | 2,314.10 | 2,319.19 | 5.09 | 4.39 USDT |
| SOL/USDT | 15.10028 | 86.30 | 86.44 | 0.14 | 2.24 USDT |

#### 现实意义

- **正值**: 当前持仓盈利（账面浮盈）
- **负值**: 当前持仓亏损（账面浮亏）
- **何时实现**: 当卖出持仓时，浮动盈亏转化为已实现盈亏

#### 关键认识 💡

**问题**: "在没有产生交易的前提下，又是如何产生未实现盈亏的？"

**答案**: 
- 这些持仓是从**历史状态文件恢复**的，不是由当前会话产生
- 当前会话启动后，这些持仓已经存在，随着市场价格变化产生盈亏
- 假设上一次关闭时持仓成本是 77,560.54 USDT/BTC
- 现在价格涨到 77,809.85 USDT/BTC
- 系统就自动计算出 +4.76 USDT 的账面盈利

#### 代码位置

| 文件 | 行号 | 功能 |
|------|------|------|
| `apps/api/server.py` | L303-330 | `_summarize_positions()` 计算函数 |
| `apps/api/server.py` | L313 | 盈亏计算公式 |

---

## 审计日志机制

### 工作原理

#### 核心审计日志函数

**函数签名**:
```python
def audit_log(event_type: str, **kwargs: object) -> None:
    """
    写入强制审计日志（CRITICAL 级别）。
    
    所有影响资金状态的事件必须通过此函数记录，包括：
    - 下单/撤单/成交确认
    - 风控拦截
    - 熔断触发与恢复
    """
```

**代码位置**: `core/logger.py` L198-205

#### 日志输出流程

```
audit_log("ORDER_SUBMITTED", order_id=123, symbol="BTC/USDT", ...)
    ↓
Python logger 处理 (CRITICAL 级别)
    ↓
添加时间戳、模块名等上下文
    ↓
输出到多个目标:
    ├─ Console (实时显示)
    ├─ File: logs/openalgo_YYYY-MM-DD.log (按日期轮转)
    └─ WebSocket: /api/v2/ws/audit (实时推送到前端)
```

### 审计频率

审计日志**不是定期轮询**，而是**事件驱动型**的：

| 事件 | 审计点 | 频率 |
|------|--------|------|
| 系统启动 | SYSTEM_STARTUP | 1 次/启动 |
| 订单提交 | ORDER_SUBMITTED | 每有订单提交 |
| 订单成交 | ORDER_FILLED / PAPER_FILL | 每有成交 |
| 订单拒绝 | ORDER_REJECTED | 每次拒绝 |
| 风控拦截 | RISK_BLOCK | 每次被拦截 |
| 熔断触发 | CIRCUIT_BROKEN | 每次触发 |
| 熔断恢复 | CIRCUIT_RECOVERED | 每次恢复 |

**特点**: 没有固定周期，而是按**业务事件**记录

### 日志类型

#### 1. 订单相关日志

**ORDER_SUBMITTED** (订单提交):
```
[13:15:23] AUDIT | event=ORDER_SUBMITTED | order_id=paper_abc123 | symbol=BTC/USDT | side=buy | order_type=market | quantity=0.1 | price=None
```

**PAPER_FILL** (Paper 模式成交):
```
[13:15:25] AUDIT | event=PAPER_FILL | order_id=paper_abc123 | symbol=BTC/USDT | side=buy | order_type=market | quantity=0.1 | fill_price=50050.5 | fee=50.05 | notional=5005.05 | cash_after=4994.95
```

**ORDER_FILLED** (Live 模式成交):
```
[13:15:26] AUDIT | event=ORDER_FILLED | local_id=local_123 | exchange_id=12345678 | symbol=BTC/USDT | side=buy | filled_qty=0.1 | avg_price=50050.5
```

**ORDER_REJECTED** (订单拒绝):
```
[13:15:27] AUDIT | event=ORDER_REJECTED | mode=paper | order_id=paper_def456 | symbol=ETH/USDT | side=buy | reason=insufficient_funds | need=5005.05 | have=4994.95
```

#### 2. 风控相关日志

**RISK_BLOCK** (风控拦截):
```
[13:20:00] AUDIT | event=RISK_BLOCK | order_id=paper_ghi789 | symbol=SOL/USDT | reason=position_limit_exceeded | current_position_pct=0.45 | max_position_pct=0.40
```

**CIRCUIT_BROKEN** (熔断触发):
```
[13:25:00] AUDIT | event=CIRCUIT_BROKEN | reason=daily_loss_exceeded | daily_pnl=-150.0 | max_loss_pct=0.05 | current_loss_pct=0.075
```

**CIRCUIT_RECOVERED** (熔断恢复):
```
[13:26:00] AUDIT | event=CIRCUIT_RECOVERED | reason=daily_reset
```

#### 3. 系统相关日志

**SYSTEM_STARTUP** (系统启动):
```
[13:10:00] AUDIT | event=SYSTEM_STARTUP | mode=paper | exchange=htx | symbols=['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
```

### 审计日志在工作区中的呈现

工作区显示的"审计日志流"部分：

```
WS/LOGS 已连接
审计日志流
[13:10:02] system | Successfully connected to live audit stream.
[13:15:23] ORDER_SUBMITTED | order_id=paper_abc123 ...
[13:15:25] PAPER_FILL | order_id=paper_abc123 ...
[13:20:00] RISK_BLOCK | order_id=paper_ghi789 ...
```

**工作原理**:
1. 前端打开 WebSocket 连接到 `/api/v2/ws/audit`
2. 后端实时推送新的审计事件
3. 前端按时间倒序显示最新事件

**代码位置**: 
- 后端: `apps/api/server.py` (WebSocket 推送逻辑)
- 前端: `apps/desktop-client/src/pages/ExecutionPage.tsx` (日志显示)

---

## 深度分析：为什么当前没有成交？

基于以上分析，现在可以诊断"最近成交 (0)"的根本原因：

### 诊断清单

#### ✅ 步骤 1: 检查是否有订单被提交

```bash
# 查看最近成交和未完成订单
curl http://localhost:8000/api/v2/dashboard/execution | jq '{
  open_orders: .open_orders | length,
  recent_fills: .recent_fills | length
}'

# 如果两者都是 0，说明根本没有订单被提交
```

**可能原因**:
- 策略未生成交易信号
- 检查 AlphaBrain 工作区

#### ✅ 步骤 2: 检查是否有交易信号生成

```bash
# 查看 Alpha Brain 是否有信号
curl http://localhost:8000/api/v2/dashboard/alpha-brain | jq '.adapter_signals | length'
```

**如果返回 0**:
- 策略可能未启用
- 数据融合工作区的数据源可能不健康
- 检查配置文件 `configs/system.yaml`

#### ✅ 步骤 3: 检查风控是否拦截了订单

```bash
# 查看风控状态
curl http://localhost:8000/api/v2/dashboard/risk-matrix | jq '{
  circuit_broken: .circuit_broken,
  circuit_reason: .circuit_reason,
  daily_pnl: .daily_pnl,
  consecutive_losses: .consecutive_losses
}'
```

**如果 circuit_broken = true**:
- 电路断路器已触发
- 需要点击"重置熔断"按钮
- 或等待日交易结束后自动重置

#### ✅ 步骤 4: 检查行情数据是否正常

```bash
# 查看最新价格和更新时间
curl http://localhost:8000/api/v2/dashboard/data-fusion | jq '.latest_prices'

# 输出格式
{
  "BTC/USDT": {
    "price": 78103.71,
    "updated_at": "2026-04-26T08:01:52.123Z",
    "age_sec": 5.2
  }
}
```

**如果 age_sec > 60**:
- 行情数据已过期
- 可能的原因: 网络故障、数据源中断
- 检查"数据融合"工作区的各项源头状态

#### ✅ 步骤 5: 检查 Paper 模式现金状况

```bash
# 查看 Paper 模式现金
curl http://localhost:8000/api/v2/dashboard/execution | jq '.paper_summary'
```

**如果 paper_cash < 100**:
- 余额极少，大部分已被冻结在持仓中
- 无法提交新的买入订单

---

## 常见问题解答 (FAQ)

### Q1: 为什么显示有持仓但没有交易记录？

**A**: 这些持仓是从上一个会话的状态文件恢复的。系统启动时自动加载 `trader_state.json`，所以即使当前会话没有交易，仍然会显示历史持仓。

### Q2: 开仓价是怎么计算的？

**A**: 这是加权平均开仓价。如果分两次买入，第一次 0.01 BTC @ 50,000 USDT，第二次 0.01 BTC @ 51,000 USDT，则开仓价 = (0.01×50,000 + 0.01×51,000) / (0.01 + 0.01) = 50,500 USDT。

### Q3: 未实现盈亏会立即显示吗？

**A**: 是的，只要行情数据更新（通过 `update_paper_price()`），未实现盈亏会立即计算并显示。

### Q4: 审计日志可以导出吗？

**A**: 可以。所有审计日志都写入到 `logs/openalgo_YYYY-MM-DD.log` 文件，可以直接读取或通过 API 获取。

### Q5: Paper 模式的滑点和手续费可以修改吗？

**A**: 当前硬编码为 10 bps (0.1%)。要修改，需要编辑：
- `modules/execution/gateway.py` L111-112
- 重新启动系统后生效

### Q6: 为什么卖出的成交价比市价低？

**A**: 这是滑点的正常表现。Paper 模式模拟真实市场的流动性成本：
- 买入: 市价 × 1.001 (多付)
- 卖出: 市价 × 0.999 (少收)

---

## 代码位置参考汇总

| 功能 | 文件 | 行号 | 说明 |
|------|------|------|------|
| Paper 模式成交 | `modules/execution/gateway.py` | L323-460 | `_paper_submit()` 方法 |
| 滑点和手续费 | `modules/execution/gateway.py` | L111-112 | 参数初始化 |
| 订单轮询成交 | `modules/execution/order_manager.py` | L203-260 | `poll_fills()` 方法 |
| 持仓汇总计算 | `apps/api/server.py` | L303-330 | `_summarize_positions()` 函数 |
| 执行快照构建 | `apps/api/server.py` | L984-1020 | `_build_execution_snapshot()` 函数 |
| 审计日志函数 | `core/logger.py` | L198-205 | `audit_log()` 函数 |
| 状态恢复 | `apps/trader/main.py` | L4884-4960 | `_load_state()` 方法 |
| 成交处理 | `apps/trader/main.py` | L4428-4494 | 成交后的持仓和盈亏更新 |

---

## 总结

### 关键要点

1. **slippage_bps = 10** 代表 0.1% 的市价偏离，模拟真实流动性成本
2. **fee_bps = 10** 代表 0.1% 的交易手续费
3. **Paper 模式的自动成交** 条件：有行情数据、资金充足、风控通过
4. **当前持仓** 来自状态文件恢复，不是由当前会话产生
5. **未实现盈亏** = (现价 - 开仓价) × 数量，实时计算
6. **审计日志** 是事件驱动的，记录所有影响资金的操作
7. **最近没有成交** 的根本原因：
   - 策略没有生成交易信号，或
   - 风控拦截了订单，或
   - 其他风控约束条件未满足

### 下一步建议

如果想在 Paper 模式下看到实时成交：

1. ✅ 确保"数据融合"工作区所有源头健康（age_sec < 60s）
2. ✅ 检查"风险矩阵"工作区，确保未触发熔断
3. ✅ 查看"Alpha Brain"工作区，确保有交易信号生成
4. ✅ 检查 Paper 模式现金余额，确保不为负数
5. ✅ 手动触发"触发熔断测试"，验证成交机制正常

---

**文档版本**: 1.0  
**最后更新**: 2026-04-26 15:30 UTC  
**审核状态**: ✅ 已验证
