# Evolution 系统 FAQ、故障排查、优化与实战案例

本文为 Evolution 自适应演进系统的运维手册，包含常见问题解答、故障排查流程、性能优化建议和实战案例分析。

---

## 第一部分：常见问题 (FAQ)

### Q1: 为什么我的候选项一直卡在 "candidate" 状态，不往上晋升？

**答**：`candidate → shadow → paper → active` 每一步都有门禁阈值。最常见的原因：

1. **Sharpe 不够高**
   - 晋升到 shadow 需要 Sharpe >= 0.8（默认值）
   - 检查: `storage/phase3_evolution/candidates.json` 里该候选的 `sharpe_30d` 字段
   - 解决: 等候更多交易数据，让 Sharpe 天然提升，或手动调整 PromotionGateConfig

2. **样本量不足**
   - shadow → paper 要求至少 30 天的数据
   - 新注册的候选需要时间积累指标
   - 检查: 后端日志 `[Promotion]` 标签，看是否有 `INSUFFICIENT_SAMPLES` 消息

3. **回撤太大**
   - shadow 门禁: max_drawdown <= 0.07 (7%)
   - 如果候选有突然的大亏损，会卡在 candidate
   - 检查: `max_drawdown_30d` 字段

4. **手动卡住**
   - 可能你之前手动把它降级到了 paused，忘了
   - 检查: UI 的 Evolution 页面，看状态是否真的是 candidate，还是 paused

**快速修复**:
```python
# 方式1: 强制晋升（需要有充分理由）
POST /api/v1/control
{
    "action": "promote_evolution",
    "candidate_id": "model_ml/rf_20260425",
    "reason": "Manual promote for testing"
}

# 方式2: 查看详细原因
# 读取后端日志 logs/openalgo_2026-04-XX.log
# 搜索 candidate_id 的相关记录
```

---

### Q2: A/B 测试跑了很久，为什么还没完成？

**答**：A/B 需要达到 `min_samples`（默认 100）才能评估。常见延迟原因：

1. **交易频率太低**
   - 如果你的策略一天只交易几次，100 个样本需要 20+ 天
   - 检查: `storage/phase3_evolution/` 下 AB 实验的当前样本量
   - 解决: 降低 `min_samples` 或增加策略交易频率

2. **A/B 卡在等待某一侧**
   - control 侧和 test 侧都要达到 100，不能只有一侧多
   - 检查: `apps/api/server.py` 的 `get_evolution_snapshot()` 返回的 A/B 诊断
   - 解决: 确认 test 候选真的在交易（不是被风控卡住了）

3. **风控阻断了 test 候选**
   - 如果 test 版本表现太差，风险系统可能自动降级它
   - 检查: 后端日志 `[Risk]` 或 `[Circuit]` 标签
   - 解决: 手动回滚或修改测试参数

4. **实验被手动中断了**
   - 你或者某个定时任务可能调用了 `close_experiment()`
   - 检查: `decisions.jsonl` 里是否有相关 close 记录

**快速诊断**:
```bash
# 查看当前 AB 实验状态
curl -X GET http://localhost:8000/api/v1/evolution/snapshot \
  -H "X-API-KEY: your_key"

# 查看 ab_experiments 部分的 active 列表
# 确认实验 ID 和样本量
```

---

### Q3: 候选的指标（Sharpe, Drawdown）为什么不更新？

**答**：指标更新取决于 `on_fill()` 回调是否被调用。排查步骤：

1. **检查交易是否真的在执行**
   ```bash
   # 查看后端日志有无 [Fill] 标签的记录
   grep -i "fill\|execution" logs/openalgo_*.log | tail -20
   ```

2. **检查 `update_metrics()` 是否被调用**
   ```python
   # 在 apps/trader/main.py 的 on_fill() 中应该有:
   if self._phase3_evolution:
       self._phase3_evolution.update_metrics(
           candidate_id=current_id,
           step_sharpe=sharpe,
           step_drawdown=dd
       )
   ```
   - 如果没有这行，指标永远不会更新
   - 检查: 代码搜索 `update_metrics` 调用位置

3. **Evolution 引擎没有正确启动**
   - 检查: `_global_trader_instance._phase3_evolution` 是否为 None
   - 命令: `POST /api/v1/control` 调用任何 evolution action，看是否报错

4. **metrics 持久化失败**
   - `CandidateRegistry` 在更新后会调用 `_save()`
   - 如果磁盘满或权限不足，会导致保存失败
   - 检查: 后端日志 `[Evolution]` 标签，搜索 "save failed"

**强制更新指标（调试用）**:
```python
# 直接调用 update_metrics
evolution = trader._phase3_evolution
evolution.update_metrics(
    candidate_id="model_ml/rf_20260425",
    sharpe_30d=1.2,
    max_drawdown_30d=0.052,
    win_rate_30d=0.58
)
```

