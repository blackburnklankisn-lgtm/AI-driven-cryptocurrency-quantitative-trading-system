# Evolution工作区 "活跃候选项" (Active Candidates) 详细解析

本文为你深入解析当前Evolution工作区显示的11个"活跃候选项"是什么、来自哪里、用来做什么。

---

## 一、什么是 "活跃候选项"

### 定义
**"活跃候选项"** = 状态为 `active` 的候选快照列表

每个候选代表的是系统某个**能力模块的一个运行版本**。它不是代码版本，而是运行时工件（parameters, models, policies, strategies 等）的版本治理对象。

### 生命周期
```
candidate → shadow → paper → active → paused/retired
```

其中 `active` 表示"当前真正在被系统使用的有效版本"。

---

## 二、11个候选项分别是什么（按owner分类）

根据代码中的候选快照结构，这11个候选项来自 6 个不同的 **owner** 模块：

### 1. **strategy_market_making/avellaneda_*** (1-2个)
- **类型**: `strategy` (策略)
- **作用**: Avellaneda 做市策略的当前有效版本
- **是什么**: 参数集 + 策略引擎的绑定版本
  - 例如: `strategy_market_making/avellaneda_g0p1200_inv0p2000_ee4a9a`
  - `g0p1200` = gamma 参数 0.12
  - `inv0p2000` = inventory_target 参数 0.20
  - `ee4a9a` = 版本后缀 (random hex)
- **位置代码**: `apps/trader/main.py` L4166

### 2. **policy_rl/ppo** (1个)
- **类型**: `policy` (RL策略)
- **作用**: 强化学习（PPO算法）的当前有效policy基线
- **是什么**: 经过训练的 RL policy 参数集，用于策略决策
- **位置代码**: `modules/evolution/self_evolution_engine.py` (RL模块注册)

### 3. **params_risk/*** (1个)
- **类型**: `params` (风险参数)
- **作用**: 风险管理参数集（如仓位上限、回撤限制、杠杆倍数等）
- **是什么**: 风控子系统当前使用的参数版本
- **示例字段**: `risk_aversion`, `max_drawdown_pct`, `position_limit_pct`
- **作用**: 直接影响订单执行风控

### 4. **params_strategy*** (3-4个)
- **类型**: `params` (策略参数)
- **作用**: 不同策略的参数集版本
- **是什么**: 策略的超参数组合
  - **可能包括**:
    - 均线交叉策略参数 (fast_ma, slow_ma, signal_period 等)
    - 动量策略参数 (momentum_window, threshold 等)
    - 价差策略参数 (spread_threshold 等)
- **重复现象**: 你看到 "3x params_strategy" 可能意味着：
  - 3 个不同基础策略各有一个 active params 版本
  - 或同一策略在不同symbol上有不同参数版本
  - 代码中 `family_key` 会区分 `mm/avellaneda/BTC` vs `mm/avellaneda/ETH`

### 5. **model_ml/rf** (1个)
- **类型**: `model` (ML模型)
- **作用**: Random Forest 机器学习模型的当前有效版本
- **是什么**: 经过训练的 RF 模型文件 + 版本ID
  - 存储在 `models/` 目录，由 `ContinuousLearner` 管理
  - 用于预测市场信号或生成策略信号
- **位置代码**: `apps/trader/main.py` L2226 (获取ML候选)

### 6. **params_ml/*** (2个)
- **类型**: `params` (ML参数)
- **作用**: ML模型的阈值和后处理参数
- **是什么**: 例如:
  - `buy_threshold: 0.65`
  - `sell_threshold: 0.35`
  - 或分类边界点 (decision boundaries)
- **来源**: 由 `ContinuousLearner.get_optimal_thresholds()` 自动生成
- **作用**: 决定 ML 预测如何转化为交易信号

---

## 三、为什么会有11个？为什么是这些owner？

### 系统架构的 "槽位" 设计

Evolution 系统将整个交易系统拆分成 6 个独立的 **参数/策略演进槽位**：

```
┌─ Strategy Layer ─────────┐
│  strategy_market_making  │  当前有效的做市策略（1个）
│  (Avellaneda)            │
└──────────────────────────┘
          │
┌─ RL Policy Layer ────────┐
│  policy_rl (PPO)         │  当前有效的RL策略（1个）
└──────────────────────────┘
          │
┌─ Risk Management Layer ──┐
│  params_risk             │  当前有效的风控参数（1个）
└──────────────────────────┘
          │
┌─ Strategy Params Layer ──┐
│  params_strategy         │  多个策略的参数（3-4个）
│  (MA Cross, Momentum,    │
│   Spread, etc.)          │
└──────────────────────────┘
          │
┌─ ML Model Layer ─────────┐
│  model_ml/rf             │  当前有效的ML模型（1个）
│  params_ml               │  ML阈值参数（2个）
│  (Thresholds)            │
└──────────────────────────┘
```

每个 owner 代表一个**独立进化循环**，可以：
- 独立注册新候选
- 独立测试（A/B实验）
- 独立晋升到 active
- 独立回滚到前任版本

### 为什么不只有一个 active

这样设计的原因是：

