# 风险矩阵工作区测试与验证 Review（2026-04-26）

## 一、结论先行
本次问题不是单一原因，而是“真实风控状态 + 仪表盘映射缺陷 + 部分高级组件未完全对齐展示”叠加造成的。

已确认并修复两类关键显示问题：
1. 风险事件时间线“空白”与上方熔断状态不一致。
2. 预算剩余/仓位模式/高级风控组件的部分字段取值路径错误导致“暂无/未知/unavailable”。

相关修复：
- [apps/api/server.py](apps/api/server.py#L722)
- [apps/desktop-client/src/services/api.ts](apps/desktop-client/src/services/api.ts#L273)

---

## 二、按严重级别的发现（Findings）

### [高] 风险事件接口与前端类型不匹配，导致时间线经常显示“暂无”
- 后端返回 envelope：`{ generated_at, items: [...] }`，见 [apps/api/server.py](apps/api/server.py#L1102)
- 前端原先把 `/api/v2/risk/events` 当成 `RiskEvent[]` 直接用，见 [apps/desktop-client/src/services/api.ts](apps/desktop-client/src/services/api.ts#L273)
- 风险页在 `riskEvents.length > 0` 时才渲染时间线，见 [apps/desktop-client/src/pages/RiskMatrixPage.tsx](apps/desktop-client/src/pages/RiskMatrixPage.tsx#L94)

影响：即使后端有熔断原因，时间线也可能因为解析错误而显示“暂无风险事件记录”。

本次修复：
- 后端统一事件字段为 `event_id/timestamp/event_type/reason/details`，见 [apps/api/server.py](apps/api/server.py#L1108)
- 前端 `getRiskEvents` 改为解析 envelope 并兼容老字段，见 [apps/desktop-client/src/services/api.ts](apps/desktop-client/src/services/api.ts#L274)

### [高] 预算剩余字段读取了不存在属性，导致 UI 长期显示“暂无”
- BudgetChecker 对外是 `remaining_budget_pct`，见 [modules/risk/budget_checker.py](modules/risk/budget_checker.py#L382)
- 风险快照原逻辑读取 `budget_remaining_pct`（对象上并不存在）

影响：预算卡片和预算条会显示“暂无”，但并不代表没预算系统。

本次修复：
- 风险快照改为读取 `remaining_budget_pct`，并回退 `snapshot().remaining_budget_pct`，见 [apps/api/server.py](apps/api/server.py#L744)

### [中] 仓位模式“未知”是字段路径设计不一致，不是风控没工作
- 风险页展示 `position_sizing_mode`，见 [apps/desktop-client/src/pages/RiskMatrixPage.tsx](apps/desktop-client/src/pages/RiskMatrixPage.tsx#L61)
- 旧逻辑从 `position_sizer.config.method` 读取，但 `PositionSizer` 没有 `config.method`，见 [modules/risk/position_sizer.py](modules/risk/position_sizer.py#L43)

影响：固定显示 unknown/未知。

本次修复：
- 后端对该字段给出可解释默认值 `dynamic`，见 [apps/api/server.py](apps/api/server.py#L776)

### [中] “熔断开关/冷却”显示 unavailable，属于对象来源错位
- 旧逻辑读取 `trader._cooldown_manager`、`trader._dca_engine`、`trader._exit_planner`，但主流程实际集中在 `AdaptiveRiskMatrix` 内部对象：
  - `self._exit_planner` [modules/risk/adaptive_matrix.py](modules/risk/adaptive_matrix.py#L108)
  - `self._dca_engine` [modules/risk/adaptive_matrix.py](modules/risk/adaptive_matrix.py#L109)
  - `self._cooldown` [modules/risk/adaptive_matrix.py](modules/risk/adaptive_matrix.py#L110)
- KillSwitch 诊断接口是 `health_snapshot()`，不是 `diagnostics()`，见 [modules/risk/kill_switch.py](modules/risk/kill_switch.py#L471)

本次修复：
- 风险快照优先对齐正确接口与对象来源，见 [apps/api/server.py](apps/api/server.py#L752)

---

## 三、逐条回答你的问题

### 1) 为什么会显示“连续亏损 8 次”？我没在仪表盘看到交易记录，数据准不准？
结论：`consecutive_losses` 来自风控引擎的成交结果统计，不来自“总览页面可见的交易列表”。

数据来源链路：
1. 每次卖出成交后，系统会计算本笔 `pnl`，并调用 `risk_manager.record_trade_outcome(won=(pnl>0))`，见 [apps/trader/main.py](apps/trader/main.py#L4462)
2. 在 RiskManager 内：盈利就清零，亏损就 `+1`，达到阈值触发熔断，见 [modules/risk/manager.py](modules/risk/manager.py#L196)
3. 风险页显示的是 `risk_manager.get_state_summary()` 的 `consecutive_losses`，见 [modules/risk/manager.py](modules/risk/manager.py#L293) 和 [apps/api/server.py](apps/api/server.py#L783)

为什么你可能“看不到交易记录”但这里有连亏数：
- 风险计数是引擎状态，交易时间线是另一个视图。
- 交易列表常常只看当前进程内 recent fills，而连续亏损可以跨会话恢复（状态文件持久化后加载），见 [apps/trader/main.py](apps/trader/main.py#L4860) 与 [apps/trader/main.py](apps/trader/main.py#L4882)

准确性判断：
- 逻辑上是可信的（按成交盈亏严格更新）。
- 但“可观测性”在修复前有缺陷：事件时间线未正确对齐，导致你看到“有状态、无事件”的割裂体验。

### 2) 为什么“仓位模式”显示未知？paper 有初始 $5000，为什么不显示？
这是两个概念：
1. 仓位模式：是下单数量方法标签，不是资金余额。
2. $5000：是 paper 账户现金初始化。

关于仓位模式“未知”：
- 原因是读取了不存在的字段路径（`position_sizer.config.method`）。
- 已修复为 `dynamic`（动态仓位）。

关于 $5000：
- paper 初始现金确实存在：见 [modules/execution/gateway.py](modules/execution/gateway.py#L107)
- 资金会随成交变化，并通过状态恢复，不一定一直是 5000。
- “预算剩余”并不是“现金余额”，而是预算控制层（BudgetChecker）的可用比例。

### 3) 为什么 paper 模式“预算使用/预算剩余”显示暂无？为什么不用虚拟资金交易？
根因：预算字段映射错误，不是没在用虚拟资金。

- BudgetChecker 已实现并在下单/平仓时更新，见 [apps/trader/main.py](apps/trader/main.py#L2011) 与 [apps/trader/main.py](apps/trader/main.py#L4465)
- 风险页显示“暂无”是因为字段名错读（已修复）。

“虚拟 $5000 是否在用”：
- 在用。paper 网关会按买卖更新 `paper_cash` 与持仓，见 [modules/execution/gateway.py](modules/execution/gateway.py#L313)

### 4) 为什么“熔断开关/冷却”显示 unavailable？
- 熔断开关：原先调错了接口（用 diagnostics，实际是 health_snapshot）。
- 冷却：原先从 trader 直接取 `_cooldown_manager`，而主流程在 AdaptiveRiskMatrix 内部 `_cooldown`。

这两个已在本次修复对齐。

### 5) 为什么 DCA 计划和退出计划是空？DCA/退出计划是什么？作用与原理？
是否实现：
- 已实现算法模块，但页面之前拿错对象导致显示空。
  - DCA：见 [modules/risk/dca_engine.py](modules/risk/dca_engine.py#L22)
  - 退出计划：见 [modules/risk/exit_planner.py](modules/risk/exit_planner.py#L30)

DCA 计划是什么：
- 在满足 regime/置信度/预算条件时，生成分层加仓价差（如 `[-0.02, -0.04]`）。
- 作用：避免一次性重仓，分层摊低成本，同时受预算上限保护。
- 核心原理：
  - 先做条件门控（regime、confidence、budget）
  - 再按预算弹性推导层数
  - 输出每层触发偏移

退出计划是什么：
- 入场后给出止损、追踪止盈、阶梯止盈参数。
- 作用：把“什么时候止损/止盈”标准化，减少主观干预。
- 核心原理：
  - ATR 或基准止损
  - 再按 regime 和信号置信度缩放
  - 输出 stop/trailing/ROI ladder

### 6) 风险状态参数含义（你截图里的 JSON）
- `circuit_broken`: 当前是否熔断（true 表示买入被阻断，系统进入保护状态）
- `circuit_reason`: 熔断触发原因文本（例如连亏触发、回撤触发）
- `daily_pnl`: 相对 `daily_start_equity` 的当日盈亏
- `consecutive_losses`: 连续亏损计数（每个亏损平仓 +1，盈利平仓清零）
- `peak_equity`: 用于回撤计算的峰值净值
- `daily_start_equity`: 当日基准净值

这些字段由 [modules/risk/manager.py](modules/risk/manager.py#L293) 统一输出。

### 7) 为什么上面说触发熔断，下面“风险事件时间线”却说暂无？
根因是接口/前端字段协议不一致，不是风控没触发。

- 状态区：读的是 risk snapshot（有 circuit_reason）
- 时间线：读 `/api/v2/risk/events`，但前端解析方式与返回结构不匹配

本次已修复后，二者会保持一致。

---

## 四、当前可解决性评估
可以解决，且已完成第一批关键修复（映射和协议层）。

剩余建议（可选下一步）：
1. 将风险事件改为持久化事件流（不只显示当前 circuit_reason 的派生事件）。
2. 在风险页增加“预算占用细分（deployed/dca/intraday）”卡片，减少“预算 vs 现金”概念混淆。
3. 执行一次端到端验收：触发连亏 -> 熔断 -> 时间线出现事件 -> 冷却倒计时变化 -> reset 后恢复。

---

## 五、本次已改动文件
- [apps/api/server.py](apps/api/server.py)
- [apps/desktop-client/src/services/api.ts](apps/desktop-client/src/services/api.ts)