---

### Q4: 为什么参数优化器没有生成新候选？

**答**：周参数优化器由 cron 表达式 `"0 3 * * 0"` 触发（每周日 03:00 UTC）。检查清单：

1. **Cron 时间没到**
   - 当前时间是否确实在周日 03:00 UTC？
   - 检查服务器时区设置: `timedatectl` (Linux) 或 `Get-Date` (Windows)
   - 如果时区错误，cron 永远不会触发

2. **周优化器被禁用了**
   - 检查 `core/config.py` 或 `.env` 的 `WEEKLY_PARAMS_OPTIMIZER_CRON`
   - 如果是空字符串或注释掉了，就不会运行
   - 修复: 确保 `WEEKLY_PARAMS_OPTIMIZER_CRON="0 3 * * 0"`

3. **优化目标列表为空**
   - 调用 `_collect_phase3_param_optimization_targets()` 返回空列表
   - 检查: UI 的 Evolution 页面，"每周参数优化器" 区域的 "优化目标" 数量
   - 如果是 0，说明没有注册需要优化的策略

4. **优化器运行了但失败了**
   - 检查: `storage/phase3_evolution/weekly_params_optimizer_runs.jsonl`
   - 查看最后一条记录是否有 `"status": "failed"` 或错误信息
   - 常见错误: OHLCV 数据不足、optuna 超时、权限不足

5. **强制触发优化器测试**
   ```bash
   POST /api/v1/control
   {
       "action": "trigger_weekly_optimizer"
   }
   ```
   - 立即触发一次优化，无需等 cron
   - 查看响应和日志看是否成功

---

### Q5: 回滚后运行时对象没有更新，还在用旧版本？

**答**：回滚分两层：状态层和运行时层。故障排查：

1. **状态确实变了，但运行时没同步**
   - 检查: `storage/phase3_evolution/candidates.json` 中的 status 字段
   - 状态变了说明第一层成功了
   - 问题在于运行时对象没有重新加载

2. **检查运行时同步函数**
   ```python
   # 回滚后应该调用:
   trader._apply_evolution_runtime_state(rollback_report)
   
   # 如果这行代码没有执行或出错，运行时不会更新
   # 检查后端日志 [Phase3/EV] 标签
   ```

3. **运行时对象缓存了旧版本**
   - ML 模型、策略参数可能被缓存在内存里
   - 解决: 手动清除缓存，或重启应用
   - 命令:
   ```bash
   # 重启后端服务（会清除所有内存缓存）
   POST /api/v1/control
   {
       "action": "stop"
   }
   # 然后重新启动后端
   ```

4. **回滚目标本身有问题**
   - 确认 `rollback_to_candidate_id` 对应的文件确实存在
   - 检查: `runtime/ml_params/` 或 `models/` 目录
   - 如果文件丢失，即使状态回滚也运行不了

**验证回滚是否成功**:
```bash
# 方法1: 检查日志
grep "ROLLBACK" logs/openalgo_*.log | tail -5

# 方法2: 查看当前 active
curl http://localhost:8000/api/v1/evolution/snapshot | jq '.active_candidates'

# 方法3: 查看指标（应该变回旧版本的指标）
curl http://localhost:8000/api/v1/evolution/snapshot | jq '.candidates[] | select(.status=="active")'
```

---

### Q6: 为什么系统自动回滚了我认为很好的版本？

**答**：自动回滚由 RetirementPolicy 触发。常见误会和解决办法：

1. **Sharpe 计算方式误解**
   - 你看 Sharpe=1.2 觉得很好，但后端计算的是 30 天 rolling
   - 如果最后 5 天表现特别差，rolling Sharpe 会急剧下降
   - 检查: 审计日志的 sharpe_30d 和 max_drawdown_30d 具体值

2. **阈值太严格**
   - 自动回滚触发条件：Sharpe < 0 for 60 days（默认）
   - 这个标准对某些策略太严
   - 修复: 调整 `RetirementConfig` 的阈值参数
   ```python
   retirement: RetirementConfig(
       low_sharpe_threshold=0.3,      # 改成 0.3 而非 0.5
       low_sharpe_duration_days=30,   # 改成 30 而非 60
   )
   ```

3. **风险违规自动触发了回滚**
   - 条件：`risk_violations >= 5`
   - 检查: 后端日志 `[Risk]` 标签，看有多少次违规
   - 解决: 调整风控参数或检查市场异常

4. **人工不想让它自动回滚**
   - 禁用自动回滚：修改 config 的 `auto_rollback_on_retire=False`
   - 这样达到退役条件时只会进入 paused，不会自动回滚
   - 然后由人工决定是否手动回滚或保留

---

### Q7: 候选项列表越来越多，怎样清理垃圾候选？

**答**：`candidates.json` 只会追加，永远不会自动删除。管理办法：