1. **模块化独立性**: 风控参数坏了不会影响做市策略；ML模型更新不会直接回滚RL policy
2. **并行演进**: 可以同时测试多个模块的新版本，而不互相干扰
3. **快速定位**: 如果系统表现突然变差，可以按模块逐一排查
4. **降级灵活性**: 可以只回滚某个模块，而保留其他模块的新版本

---

## 四、每个候选的数据结构是什么

当你在UI上看到一个候选项时，其实是这样的结构（来自 `_candidate_to_summary` 函数）：

```python
{
    "candidate_id":    "strategy_market_making/avellaneda_g0p1200_inv0p2000_ee4a9a",
    "owner":           "strategy_market_making",
    "family_key":      "mm/avellaneda/BTC",  # 用于分组和A/B对照
    "strategy_id":     "avellaneda_btc",     # 关联的策略ID
    "version":         "avellaneda_g0p1200_inv0p2000_ee4a9a",
    "status":          "active",
    "candidate_type":  "strategy",
    "sharpe_30d":      1.2,                  # 近30天Sharpe比率
    "max_drawdown_30d": 0.052,               # 最大回撤
    "win_rate_30d":    0.58,                 # 胜率
    "ab_lift":         0.15,                 # A/B测试相对收益提升
}
```

**关键字段说明**：
- `candidate_id`: 全局唯一ID
- `family_key`: 用于同family对象的A/B对照（例如 `mm/avellaneda/BTC` 和 `mm/avellaneda/ETH` 是不同family）
- `status = "active"`: 表示当前在生效使用
- `sharpe_30d`: 近30天Sharpe，用于评估版本质量
- `ab_lift`: A/B测试时新版本vs当前版本的相对增益

---

## 五、它们是怎么注册的？为什么有222个candidate但只有11个active？

### 注册过程
当系统启动或运行时，以下场景会产生新候选：

1. **参数优化器运行** → 生成新的 `params_strategy` 候选
2. **模型重训练** → 生成新的 `model_ml` 候选
3. **ML阈值优化** → 生成新的 `params_ml` 候选
4. **RL policy更新** → 生成新的 `policy_rl` 候选
5. **手动参数调整** → 生成新的候选版本

**持久化位置**: `storage/phase3_evolution/candidates.json`

```json
{
  "strategy_market_making/avellaneda_g0p1200_inv0p2000_ee4a9a": {
    "candidate_id": "strategy_market_making/avellaneda_g0p1200_inv0p2000_ee4a9a",
    "candidate_type": "strategy",
    "owner": "strategy_market_making",
    "version": "...",
    "status": "active",
    "created_at": "2026-04-27T10:30:00+00:00",
    "promoted_at": "2026-04-27T10:35:00+00:00",
    "sharpe_30d": 1.2,
    "max_drawdown_30d": 0.052,
    "win_rate_30d": 0.58,
    "ab_lift": 0.15,
    "metadata": {...}
  },
  ... (共222条记录)
}
```

### 为什么 11 vs 222
- **11 个 active**: 当前真正在用的各个模块版本
- **222 个总候选**: 历史上所有生成过的版本
  - 老版本不会删除，只会变更状态为 `paused` 或 `retired`
  - 这样设计是为了可审计和可回滚
  - 例如: `params_strategy` 可能有 20+ 个历史版本，但只有最新的1个是 `active`

---

## 六、这些候选是如何进入 "active" 状态的？

### 正常流程（理论设计）
```
candidate --[验证Sharpe≥0.8等]--> shadow
   ↓                                  ↓
   │                         [验证30天稳定]
   │                                  ↓
   │                            paper
   └──────────[或直接跳过]──────────→ ↓
                                [A/B测试]
                                   ↓
                               active
```

### 当前实际来源（你看到的这11个）
当前这批 active 候选**大多数不是通过上面自动门禁跑出来的**，而是：

**系统启动时的 "基线初始化"**

代码逻辑（来自 `apps/trader/main.py` L2200-2250）：

```python
def _maybe_activate_or_ab_candidate(self, strategy_id: str):
    # 检查该family是否已有active baseline
    existing_active = self._phase3_evolution.get_active_for_family(family)
    
    if existing_active is None:
        # 没有baseline，直接强制晋升
        current_candidate = self._register_new_candidate(...)
        self._phase3_evolution.force_promote(
            current_candidate.candidate_id,
            reason="INITIAL_RUNTIME_BASELINE",  # ← 关键标记
            metadata={"MANUAL_OVERRIDE": True}
        )
```

**含义**：
- 系统启动时，为了能立即运行，对每个 family 强制设置一个 active baseline
- 这不是说这个版本最优，只是说"系统需要一个起点"
- 审计日志里会记录 `reason = "INITIAL_RUNTIME_BASELINE"`
- 这个设计的目的是让 A/B 和回滚有对标对象

---

## 七、这些候选的核心作用是什么？

### 在实时交易中的应用链路

```
市场行情 → 数据预处理
    ↓
[1] strategy_market_making.compute()  ← 使用avellaneda候选参数
    ↓
[2] RL Policy evaluate                 ← 使用policy_rl候选
    ↓
[3] Strategy 参数应用                   ← 使用params_strategy候选（3-4个）
    ↓
[4] ML Model 预测                       ← 使用model_ml候选
    ↓
[5] ML 阈值处理                         ← 使用params_ml候选（2个）
    ↓
[6] 风控参数检查                        ← 使用params_risk候选
    ↓
订单执行
```

