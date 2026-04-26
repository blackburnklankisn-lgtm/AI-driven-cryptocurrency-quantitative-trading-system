# Risk Matrix E2E Validation (2026-04-26)

## 验证目标
验证以下链路是否真实打通：
1. 连续亏损计数显示
2. 熔断触发
3. 风险事件时间线
4. 预算字段显示
5. 熔断重置后的状态恢复

## 运行环境说明
- API 实际可访问地址：`http://localhost:8000`
- 在当前 Windows 环境下，`http://127.0.0.1:8000` 被拒绝，但 `localhost` 和 `::1` 正常
- 这说明当前 Uvicorn 绑定方式更偏向 IPv6/localhost 回环解析

## 验证过程

### 1. 基线验证
调用：
- `/api/v2/dashboard/risk-matrix`
- `/api/v2/risk/events`
- `/api/v2/dashboard/execution`

结果：
- `circuit_broken = false`
- `consecutive_losses = 0`
- `budget_remaining_pct = 0`
- `position_sizing_mode = dynamic`
- `kill_switch_active = false`
- `risk_events_count = 0`
- `paper_mode = paper`

结论：
- 风险矩阵接口已返回修复后的字段
- `position_sizing_mode` 不再是 `unknown`
- DCA / Exit 配置不再是空对象

### 2. 连续亏损 8 次显示验证
方法：
- 对 `storage/trader_state.json` 做受控注入，将 `risk_consecutive_losses` 设为 `8`
- 重启后端，让风险状态从持久化文件恢复

恢复后实时结果：
- `consecutive_losses = 8`
- `circuit_broken = false`
- `risk_events_count = 0`

结论：
- “连续亏损 8 次”可以独立显示
- 仅恢复 `consecutive_losses=8` 不会自动触发熔断
- 这证明“连续亏损计数”与“当前熔断状态”是两个不同维度

### 3. 熔断触发与时间线验证
方法：
- 调用 `/api/v1/control`，发送 `{"action":"trigger_circuit_test"}`

返回：
- `{"result":"ok","message":"Circuit breaker triggered for testing"}`

触发后读取风险矩阵：
- `circuit_broken = true`
- `circuit_reason = 手动测试触发熔断 [trigger_circuit_test]`
- `cooldown_sec = 3599`
- `consecutive_losses = 8`

触发后读取风险事件：
- `risk_events_count = 1`
- 第一个事件包含：
  - `event_type = circuit_breaker`
  - `reason = 手动测试触发熔断 [trigger_circuit_test]`
  - `details.consecutive_losses = 8`

结论：
- 熔断状态、原因文本、冷却倒计时、时间线事件已形成完整闭环
- 前端风险事件时间线在当前修复后可以正确显示事件

### 4. 预算字段验证
读取 `storage/risk_runtime_state.json`：
- `budget.deployed_pct = 0.9951571231350016`

BudgetChecker 逻辑：
- 默认总预算上限 `max_budget_usage_pct = 0.90`
- 剩余预算 = `max(0.90 - deployed_pct, 0)`

按当前状态计算：
- `0.90 - 0.995157... < 0`
- 因此 `budget_remaining_pct = 0`

结论：
- 当前“预算剩余显示 0”是正确的，不是显示错误
- 它表达的是“预算风控可再部署比例为 0”，不是“paper 账户现金为 0”
- paper 现金与预算是两个不同概念

### 5. 熔断重置验证
方法：
- 调用 `/api/v1/control`，发送 `{"action":"reset_circuit"}`

最初发现的问题：
- 重置后 `circuit_broken = false`
- 但 `circuit_cooldown_remaining_sec` 仍然大于 0

根因：
- `reset_circuit_breaker()` 没有清除 `circuit_broken_at`

已修复：
- [modules/risk/manager.py](modules/risk/manager.py#L272)

修复后复验结果：
- 触发后：`trigger_cooldown = 3599`
- 重置后：
  - `reset_circuit = false`
  - `reset_cooldown = 0`
  - `reset_reason = ''`
  - `reset_consecutive_losses = 0`

结论：
- 熔断重置后的冷却显示 bug 已修复

## 本轮验证确认通过的点
1. 连续亏损计数能正确显示
2. 熔断触发能正确反映到风险矩阵
3. 风险事件时间线能正确显示当前熔断事件
4. 预算剩余字段能正确返回，不再是错误的 `暂无`
5. 仓位模式能正确返回 `dynamic`
6. 熔断重置后冷却秒数已正确归零

## 本轮验证发现但未扩展实现的限制
1. 当前 `/api/v2/risk/events` 仍然是“当前状态派生事件”，不是持久化历史事件流
2. 因此重置熔断后，`risk_events_count` 会回到 `0`
3. 这意味着它更像“当前活动风险事件”，而不是“完整历史时间线”

## 清理说明
- 已将受控注入使用的 `storage/trader_state.json` 恢复为备份版本
- 本轮验证未保留额外的测试污染状态