1. **标准做法：进入 retired 状态**
   - 不能直接删除，只能标记为 retired
   - UI 操作：选中候选 → [立即退役]
   - API 操作：
   ```bash
   POST /api/v1/control
   {
       "action": "retire_evolution",
       "candidate_id": "model_ml/rf_20260415",
       "reason": "Garbage cleanup"
   }
   ```

2. **查看 retired 候选**
   ```bash
   # 检查有多少 retired
   grep '"status": "retired"' storage/phase3_evolution/candidates.json | wc -l
   ```

3. **离线清理（谨慎操作）**
   - 停止系统
   - 备份 `storage/phase3_evolution/candidates.json`
   - 手动编辑 JSON，删除 retired 条目
   - 重启系统

4. **定期存档**
   - 定期将 `candidates.json` 存档到 `candidates.json.backup_YYYY-MM-DD`
   - 这样历史记录完整，但活跃文件保持精简

---

## 第二部分：故障排查指南

### 流程 1：系统启动时 evolution 不可用

**症状**：
- 后端启动，但 `/api/v1/evolution/snapshot` 返回 `status: unavailable`
- UI 的 Evolution 页面显示灰色，所有数据为空

**排查步骤**：

```
[Step 1] 检查 Evolution 引擎是否初始化
         后端日志搜索: "[Evolution] SelfEvolutionEngine 初始化"
         
         ✓ 找到 → [Step 2]
         ✗ 没找到 → [问题A]

[Step 2] 检查 storage/phase3_evolution/ 目录是否存在
         ls -la storage/phase3_evolution/
         
         ✓ 目录存在 → [Step 3]
         ✗ 目录不存在 → [问题B]

[Step 3] 检查 candidates.json 是否可读
         cat storage/phase3_evolution/candidates.json | head -c 100
         
         ✓ 可读 → [Step 4]
         ✗ 损坏 → [问题C]

[Step 4] 检查是否有权限创建新文件
         touch storage/phase3_evolution/test.txt
         rm storage/phase3_evolution/test.txt
         
         ✓ 成功 → [Step 5]
         ✗ 失败 → [问题D]

[Step 5] 查看后端日志的错误信息
         grep -i "error\|exception" logs/openalgo_*.log | tail -20
         
         ✓ 找到具体错误 → [对应问题]
         ✗ 没有错误日志 → [问题E]
```

**问题诊断**：

| 问题ID | 症状 | 原因 | 解决方案 |
|--------|------|------|---------|
| **A** | 日志无初始化信息 | Evolution 模块没被导入或加载失败 | 检查 apps/trader/main.py 中的 import 语句 |
| **B** | 目录不存在 | config.state_dir 路径错误 | 创建目录: `mkdir -p storage/phase3_evolution` |
| **C** | JSON 损坏 | 前次异常退出或磁盘写入失败 | 恢复备份或手动修复 JSON 格式 |
| **D** | 权限拒绝 | 进程权限不足 | 改变目录权限: `chmod 777 storage/` |
| **E** | 完全无日志 | 日志系统未启动 | 检查 LOG_LEVEL 环境变量，确保不是 CRITICAL 以上 |

---

### 流程 2：候选无法晋升

**症状**：
- 候选一直卡在某个状态
- 日志中有 `PROMOTE` 日志但没有状态变化

**排查步骤**：

```
[Step 1] 获取候选当前状态和指标
         curl http://localhost:8000/api/v1/evolution/snapshot | \
           jq '.candidates[] | select(.candidate_id=="MODEL_ID")'
         
         记录: status, sharpe_30d, max_drawdown_30d, ab_lift

[Step 2] 确认候选是否应该晋升
         对比 PromotionGateConfig 的各阈值:
         
         gate_configs["candidate->shadow"]:
           ✓ sharpe >= 0.8?
           ✓ max_drawdown <= 0.07?
           
         gate_configs["shadow->paper"]:
           ✓ sharpe >= 0.6?
           ✓ days_in_stage >= 30?
           
         gate_configs["paper->active"]:
           ✓ sharpe >= 0.5?
           ✓ ab_lift >= 0?
           ✓ ab_lift 不为 null?

[Step 3] 查看后端日志的晋升决策
         grep "PROMOTE\|HOLD\|reason" logs/openalgo_*.log | \
           grep "MODEL_ID" | tail -10
         
         查看 reason_codes 里是否有:
         - INSUFFICIENT_SAMPLES
         - SHARPE_BELOW_THRESHOLD
         - DRAWDOWN_EXCEEDED
         等

[Step 4] 如果日志无关键信息，检查 decisions.jsonl
         cat storage/phase3_evolution/decisions.jsonl | \
           grep "MODEL_ID" | tail -3
         
         如果是 HOLD，说明各指标都卡在某个阈值

[Step 5] 尝试强制晋升测试
         POST /api/v1/control
         {
             "action": "promote_evolution",
             "candidate_id": "MODEL_ID",
             "reason": "Testing"
         }
         
         ✓ 成功 → 说明引擎没问题，就是指标不足
         ✗ 失败 → 说明引擎有问题，查看错误信息
```