每个 active 候选都在链路上发挥实际作用。

---

## 八、实现细节概览

### 候选管理的三个核心模块

1. **modules/alpha/contracts/evolution_types.py**
   - `CandidateType` 枚举: 定义6种候选类型 (model/strategy/policy/params)
   - `CandidateStatus` 枚举: 定义生命周期状态
   - `CandidateSnapshot`: 候选数据结构 (frozen dataclass)

2. **modules/evolution/candidate_registry.py**
   - `register()`: 注册新候选
   - `transition()`: 变更候选状态
   - `update_metrics()`: 更新候选指标 (sharpe/drawdown/win_rate)
   - 持久化到 `storage/phase3_evolution/candidates.json`

3. **modules/evolution/self_evolution_engine.py**
   - `run_cycle()`: 执行一次完整演进周期
   - `force_promote()`: 强制晋升（用于基线初始化）
   - 协调所有子模块 (PromotionGate, RetirementPolicy, ABTestManager等)

### 它们是如何被执行的

在实时交易中：

```python
# apps/trader/main.py L2200+
def _get_evolution_candidate_id(self, strategy_id: str, slot: str = "model"):
    """获取指定槽位的当前active候选ID"""
    candidates = self._phase3_evolution.list_active()
    for c in candidates:
        if c.family_key == strategy_id and c.slot == slot:
            return c.candidate_id
    return None

# 实时执行
current_strategy_candidate = self._get_evolution_candidate_id("avellaneda_btc")
current_candidate = self._phase3_evolution.get_candidate(current_strategy_candidate)
result = current_candidate.load_and_execute(market_data)
```

---

## 九、可视化导览

```
Evolution 工作区的 11 个 active 候选项
    ├─ [1] strategy_market_making (Avellaneda做市)
    │     └─ ID: strategy_market_making/avellaneda_g0p1200_inv0p2000_ee4a9a
    │        Status: active
    │        Owner: strategy_market_making
    │        Type: strategy
    │        Sharpe: 1.2 | Drawdown: 5.2% | Win%: 58%
    │
    ├─ [2] policy_rl (RL强化学习策略)
    │     └─ ID: policy_rl/ppo_v20260427_abc123
    │        Type: policy
    │        Status: active
    │        AB Lift: +12%
    │
    ├─ [3] params_risk (风险参数)
    │     └─ ID: params_risk/20260427_baseline
    │        Contains: max_drawdown_pct, position_limit, leverage_max, etc.
    │        Type: params
    │
    ├─ [4-6] params_strategy (3个策略参数组)
    │     ├─ params_strategy/macross_btc_20260427
    │     ├─ params_strategy/momentum_eth_20260427
    │     └─ params_strategy/spread_sol_20260427
    │        Type: params
    │        Each controls: period, threshold, signal_mode, etc.
    │
    ├─ [7] model_ml/rf (Random Forest模型)
    │     └─ ID: model_ml/rf_20260420_trained
    │        Path: models/rf_v20260420.pkl
    │        Type: model
    │        Last Retrain: 2026-04-20
    │
    └─ [8-9] params_ml (2个ML阈值)
          ├─ params_ml/rf_thresholds_btc
          └─ params_ml/rf_thresholds_eth
             Type: params
             Contains: buy_threshold, sell_threshold, signal_weight

每个候选都是独立进化循环的胜出者或基线版本
```

---

## 十、总结要点

| 概念 | 说明 |
|------|------|
| **Active Candidate** | 系统当前正在使用的某个能力模块的版本 |
| **11个** | 当前有11个不同"槽位"的有效版本 |
| **222个** | 历史上产生过222个候选版本，只有11个active |
| **Owner** | 候选所属的模块 (strategy/policy/params/model) |
| **Family Key** | 用于同类对象分组和A/B对照的标识 |
| **Candidate ID** | 全局唯一，格式: `{owner}/{params}_{hash}` |
| **Status** | active/paused/retired/candidate/shadow/paper |
| **Metrics** | sharpe_30d, max_drawdown_30d, win_rate, ab_lift |
| **来源** | 系统初始化基线 + 参数优化器 + 模型重训 + RL更新 |
| **实时应用** | 交易链路上每个环节都实际使用对应候选 |

---

## 十一、A/B测试具体流程详解

### A/B测试的核心目的

A/B测试是为了在新版本上线前，**定量验证新候选相对于当前版本是否真的更优**，而不是凭感觉或理论推断。

### A/B测试的完整流程

```
[1] 创建实验
     ↓
control_id = 当前active候选ID
test_id = 新的候选ID
experiment_id = ab_xxx (随机ID)

[2] 运行期间累积数据
     ↓
Control分支: 继续使用当前active版本
Test分支: 使用新候选版本
两个分支 **并行运行，同步喂入 step_pnl 数据**

[3] 记录step PnL
每个时间步：
  - control_side += step_pnl
  - test_side += step_pnl
  - 同时跟踪 max_drawdown

[4] 样本量检查
     ↓
当 control_samples >= min_samples (默认100)
且 test_samples >= min_samples
则进行评估

[5] 门禁评估
     ↓
lift = test_pnl_sum - control_pnl_sum
passes = (
    lift >= lift_threshold (默认0.0)
    AND
    (test_max_drawdown - control_max_drawdown) <= max_drawdown_diff (默认0.02)
    AND
    样本量都达到最小值
)

[6] 决策
     ↓
IF passes:
    winner = test_id (新版本更优)
    将 ab_lift 写入 test 候选指标
ELSE:
    winner = control_id (保持现状)
    记录失败原因 (reason_codes)

[7] 后续晋升
     ↓
IF test通过A/B:
    paper → active (晋升到生产环境)
    旧active → paused (旧版本暂停)
ELSE:
    test 保持在 paper 状态，继续观察或回滚
```

### A/B测试的数据结构

```python
# 进行中的实验状态
{
    "experiment_id": "ab_7cdf92f5",
    "control_id": "model_ml/rf_20260410",        # 当前版本
    "test_id": "model_ml/rf_20260425",           # 新版本
    "control_pnl_sum": 1250.5,                   # 累计PnL
    "test_pnl_sum": 1380.2,
    "control_samples": 105,                      # 样本量
    "test_samples": 105,
    "control_max_drawdown": 0.045,               # 最大回撤
    "test_max_drawdown": 0.038,
    "has_sufficient_samples": true,              # 是否有足够样本
    "started_at": "2026-04-22T10:30:00Z"
}

# 评估结果
{
    "experiment_id": "ab_7cdf92f5",
    "control_id": "model_ml/rf_20260410",
    "test_id": "model_ml/rf_20260425",
    "control_pnl": 1250.5,
    "test_pnl": 1380.2,
    "lift": 129.7,                               # PnL差值
    "control_max_drawdown": 0.045,
    "test_max_drawdown": 0.038,
    "control_samples": 105,
    "test_samples": 105,
    "passes_gate": true,                         # 通过门禁
    "reason_codes": [],                          # 失败原因（如有）
    "decided_at": "2026-04-24T15:20:00Z"
}
```

### 代码实现位置

- **ABTestManager**: `modules/evolution/ab_test_manager.py`
  - `create_experiment()`: 创建新实验
  - `record_step()`: 喂入step数据
  - `evaluate()`: 执行门禁评估
  - `close_experiment()`: 强制完结实验

- **调用方**: `apps/trader/main.py`
  - 在每个订单执行后调用 `record_step(experiment_id, is_test, step_pnl)`
  - 定期检查 `evaluate(experiment_id)` 是否通过

### 真实场景示例

假设你要升级 ML 模型：

```
时刻1: 注册新ML模型候选 (model_ml/rf_20260425)
      → 状态: candidate

时刻2: 创建A/B实验
      → control = model_ml/rf_20260410 (当前active)
      → test = model_ml/rf_20260425 (新候选)

时刻3-14: 连续10天运行
      → 每个订单填充 record_step(...)
      → control侧: 积累 1250.5 PnL, 100 samples
      → test侧: 积累 1380.2 PnL, 100 samples

时刻15: 自动评估
      → lift = 1380.2 - 1250.5 = 129.7 ✓ (超过门禁 0.0)
      → drawdown_diff = 0.038 - 0.045 = -0.007 ✓ (小于容忍 0.02)
      → passes_gate = true ✓

时刻16: 晋升决策
      → model_ml/rf_20260425 自动晋升为 active
      → model_ml/rf_20260410 自动进入 paused
      → 后续所有订单使用新模型

时刻17: 旧版本可选回滚
      → 如果新模型表现进一步恶化
      → 可手动回滚到 model_ml/rf_20260410
```

---

## 十二、回滚机制的详细流程图

### 回滚的核心作用

回滚是当版本表现恶化时，**自动或手动将系统切回上一个稳定版本**的能力。这是交易系统必需的"失败恢复"机制。

### 自动回滚的触发条件

```
系统持续监控 active 候选的表现
     ↓
检查以下条件（RetirementPolicy）：

[条件1] 轻微恶化
  Sharpe < 0.5 for 30 days
  → 动作: DEMOTE (降级到paused)
  
[条件2] 严重恶化
  Sharpe < 0 for 60 days
  → 动作: ROLLBACK (回滚到上一版本)
  
[条件3] 风险违规
  risk_violations >= 5
  → 动作: ROLLBACK (立即回滚)
  
[条件4] 极限回撤
  max_drawdown > 0.15 (15%)
  → 动作: ROLLBACK (立即回滚)
```

### 自动回滚的完整流程

```
┌─ 监控 active 候选指标 ──────────┐
│                               │
│  每 N 个 bar 更新一次:        │
│  - sharpe_30d                 │
│  - max_drawdown_30d           │
│  - win_rate_30d               │
│  - 风险违规次数                │
│                               │
└────────────────┬──────────────┘
                 ↓
        ┌─ 触发回滚条件? ──────┐
        │                    │
        YES                  NO
        │                    │
        ↓                    └→ 继续监控
   
┌─ 获取上一版本信息 ──────┐
│                      │
│ _prev_active 字典   │
│ 记录了该family的    │
│ 上一个active版本ID  │
│                      │
└──────────┬──────────┘
           ↓
    ┌─ 上一版本存在? ─┐
    │               │
    YES            NO
    │              │
    ↓              └→ 直接RETIRE
                       (无回滚目标)

┌─ 执行回滚 ──────────────────────┐
│                               │
│ [1] 当前版本状态变更          │
│     active → paused          │
│                               │
│ [2] 上一版本状态变更          │
│     (paused/retired) → active │
│                               │
│ [3] 运行时同步               │
│     reload 上一版本的工件     │
│                               │
│ [4] 审计日志                 │
│     记录 ROLLBACK 事件       │
│     from: 当前ID             │
│     to: 上一ID               │
│     reason: 条件1/2/3/4      │
│                               │
└───────────────┬──────────────┘
                ↓
        ┌─ 后续策略 ──┐
        │            │
        新版本 paused,
        已回滚至上一版本,
        系统继续运行,
        待人工审查
```