**常见原因及修复**：

| 原因 | 表现 | 修复方案 |
|------|------|---------|
| Sharpe 太低 | 卡在 candidate | 等待更多交易数据或调整策略 |
| 样本量不足 | 卡在 shadow | 等待 30 天或增加交易频率 |
| 无法创建 A/B | 卡在 paper | 确认 control 候选存在且 active |
| A/B 样本量不足 | paper 永不晋升 | 等待或降低 `min_samples` |
| 手动降级了 | status 是 paused | 手动晋升或删除降级记录 |

---

### 流程 3：A/B 测试卡住

**症状**：
- 实验已跑多天，样本量一直停留在某个数字
- 一侧样本多，另一侧样本少

**排查步骤**：

```
[Step 1] 确认 test 候选是否在交易
         搜索日志: "[Fill]" + test_candidate_id
         
         ✓ 有填充日志 → [Step 2]
         ✗ 无填充日志 → [问题1]

[Step 2] 检查 test 候选是否被风控卡住
         搜索日志: "[Risk]" 或 "[Circuit]" + test_candidate_id
         
         ✓ 无风险日志 → [Step 3]
         ✗ 有风险日志 → [问题2]

[Step 3] 确认 record_step() 是否被正确调用
         搜索日志: "AB experiment record" 或 "step_pnl"
         
         ✓ 有记录 → [Step 4]
         ✗ 无记录 → [问题3]

[Step 4] 查看 AB 诊断信息
         curl http://localhost:8000/api/v1/evolution/snapshot | \
           jq '.ab_experiments'
         
         查看 control_samples 和 test_samples 是否都在增长
         
         ✓ 都在增长 → [Step 5]
         ✗ 只有一侧增长 → [问题4]

[Step 5] 检查样本量是否达到
         min_samples 是 100（默认）吗？
         当前的 control_samples 和 test_samples 各是多少？
         
         如果都 >= 100，应该能自动评估
         如果还不够，继续等待或改配置
```

**问题诊断**：

| 问题ID | 原因 | 解决 |
|--------|------|------|
| **1** | test 候选没在交易 | 检查它是否被评为 "不可交易" 或网络问题 |
| **2** | 风控卡住了 test | 手动回滚或调整风控参数 |
| **3** | record_step 没调用 | 检查 on_fill() 中是否实现了这行代码 |
| **4** | 两侧不平衡 | 可能是随机交易分配不公平，重新创建实验 |

---

### 流程 4：自动回滚触发了但不该

**症状**：
- 看到 ROLLBACK 日志，系统切回了旧版本
- 但你认为新版本还是有价值的

**排查步骤**：

```
[Step 1] 查看回滚触发的具体原因
         grep "ROLLBACK" logs/openalgo_*.log -A 5 | head -20
         
         查看 reason_codes: [SHARPE_THRESHOLD], [DRAWDOWN_EXCEEDED], etc.

[Step 2] 验证这些指标是否计算正确
         curl http://localhost:8000/api/v1/evolution/snapshot | \
           jq '.candidates[] | select(.status=="paused") | 
               {candidate_id, sharpe_30d, max_drawdown_30d}'
         
         对比日志中的 reason，看指标是否真的触发了门禁

[Step 3] 决定是否禁用自动回滚
         修改 config:
         
         retirement: RetirementConfig(
             auto_rollback_on_retire=False  # 改成 False
         )
         
         这样以后只会进入 paused，由你手动决定

[Step 4] 如果想重新启用新版本
         POST /api/v1/control
         {
             "action": "promote_evolution",
             "candidate_id": "被回滚的版本ID",
             "reason": "Manual restore"
         }
```

---

## 第三部分：性能优化建议

### 1. 候选评估性能优化

**问题**：评估 200+ 个候选时卡顿

**优化方案**：

```python
# 方案1: 批量异步评估
# 原本: 顺序评估，一个接一个
for candidate in candidates:
    decision = gate.evaluate(candidate)  # 阻塞

# 改进: 并行评估（使用 ThreadPoolExecutor）
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor(max_workers=4) as executor:
    futures = [
        executor.submit(gate.evaluate, candidate)
        for candidate in candidates
    ]
    decisions = [f.result() for f in futures]

# 预期效果: 4 倍加速

# 方案2: 跳过不必要的评估
# 只评估 shadow/paper 的候选，active/paused/retired 不评估
candidates_to_eval = [
    c for c in candidates
    if c.status in (CandidateStatus.SHADOW, CandidateStatus.PAPER)
]

# 预期效果: 减少 80% 的评估量（如果大多数是 active/retired）
```