### 手动回滚的操作（UI界面）

```
用户在 Evolution 页面点击 "手动回滚进化" 按钮
     ↓
选择要回滚的候选 (current_candidate_id)
     ↓
选择回滚目标 (rollback_to_candidate_id)
     ↓
确认回滚
     ↓
API 调用: POST /api/v1/control
  {
    "action": "rollback_evolution",
    "current_candidate_id": "model_ml/rf_20260425",
    "rollback_to_candidate_id": "model_ml/rf_20260410"
  }
     ↓
后端执行 manual_rollback_evolution()
  
  [1] 验证evolution引擎可用
  [2] 调用 evolution.manual_rollback(...)
  [3] 同步运行时状态
  [4] 返回结果 {ok, message, rollback_from, rollback_to}
     ↓
前端显示结果
  ✓ 回滚成功: "已从 v2 回滚至 v1"
  ✗ 回滚失败: "no_rollback_target" 等原因
```

### 回滚的数据流

```python
# 审计记录示例
{
    "action": "ROLLBACK",
    "from_status": "active",
    "to_status": "active",
    "current_candidate_id": "model_ml/rf_20260425",
    "rollback_to_candidate_id": "model_ml/rf_20260410",
    "reason_codes": ["SHARPE_THRESHOLD", "DRAWDOWN_EXCEEDED"],
    "timestamp": "2026-04-24T18:45:00Z",
    "metadata": {
        "trigger": "automatic",  # 或 "manual"
        "sharpe_30d": -0.15,
        "max_drawdown_30d": 0.18,
        "wind_factor": 0.8
    }
}

# 运行时状态同步
before:
{
    "active_model": "model_ml/rf_20260425",
    "model_weights": [0.6, 0.4],
    "thresholds": {"buy": 0.65, "sell": 0.35}
}

after (回滚):
{
    "active_model": "model_ml/rf_20260410",
    "model_weights": [0.5, 0.5],
    "thresholds": {"buy": 0.60, "sell": 0.40}
}
```

### 关键代码位置

- **回滚逻辑**: `modules/evolution/self_evolution_engine.py` L747-850
  - `_run_retirements()`: 检查退役条件，决定是否DEMOTE/RETIRE/ROLLBACK
  
- **手动回滚**: `apps/trader/main.py` L3199-3270
  - `manual_rollback_evolution()`: 执行手动回滚
  - `_apply_evolution_runtime_state()`: 同步运行时状态

- **API接口**: `apps/api/server.py` L1126-1145
  - `/api/v1/control` endpoint 处理 `rollback_evolution` 动作

---

## 十三、参数优化器如何生成新候选

### 参数优化器的目的

参数优化器是一个**自动化工具**，定期（通常周级）根据最新的历史市场数据，重新计算最优参数，并将结果注册为新候选。这使得参数调优从手工变成系统能力。

### 触发机制

```
┌─ 周期性任务 ─────────────────────┐
│                                │
│ Cron 表达式: "0 3 * * 0"       │
│ → 每周日 03:00 UTC              │
│                                │
└────────────┬────────────────────┘
             ↓
    ┌─ 检查触发条件 ───┐
    │                 │
    │ 当前时刻是否    │
    │ 匹配 cron 表达式?│
    │                 │
    └─ YES ──┬──── NO ─→ 跳过
             ↓
  启动后台线程处理
```

### 优化流程

```
[第1步] 收集优化目标
        target_kind = "ml_strategy"
        收集所有需要优化的策略 ID
        (来自 _collect_phase3_param_optimization_targets())

[第2步] 循环处理每个策略
        FOR each strategy_id in targets:
            symbol = strategy.symbol
            
            [2.1] 获取历史OHLCV数据
                  bar_count = 5000  (约20-30天)
                  df = fetch_ohlcv(symbol, "1h")
                  
            [2.2] 构建优化数据框
                  df_opt = _build_weekly_ml_optimization_dataframe(df)
                  包含: 特征、标签、信号等
                  
            [2.3] 调用优化器
                  result = optimize_params_from_dataframe(
                      df_opt,
                      strategy_type = strategy.model_type,
                      optimize_target = "sharpe",  # 或 "roi" / "win_rate"
                      trial_count = 100,
                      timeout_sec = 600
                  )
                  使用 optuna 库进行贝叶斯优化
                  
            [2.4] 提取最优参数
                  best_params = result.best_params
                  例如: {
                    "buy_threshold": 0.62,
                    "sell_threshold": 0.38,
                    "signal_strength_weight": 0.8,
                    ...
                  }
                  
            [2.5] 生成运行时工件
                  artifact_path = save_optimized_params(
                      symbol,
                      best_params,
                      result.best_value  # Sharpe值
                  )
                  保存到 runtime/ml_params/{symbol}/ 目录
                  
            [2.6] 注册新候选
                  candidate = evolution.register_candidate(
                      candidate_type = CandidateType.PARAMS,
                      owner = "params_ml",
                      version = f"optimized_{symbol}_{timestamp}",
                      metadata = {
                          "symbol": symbol,
                          "artifact_path": artifact_path,
                          "best_value": result.best_value,
                          "trial_count": 100,
                          "source": "weekly_optimizer"
                      }
                  )
                  → 新候选状态: CANDIDATE
                  
            [2.7] 记录日志
                  optimized_symbols.append({
                      "strategy_id": strategy_id,
                      "symbol": symbol,
                      "candidate_id": candidate.candidate_id,
                      "best_sharpe": result.best_value,
                      "artifact_path": artifact_path
                  })

[第3步] 写入审计日志
        evolution.record_weekly_params_optimizer_finish(
            slot_id = slot,
            status = "success",
            optimized_count = len(optimized_symbols),
            optimized_symbols = optimized_symbols,
            errors = errors
        )
        → 保存到 storage/phase3_evolution/weekly_params_optimizer_runs.jsonl
        
[第4步] 更新周优化器状态
        update weekly_params_optimizer_state.json:
        {
            "last_run_at": "2026-04-27T03:00:00Z",
            "status": "completed",
            "optimized_count": 6,
            "next_run_at": "2026-05-04T03:00:00Z"
        }
```

### 参数优化流水线示意

```
┌──────────────────────────────────────────────────────────┐
│          每周日 03:00 UTC 自动触发                        │
└────────┬─────────────────────────────────────────────────┘
         ↓
┌──────────────────────────────────────────────────────────┐
│ [1] 获取目标                                              │
│     strategy_params_btc_macross                          │
│     strategy_params_eth_momentum                         │
│     strategy_params_sol_spread                           │
└────────┬─────────────────────────────────────────────────┘
         ↓
┌──────────────────────────────────────────────────────────┐
│ [2] 拉历史数据（5000根1h bar）                            │
│     BTC/USDT: 2026-04-13 - 2026-04-27                   │
│     ETH/USDT: 2026-04-13 - 2026-04-27                   │
│     SOL/USDT: 2026-04-13 - 2026-04-27                   │
└────────┬─────────────────────────────────────────────────┘
         ↓
┌──────────────────────────────────────────────────────────┐
│ [3] 特征工程 & 构建优化框架                               │
│     特征: OHLC, 均线, 波动率, 成交量...                  │
│     标签: 下个时段是否上涨 (binary)                      │
│     拆分: train (70%) + validate (30%)                  │
└────────┬─────────────────────────────────────────────────┘
         ↓
┌──────────────────────────────────────────────────────────┐
│ [4] Optuna 贝叶斯优化（100 trials）                      │
│     搜索空间:                                            │
│       buy_threshold: [0.5, 0.8]                         │
│       sell_threshold: [0.2, 0.5]                        │
│       signal_strength_weight: [0.0, 1.0]                │
│     目标函数: 最大化 Sharpe                              │
└────────┬─────────────────────────────────────────────────┘
         ↓
┌──────────────────────────────────────────────────────────┐
│ [5] 输出最优参数                                          │
│     params_ml/btc_20260427:                             │
│       buy_threshold: 0.65                               │
│       sell_threshold: 0.32                              │
│       signal_strength_weight: 0.82                      │
│       optimized_sharpe: 1.35                            │
└────────┬─────────────────────────────────────────────────┘
         ↓
┌──────────────────────────────────────────────────────────┐
│ [6] 注册候选                                              │
│     candidate_id: params_ml/ml_params_btc_20260427_x7f2 │
│     status: CANDIDATE                                   │
│     metadata.best_value: 1.35                           │
│     metadata.source: weekly_optimizer                   │
└────────┬─────────────────────────────────────────────────┘
         ↓
┌──────────────────────────────────────────────────────────┐
│ [7] 晋升候选（自动或手动）                               │
│     IF 通过门禁 (Sharpe >= 0.8):                       │
│       candidate → shadow → paper → active               │
│     ELSE:                                               │
│       保持 candidate 状态观察                            │
└──────────────────────────────────────────────────────────┘
```

### 关键代码位置

- **周期调度**: `apps/trader/main.py` L3140-3160
  - `_maybe_start_weekly_ml_params_optimization()`: 检查cron，启动线程
  
- **优化执行**: `apps/trader/main.py` L3363-3380
  - `_run_weekly_ml_params_optimization(slot_id)`: 实现优化流程
  
- **特征构建**: `apps/trader/main.py` L3197-3220
  - `_build_weekly_ml_optimization_dataframe()`: 特征工程
  
- **优化函数**: `scripts/optimize_phase1_params.py`
  - `optimize_params_from_dataframe()`: 使用optuna优化
  
- **审计记录**: `modules/evolution/state_store.py`
  - `record_weekly_params_optimizer_finish()`: 写入日志

---