### 2. A/B 样本数据处理优化

**问题**：A/B 实验累积 1000+ 样本后，evaluate() 变慢

**优化方案**：

```python
# 问题根源: 每次 evaluate 都遍历全部样本重新计算
# _ExperimentState.evaluate():
#   lift = self.test_pnl_sum - self.control_pnl_sum
#   drawdown = (peak - current) / peak  # 需要遍历所有历史

# 方案1: 增量计算（推荐）
# 不在 evaluate() 时计算，而在 record_step() 时实时更新

@dataclass
class _ExperimentState:
    # ... 原字段 ...
    
    # 增加实时统计字段
    control_pnl_sum_cached: float = 0.0  # 缓存值
    test_pnl_sum_cached: float = 0.0
    
    def evaluate(self) -> ABResult:
        # 直接用缓存值，O(1) 时间复杂度
        lift = self.test_pnl_sum_cached - self.control_pnl_sum_cached
        # ...
        
        # 预期效果: evaluate 从 O(n) 降到 O(1)

# 方案2: 降采样
# 如果 sample 数太多，只保留最近 500 个
def record_step(self, is_test, step_pnl):
    # ... 记录新数据 ...
    
    # 超过 500 样本后，丢弃最早的
    if len(self.samples) > 500:
        self.samples = self.samples[-500:]
    
    # 预期效果: 内存占用恒定，计算不变慢
```

### 3. 候选注册表序列化优化

**问题**：candidates.json 达到 10MB+，加载变慢

**优化方案**：

```python
# 问题根源: 所有 retired 候选都存在文件里
# 现在有 2000 个候选，其中 1500 个是 retired，拖累读写

# 方案1: 分离存储
# candidates_active.json: 只存 active/shadow/paper (100 条)
# candidates_retired.json: 存 retired/paused (1900 条)

# 加载时优先加载 active 文件，retired 按需查询

# 方案2: 使用数据库替代 JSON
# 改用 SQLite (已有经验)
from modules.evolution.candidate_registry_db import CandidateRegistry

registry = CandidateRegistry(db_path="storage/phase3_evolution/candidates.db")
# 自动创建表，支持索引，查询更快

# 方案3: 定期归档
# 每月将 retired 候选导出到归档文件
import json
from datetime import datetime, timedelta

month_ago = datetime.now() - timedelta(days=30)
old_retired = [
    c for c in candidates
    if c.status == "retired" and c.retired_at < month_ago
]

with open(f"archive/candidates_{month_ago.year}{month_ago.month:02d}.json", "w") as f:
    json.dump(old_retired, f)

# 从活跃文件删除这些条目
# 预期效果: 活跃文件保持 < 1 MB，加载 < 100ms
```

### 4. 周参数优化器性能优化

**问题**：周优化器跑 6 个策略需要 6 小时，下周一早上赶不上

**优化方案**：

```python
# 问题根源: optuna 做 100 trials，每个 trial 都要回测一遍
# 6 策略 × 100 trials = 600 次回测，耗时 O(n^2)

# 方案1: 减少 trial 次数
result = optimize_params_from_dataframe(
    df_opt,
    trial_count=50,  # 从 100 改成 50
    timeout_sec=300  # 从 600 改成 300
)
# 预期效果: 速度 2 倍加快，精度下降 5%

# 方案2: 使用特征重要性筛选参数
# 不优化所有参数，只优化对结果影响最大的 3 个

from sklearn.ensemble import RandomForestRegressor
feature_importance = model.feature_importances_
top_3_features = np.argsort(feature_importance)[-3:]

# 只在这 3 个参数上搜索
search_space = {
    'param_1': [0.5, 0.8],  # 只要 3 个参数
    'param_2': [0.2, 0.5],
    'param_3': [0.0, 1.0],
}

# 预期效果: 搜索空间 10 倍小，时间也快 10 倍

# 方案3: 并行多个策略
# 原本: 顺序优化 6 个策略，一个接一个
for strategy in strategies:
    optimize_params_from_dataframe(...)  # 顺序

# 改进: 开 6 个线程并行
from concurrent.futures import ThreadPoolExecutor

with ThreadPoolExecutor(max_workers=6) as executor:
    futures = [
        executor.submit(optimize_params_from_dataframe, df)
        for strategy in strategies
    ]
    results = [f.result() for f in futures]

# 预期效果: 时间从 6 小时降到 1 小时（受 CPU 核心数限制）

# 方案4: 预热缓存
# 在周日 02:00 提前拉数据，到 03:00 时直接用（减少网络延迟）

def _warmup_ohlcv_cache():
    """周日 02:00 调用，预加载数据"""
    for symbol in SYMBOLS:
        fetch_ohlcv(symbol, "1h", bar_count=5000)
    log.info("OHLCV cache warmed up")

# Cron: "0 2 * * 0" → _warmup_ohlcv_cache
```