## 十四、如何从UI手动控制候选的晋升/退役

### UI操作入口

当前Evolution工作区页面包含以下手动控制按钮：

```
┌─────────────────────────────────────────┐
│      Evolution 工作区 / 手动操作面板     │
├─────────────────────────────────────────┤
│                                         │
│  □ 候选项选择器                          │
│    ├─ strategy_market_making/...        │
│    ├─ policy_rl/ppo                     │
│    ├─ params_risk/...                   │
│    ├─ params_strategy/... (x3)          │
│    ├─ model_ml/rf                       │
│    └─ params_ml/... (x2)                │
│                                         │
│  [按钮栏]                                │
│  [ 强制晋升 ]  [ 降级暂停 ]  [ 立即退役 ]│
│  [ 手动回滚 ]  [ 刷新状态 ]  [ 导出审计 ]│
│                                         │
│  [详情面板]                              │
│  ├─ 候选ID: model_ml/rf_20260425       │
│  ├─ 类型: model                        │
│  ├─ 当前状态: shadow                    │
│  ├─ 创建时间: 2026-04-20T10:30:00Z     │
│  ├─ Sharpe 30d: 0.95                   │
│  ├─ Max Drawdown: 0.062                │
│  ├─ Win Rate: 56%                      │
│  ├─ A/B Lift: pending                  │
│  └─ 最后更新: 2026-04-26T15:20:00Z     │
│                                         │
└─────────────────────────────────────────┘
```

### 强制晋升 (Force Promote)

**作用**: 绕过自动门禁，直接将候选提升到下一个生命周期状态。

**使用场景**:
- 新参数已经人工验证过了，不想等自动评估
- 急需切换到新版本应对市场变化
- 基线初始化（系统启动时）

**操作步骤**:

```
[1] 选择候选: candidate_model_ml/rf_20260425
[2] 点击 [强制晋升] 按钮
[3] 弹出确认对话
    当前状态: shadow
    目标状态: paper
    原因: (optional) "Manual override for market condition change"
[4] 确认
     ↓
后端API调用:
  POST /api/v1/control
  {
    "action": "promote_evolution",
    "candidate_id": "model_ml/rf_20260425",
    "from_status": "shadow",
    "to_status": "paper",
    "reason": "Manual override for market condition change"
  }
     ↓
后端逻辑:
  evolution.force_promote(
      candidate_id,
      reason="MANUAL_PROMOTE",
      metadata={"user_reason": reason}
  )
     ↓
执行结果:
  ✓ success: "已晋升至 paper 状态"
  ✗ error: "晋升失败: reason_xxx"
     ↓
前端更新
  - 候选状态显示变更为 paper
  - 审计日志新增一条 PROMOTE 记录
  - 相关metrics刷新
```

**代码实现**: `apps/api/server.py` L1110-1120
```python
elif cmd.action == "promote_evolution":
    current = evolution.get_candidate(cmd.candidate_id)
    if current and current.status != cmd.to_status:
        evolution.force_promote(
            cmd.candidate_id,
            reason="MANUAL_PROMOTE",
            metadata={"user_reason": cmd.reason or ""}
        )
        return {"ok": True, "message": f"Promoted to {cmd.to_status}"}
    return {"ok": False, "message": "Invalid state transition"}
```

### 降级暂停 (Demote to Paused)

**作用**: 将 active 或 paper 候选降级到 paused，暂停其使用，但保留恢复可能。

**使用场景**:
- 发现新版本有问题，但还不确定是否永久放弃
- 想快速切回上一版本观察
- 需要隔离某个有风险的候选

**操作步骤**:

```
[1] 选择候选: model_ml/rf_20260425 (当前状态: active)
[2] 点击 [降级暂停] 按钮
[3] 弹出确认对话
    当前状态: active
    目标状态: paused
    原因: (optional) "Suspicious signal distribution"
    自动切回: (checkbox) ☑ 切回上一版本
[4] 确认
     ↓
后端API调用:
  POST /api/v1/control
  {
    "action": "demote_evolution",
    "candidate_id": "model_ml/rf_20260425",
    "auto_rollback": true,
    "reason": "Suspicious signal distribution"
  }
     ↓
后端逻辑:
  evolution.demote(
      candidate_id,
      reason="MANUAL_DEMOTE",
      auto_rollback=true
  )
  if auto_rollback and _prev_active exists:
      evolution.manual_rollback(...)  # 自动回滚
     ↓
执行结果:
  ✓ success: "已降级至 paused，自动切回上一版本"
  或
  ✓ success: "已降级至 paused"（没有上一版本时）
     ↓
前端更新
  - 当前active候选变更为上一版本（如果回滚）
  - 降级的候选状态显示 paused
```

### 立即退役 (Retire)

**作用**: 永久淘汰某个候选，不再使用。

**使用场景**:
- 确实发现候选不可用，不需要保留
- 清理历史垃圾候选
- 发现代码bug导致的候选

**操作步骤**:

```
[1] 选择候选: model_ml/rf_20260415
[2] 点击 [立即退役] 按钮
[3] 弹出二次确认（严重操作需二次确认）
    警告: ⚠️ 此操作不可撤销！
    候选: model_ml/rf_20260415
    原因: (required) "Bug in feature scaling"
    [取消] [确认退役]
[4] 确认
     ↓
后端API调用:
  POST /api/v1/control
  {
    "action": "retire_evolution",
    "candidate_id": "model_ml/rf_20260415",
    "reason": "Bug in feature scaling"
  }
     ↓
后端逻辑:
  evolution.retire(
      candidate_id,
      reason="MANUAL_RETIRE",
      metadata={"user_reason": reason}
  )
     ↓
执行结果:
  ✓ success: "已退役"
  ✗ error: "无法退役：候选正在active状态"
           (需先降级或回滚)
     ↓
前端更新
  - 候选状态显示 retired
  - 从活跃列表隐藏（或标记灰色）
  - 审计日志新增 RETIRE 记录
```

### 手动回滚 (Manual Rollback)

详见 "十二、回滚机制" 部分。

```
[1] 选择当前active候选
[2] 点击 [手动回滚] 按钮
[3] 弹出回滚目标选择器
    当前版本: model_ml/rf_20260425
    候选回滚目标:
    ☑ model_ml/rf_20260410 (上次active)
    ○ model_ml/rf_20260405
    ○ (no other targets)
[4] 选择目标 + 确认
     ↓
后端API调用:
  POST /api/v1/control
  {
    "action": "rollback_evolution",
    "current_candidate_id": "model_ml/rf_20260425",
    "rollback_to_candidate_id": "model_ml/rf_20260410"
  }
     ↓
执行结果:
  ✓ success: "已回滚: 20260425 → 20260410"
  ✗ error: "无法回滚：目标候选不存在或无效"
```

### 完整API端点参考

**基础信息**：
- **端点**: `POST /api/v1/control`
- **认证**: 需要 API key
- **限流**: 默认 100 req/min

**支持的动作** (action字段):

| 动作 | 参数 | 效果 |
|------|------|------|
| `promote_evolution` | `candidate_id`, `from_status`, `to_status`, `reason` | 强制晋升 |
| `demote_evolution` | `candidate_id`, `auto_rollback`, `reason` | 降级暂停 |
| `retire_evolution` | `candidate_id`, `reason` | 立即退役 |
| `rollback_evolution` | `current_candidate_id`, `rollback_to_candidate_id` | 手动回滚 |
| `trigger_weekly_optimizer` | `(none)` | 立即触发周优化器 |
| `reset_circuit` | `(none)` | 重置熔断器 |
| `stop` | `(none)` | 停止交易 |

**响应格式**:

```json
{
  "ok": true,
  "action": "promote_evolution",
  "message": "已晋升至 paper 状态",
  "data": {
    "candidate_id": "model_ml/rf_20260425",
    "old_status": "shadow",
    "new_status": "paper",
    "timestamp": "2026-04-27T10:15:00Z"
  }
}
```

### 关键代码位置

- **API端点**: `apps/api/server.py` L1095-1160
  - `post_system_control()`: 处理所有control请求
  
- **晋升逻辑**: `modules/evolution/self_evolution_engine.py` L450-480
  - `force_promote()`: 强制晋升实现
  
- **降级逻辑**: `modules/evolution/self_evolution_engine.py` L500-530
  - `demote()`: 降级实现
  
- **退役逻辑**: `modules/evolution/self_evolution_engine.py` L550-580
  - `retire()`: 退役实现

---

## 十五、总结与最佳实践

### 四个核心机制的协作关系

```
参数优化器
   ↓ (生成新candidates)
A/B测试
   ↓ (验证新版本优劣)
晋升/降级
   ↓ (状态转移)
回滚机制
   ↓ (失败恢复)
   ↓
回到active稳定运行
```

### 使用建议

1. **参数优化**：
   - 让周优化器自动跑，无需人工干预
   - 定期检查生成的新candidates质量
   - 不满意时可手动禁用某些target

2. **A/B测试**：
   - 新版本自动进入A/B，不要跳过
   - 门禁参数 (min_samples, lift_threshold) 根据交易频率调整
   - 样本量不足时耐心等待，不要急于晋升

3. **晋升控制**：
   - 优先用自动晋升流程（通过门禁自动升）
   - 只在必要时才手动强制晋升
   - 强制晋升要记录清晰的reason，便于审计

4. **回滚策略**：
   - 启用自动回滚（RetirementPolicy.auto_rollback=true）
   - 监控回滚日志，分析失败模式
   - 手动回滚作为应急手段，日常不需要频繁用

5. **审计跟踪**：
   - 所有操作都会记录在 `decisions.jsonl` 和 `retirements.jsonl`
   - 定期检查审计日志，了解系统演进历史
   - 出问题时回溯审计日志快速定位根因

---

## 十六、相关文档

- [evolution-workspace-review-zh.md](evolution-workspace-review-zh.md) - Evolution工作区整体状态回顾
- [AI_QUANT_TRADER_EVOLUTION_STRATEGY.md](AI_QUANT_TRADER_EVOLUTION_STRATEGY.md) - Evolution战略设计文档
- [alpha-brain-tech-explanation-zh.md](alpha-brain-tech-explanation-zh.md) - Alpha Brain技术细节

---

**最后更新**: 2026-04-26  
**文档版本**: 2.0  
**新增章节**: 十一至十五（A/B测试、回滚、参数优化、UI控制）