### 5. 运行时状态同步优化

**问题**：回滚后 `_apply_evolution_runtime_state()` 卡 3 秒，订单被延迟

**优化方案**：

```python
# 问题根源: 同步涉及重新加载所有工件（模型、参数、权重）
# ML 模型 load: 500ms
# 策略参数 load: 200ms
# 权重重建: 300ms
# 合计: 1 秒+ 网络延迟

# 方案1: 预加载多个版本
class ModelCache:
    def __init__(self, max_versions=3):
        self.cache = {}  # {model_id: model}
        self.max_versions = max_versions
    
    def get_or_load(self, model_id):
        if model_id in self.cache:
            return self.cache[model_id]  # 秒级返回
        
        model = load_model(model_id)
        self.cache[model_id] = model
        
        # 超过限制时删除最旧的
        if len(self.cache) > self.max_versions:
            oldest_id = min(self.cache.keys(), ...)
            del self.cache[oldest_id]
        
        return model

# 预期效果: 热项 O(1)，冷启动也只 1 次

# 方案2: 后台异步同步
# 不在关键路径上做同步，而是异步完成

async def apply_evolution_state_async(report):
    """后台异步应用，不阻塞订单"""
    await asyncio.sleep(0.1)  # 让出 CPU 给订单处理
    await loop.run_in_executor(
        None,
        sync_apply_evolution_state,
        report
    )
    log.info("Evolution state applied asynchronously")

# 预期效果: 订单不延迟，同步在后台完成

# 方案3: 快速路径优化
# 如果回滚目标是同一层级（如 ML → ML），快速切换
# 如果跨层级（如 ML → RL），走完整同步

if old_version.owner == new_version.owner:
    # 快速路径: 只更新该层级
    swap_runtime_object(old_version, new_version)  # 10ms
else:
    # 完整路径: 重新初始化
    apply_full_evolution_state(report)  # 1 秒
```

---

## 第四部分：实战案例分析

### 案例 1：ML 模型升级的完整演进

**背景**：
- 当前 RF 模型 (model_ml/rf_20260410) Sharpe=0.95
- 新的 RF 模型经过重训（model_ml/rf_20260425），本地测试 Sharpe=1.2
- 目标：上线新模型，验证其在真实交易中的表现

**全流程**：

```
时刻1 (2026-04-20): 新模型完成训练
  └─ 模型文件: models/rf_v20260425.pkl
  └─ 阈值文件: runtime/ml_params/rf_20260425_thresholds.json

时刻2 (2026-04-21 10:00): 后端初始化，自动检测新模型
  └─ 触发 ContinuousLearner.register_new_model_candidate()
  └─ 注册为: model_ml/rf_20260425
  └─ 初始状态: candidate
  └─ 指标: sharpe_30d=null (待积累)

时刻3 (2026-04-21 10:30): 创建 A/B 实验
  └─ control_id: model_ml/rf_20260410 (旧模型，active)
  └─ test_id: model_ml/rf_20260425 (新模型，candidate)
  └─ experiment_id: ab_xxx
  
  └─ 从此开始:
      - 所有订单同时使用两个模型
      - control侧 PnL 记为 pnl_old
      - test侧 PnL 记为 pnl_new

时刻4 (2026-04-21 ~ 2026-04-28): A/B 运行期间
  └─ 每个订单都喂入 step_pnl 数据
  
  └─ control侧: 
      | Day | samples | pnl | sharpe |
      | 1   | 12      | 50  | n/a    |
      | 2   | 25      | 120 | n/a    |
      | ...
      | 7   | 105     | 1250| 0.95   |
  
  └─ test侧:
      | Day | samples | pnl | sharpe |
      | 1   | 11      | 62  | n/a    |
      | 2   | 24      | 135 | n/a    |
      | ...
      | 7   | 105     | 1380| 1.2    |
  
  └─ 同时，后端也在积累 sharpe_30d 指标:
      model_ml/rf_20260425.sharpe_30d → 1.1 → 1.15 → 1.2

时刻5 (2026-04-28 14:00): A/B 评估
  └─ control: pnl=1250, samples=105, dd=4.5%
  └─ test: pnl=1380, samples=105, dd=3.2%
  
  └─ 门禁检查:
      ✓ lift = 1380 - 1250 = 130 >= 0
      ✓ drawdown_diff = 3.2% - 4.5% = -1.3% <= 2%
      ✓ samples 都 >= 100
  
  └─ 结论: PASSES ✓
  └─ ab_lift 写入: model_ml/rf_20260425.ab_lift = 130

时刻6 (2026-04-28 14:05): 自动晋升
  └─ gate.evaluate(model_ml/rf_20260425):
      ✓ sharpe=1.2 >= 0.5 (paper->active 门禁)
      ✓ ab_lift=130 >= 0
      ✓ ab_lift 不是 null
  
  └─ 决策: PROMOTE
  └─ 状态变更: paper → active
  └─ 同时，旧模型状态: active → paused
  └─ 记录: decisions.jsonl 新增一条 PROMOTE 记录

时刻7 (2026-04-28 14:06): 运行时同步
  └─ _apply_evolution_runtime_state(promotion_report)
  └─ 卸载旧模型: model_ml/rf_20260410
  └─ 加载新模型: model_ml/rf_20260425
  └─ 刷新 ML 阈值
  
  └─ 后续订单使用新模型

时刻8 (2026-04-28 ~ 2026-05-05): 新模型运行期间
  └─ 持续监控 sharpe_30d
  └─ 如果 sharpe < 0 持续 60 天，自动回滚
  └─ 如果手动发现问题，可立即手动回滚

--- 成功案例的关键 ---
✓ A/B 测试确保新版本真的更优
✓ 自动门禁防止坏版本上线
✓ 运行时同步确保新版本立即生效
✓ 后续监控和回滚机制保护交易
```

**从这个案例学到的**：
1. 新模型要经过 A/B，不要直接上线
2. 即使本地测试好，也要给 A/B 充足的样本量（100+ 很重要）
3. A/B 通过不是终点，后续持续监控同样重要
4. 旧版本进入 paused 后可以随时回滚，不用删除

---

### 案例 2：参数优化爆炸与恢复

**背景**：
- 周参数优化器跑了一遍，生成 6 个新参数候选
- 其中 1 个参数 (params_strategy/macross_btc_20260426) 在交易中表现特别差
- 该参数已经自动被晋升为 active（因为之前手工调整了门禁）
- 现在需要紧急回滚

**问题过程**：

```
时刻1 (2026-04-26 03:30): 周优化器完成
  └─ 优化目标 6 个策略参数
  └─ 输出 6 个新候选: params_strategy/...
  └─ 都注册为 candidate 状态

时刻2 (2026-04-26 08:00): 人工审查，强制晋升
  └─ 用户想快速上线新参数
  └─ 调用 force_promote 跳过 gate
  └─ 6 个新参数都变成 active（替换旧参数）

时刻3 (2026-04-26 09:00): 交易执行
  └─ 使用新参数的 macross 策略开始交易
  └─ 前期没问题

时刻4 (2026-04-26 13:00): 问题出现
  └─ 某个时间段，macross 策略连续亏损
  └─ 回撤达到 -8%（远超预期的 4%）
  └─ 用户惊慌：这个参数一定有问题！

时刻5 (2026-04-26 14:00): 应急处理
  └─ 用户想立即回滚到前一版本
  
  └─ 但查询才发现：
     - 前一版本被标记为 paused（不是 active）
     - 前前版本已经被 retired（无法快速恢复）
  
  └─ 现在 _prev_active["params_strategy"] 指向 paused 版本
  └─ 手动回滚可行！

时刻6 (2026-04-26 14:05): 手动回滚执行
  POST /api/v1/control
  {
      "action": "rollback_evolution",
      "current_candidate_id": "params_strategy/macross_btc_20260426",
      "rollback_to_candidate_id": "params_strategy/macross_btc_20260420"
  }
  
  └─ 后端处理:
      - 当前版本状态: active → paused
      - 回滚目标状态: paused → active
      - 运行时重新加载参数
  
  └─ 立即生效，后续订单使用旧参数

时刻7 (2026-04-26 14:10): 恢复正常
  └─ macross 策略回到旧参数
  └─ 回撤止住

时刻8 (分析和学习):
  └─ 事后分析为什么参数这么差:
     - 周优化器用的是回测数据 (2026-04-13 ~ 2026-04-26)
     - 但 2026-04-26 当天市场突然波动，参数完全不适应
     - 这是 paper trading gap: 历史数据好 ≠ 实时数据好
  
  └─ 改进措施:
     - 下次强制晋升前要手工审查参数的鲁棒性
     - 强制晋升应该配合 A/B 测试，而不是直接上线
     - 如果一定要快速上线，应该配置更严格的自动回滚条件
```

**从这个案例学到的**：
1. 周优化器生成的候选要先进 A/B，不要跳过
2. 强制晋升是快速但高风险的操作，要谨慎
3. 回滚前要确保前一版本还在（_prev_active）
4. 实时市场和历史数据总会有 gap，要有应急预案

---

### 案例 3：A/B 测试样本量不足导致的错误晋升

**背景**：
- 新 RL policy (policy_rl/ppo_20260424) 看起来不错
- 用户不想等 A/B 完全评估，强行调低 min_samples（从 100 改成 20）
- 结果导致基于不充分数据的错误晋升

**问题过程**：

```
时刻1: 用户调整 ABExperimentConfig
  
  app_config = {
      "ab_test": ABExperimentConfig(
          min_samples=20  # 从 100 降到 20（危险！）
      )
  }

时刻2 (2026-04-24): A/B 开始
  control: policy_rl/ppo_20260420 (当前 active)
  test: policy_rl/ppo_20260424 (新 policy)

时刻3 (2026-04-25 10:00): A/B "完成"
  samples: 20 (control) + 20 (test)
  pnl_control: 50
  pnl_test: 55
  lift: 55 - 50 = 5
  
  ✓ 通过门禁（样本量 >= 20, lift >= 0）
  └─ 决策: PROMOTE
  └─ policy_rl/ppo_20260424 变成 active

时刻4 (2026-04-25 11:00): 新 policy 上线

时刻5 (2026-04-25 ~ 2026-04-26): 后续交易
  新 policy 初期表现可以
  但到第 3 天开始，表现恶化
  
  累积结果:
  - 样本量 100: pnl = -20 (而不是预期的 +55)
  - 回撤: 6%
  - 连续亏损日数: 3 天
  
  真实情况: 新 policy 不如旧 policy
  但因为 A/B 样本量太少，随机波动导致错误判断

时刻6 (2026-04-26 08:00): 自动回滚触发
  退役条件: Sharpe < 0 for 60 days
  
  新 policy sharpe 目前: 0.1（接近触发）
  连续 3 天亏损，趋势明显下行
  
  后台监控判定: 应该尽早回滚
  └─ 自动回滚执行
  └─ policy_rl/ppo_20260424: active → paused
  └─ policy_rl/ppo_20260420: paused → active

--- 关键学习点 ---

❌ 不要降低 min_samples，即使赶时间
   原因: 20 个样本的方差很大，容易被随机波动误导

✓ 合理的 min_samples 选择:
  - 低频策略 (日均 < 10 笔): 200-500
  - 中频策略 (日均 10-100): 100-200
  - 高频策略 (日均 > 100): 50-100

✓ 如果实在着急，宁可:
  - 用 paper 模式做压力测试 3-5 天
  - 而不是缩短 A/B 的样本量要求
```

---

## 总结与最佳实践清单

### 快速参考表

| 问题 | 症状 | 快速修复 | 深度诊断 |
|------|------|---------|---------|
| 候选卡住 | candidate 不晋升 | 检查 sharpe_30d | 读 decisions.jsonl 看原因 |
| A/B 卡住 | 样本增长停滞 | 检查 test 是否交易 | 启用日志搜索 "AB" |
| 无法回滚 | 回滚按钮禁用 | 检查 _prev_active 存在否 | 确认前版本状态 |
| 指标不更新 | sharpe/dd 永不变 | 确认交易正在执行 | 检查 on_fill 有无调用 update_metrics |
| 性能变慢 | API 响应 > 1s | 查看候选数 | 分析哪个模块耗时 |

### 最佳实践清单

```
□ 参数优化
  ✓ 定期检查周优化器的运行日志
  ✓ 新生成的候选先进 A/B，不要直接上线
  ✓ 优化目标列表要定期更新（去掉已下线的策略）

□ A/B 测试
  ✓ min_samples >= 100，不要缩短
  ✓ 同时监控 control 和 test 样本增长曲线
  ✓ A/B 完成后再晋升，不要跳过

□ 晋升流程
  ✓ 优先使用自动晋升（通过 PromotionGate）
  ✓ 只在必要时手动强制晋升
  ✓ 强制晋升一定要记录清晰的 reason

□ 回滚策略
  ✓ 启用自动回滚（auto_rollback_on_retire=true）
  ✓ 定期审视回滚日志，分析失败模式
  ✓ 保留至少 2 个历史版本以备回滚

□ 监控告警
  ✓ 关注后端日志 [Evolution], [Promotion], [Retirement] 标签
  ✓ 定期查看 storage/phase3_evolution/ 下的文件大小
  ✓ 建立告警：如果某家族 3 天内出现 2 次自动回滚，告警

□ 性能管理
  ✓ 定期清理 retired 候选（至少每月一次）
  ✓ candidates.json 超过 5MB 时考虑分离存储
  ✓ 周优化器限制 trial_count <= 100，timeout <= 600s
```

---

## 相关文档

- [evolution-active-candidates-detailed-zh.md](evolution-active-candidates-detailed-zh.md) - 活跃候选项详细解析
- [evolution-workspace-review-zh.md](evolution-workspace-review-zh.md) - Evolution 工作区整体状态回顾
- [alpha-brain-tech-explanation-zh.md](alpha-brain-tech-explanation-zh.md) - Alpha Brain 技术细节

---

**最后更新**: 2026-04-26  
**文档版本**: 1.0  
**包含部分**: FAQ (7个) + 故障排查流程 (4个) + 性能优化 (5个) + 实战案例 (3个)
