# Evolution 工作区代码复盘与字段解释

本文基于当前仓库代码、前端页面实现以及本地持久化数据 `storage/phase3_evolution` 的真实内容，对 Evolution 工作区中你提到的所有字段逐项解释，并区分三类情况：

1. 后端能力已经实现且当前有真实数据。
2. 后端能力已经实现，但当前运行条件还没有触发，所以页面为空。
3. 页面已经做了入口，但后端还没有完整实现，或者前后端契约没有对齐，所以显示异常或信息不足。

---

## 一、先说结论

当前 Evolution 工作区并不是“完全没有实现”，而是处在“底层状态机已经搭好，但前端展示和运行闭环还没有完全打通”的阶段。

最关键的几个结论如下：

1. `活跃：11 / 候选：222` 这两个数字是真实来自注册表 `storage/phase3_evolution/candidates.json`，不是前端误报。
2. 你当前看到的“最近晋升”大多不是严格通过门禁后自然晋升出来的结果，而是系统启动时为了给每个 family 建一个运行基线，走了 `INITIAL_RUNTIME_BASELINE + MANUAL_OVERRIDE` 的强制晋升。
3. “最近退役”“最近回滚”为空，不代表机制不存在，而是当前数据还没有触发这些条件。
4. “进化报告”区域当前不是没有后端，而是前后端字段定义没有对齐，所以虽然能拿到 `latest_report.json`，但表格中的 `候选项 / 结果 / 创建时间` 基本显示不出来。
5. “A/B 实验”区域当前拿到的是诊断摘要，不是实验明细，所以页面把 `active_experiments / completed_experiments / experiment_ids` 错当成实验行来渲染了，这就是为什么你看到的表格内容很怪。
6. “每周参数优化器”代码已经实现，但当前 `storage/phase3_evolution` 下根本还没有生成 `weekly_params_optimizer_state.json` 和 `weekly_params_optimizer_runs.jsonl`，说明这条任务在当前这份状态目录下实际上还没有跑起来。
7. 页面上的“手动回滚进化”按钮目前前端已经做了，但后端 `/api/v1/control` 实际并没有实现 `rollback_evolution` 动作，所以这是一个明确的未完成功能点。

---

## 二、状态统计与活跃候选项是什么意思

### 1. 什么是 candidate / active

Evolution 的基础数据结构定义在 `modules/alpha/contracts/evolution_types.py` 中，候选生命周期是：

`candidate -> shadow -> paper -> active -> paused / retired`

其中：

1. `candidate`：新注册出来的候选版本，还没通过验证。
2. `shadow`：通过了基础门禁，但只是影子观察，不直接影响真实执行。
3. `paper`：进入纸面验证阶段，通常要配合 A/B 测试。
4. `active`：当前真正被系统当成有效版本使用的候选。
5. `paused`：暂停使用。
6. `retired`：淘汰。

所以页面中的“活跃候选项”，本质上就是注册表里 `status == active` 的候选快照。

### 2. 这些 active 候选项分别是什么

你列出来的这些 active 候选项，本质上是当前系统各个“能力槽位”的正在生效版本。它们不是一种东西，而是不同类别的运行时工件：

1. `strategy_market_making/...`：当前有效的做市策略基线。
2. `policy_rl/...`：当前有效的 RL 策略基线。
3. `params_risk/...`：当前有效的风险参数基线。
4. `params_strategy/...`：当前有效的具体策略参数版本，例如均线交叉、动量策略的参数集。
5. `model_ml/...`：当前有效的 ML 模型版本。
6. `params_ml/...`：当前有效的 ML 阈值/参数版本，例如 buy threshold / sell threshold。

也就是说，Evolution 不是只管“策略代码”本身，它把模型、策略、参数、RL policy 都统一抽象成候选对象来管理。

### 3. 为什么会有 222 个 candidate

这是因为候选注册表 `modules/evolution/candidate_registry.py` 采用的是“持续注册 + 状态变更”的方式，而不是“只保留最新版本”的方式。

每次系统初始化、每次新参数生成、每次模型版本变化，都可能调用 `register_candidate()` 新增一条候选记录。旧候选不会自动删除，只会保留在注册表里，并通过状态字段区分它当前是不是仍然生效。

因此：

1. `11` 个 `active` 表示当前正在用的版本数量。
2. `222` 个 `candidate` 表示历史上已经注册出来、但还没进入 active 的候选积累数量。

这也是为什么你会看到很多同 family 的重复候选，只是尾部随机后缀不同。

### 4. 活跃候选项的选择标准是什么

这里要分“设计上的标准”和“你当前这批数据的实际来源”两层来看。

#### 设计上的标准

正常情况下，候选应当通过 `SelfEvolutionEngine.run_cycle()` 进入自动状态流转：

1. `candidate -> shadow`
   条件来自 `modules/evolution/promotion_gate.py`
   默认要求：`Sharpe >= 0.8`、`max_drawdown <= 0.07`、`risk_violations <= 0`

2. `shadow -> paper`
   默认要求：`Sharpe >= 0.6`、`max_drawdown <= 0.09`

3. `paper -> active`
   默认要求：`Sharpe >= 0.5`、`max_drawdown <= 0.10`、`A/B 已完成`、`ab_lift >= 0`

也就是说，理论上 active 版本应该是经过多阶段验证后留下来的“胜出者”。

#### 你当前这批 active 的实际来源

你现在页面里看到的这批 active，绝大多数并不是通过上面的自动门禁跑出来的，而是 `apps/trader/main.py` 在初始化阶段执行了 `_maybe_activate_or_ab_candidate()`，里面明确写了：

1. 如果 `source` 是 `initial_load` 或 `phase3_init`
2. 并且该 family 还没有 active baseline
3. 就直接调用 `force_promote(..., reason="INITIAL_RUNTIME_BASELINE")`

而 `force_promote()` 在 `modules/evolution/self_evolution_engine.py` 里会写入一条审计日志，并附带 `MANUAL_OVERRIDE`。

这意味着：

1. 当前 active 列表是“启动基线版本”。
2. 它的作用是让系统一启动就有一个可运行的 baseline，不至于所有 family 都卡在 candidate。
3. 这是一种工程上的 bootstrap 设计，而不是严格意义上的“演进竞赛胜出者”。

### 5. 这样设计的意义是什么

这样设计的目的不是为了证明这些 active 候选已经最优，而是为了保证系统一开机就能工作。

核心意义有三个：

1. 每个 family 必须先有一个 baseline，后续新候选才能拿它做对照、做 A/B、做回滚目标。
2. 没有 baseline，就没有 `control`，A/B 体系根本无法启动。
3. 出问题时系统必须知道“应该回滚到谁”，所以先建立 active 基线是必要的。

换句话说，当前这套设计把“运行基线建立”放在了“自动演进优化”之前。

---

## 三、最近晋升是什么，为什么会有这些数据

### 1. 最近晋升的数据来自哪里

页面里的“最近晋升”来自：

1. `apps/api/server.py` 的 `_build_evolution_snapshot()`
2. 它读取 `EvolutionStateStore.load_decisions(limit=10)`
3. 对应文件是 `storage/phase3_evolution/decisions.jsonl`

也就是说，这里显示的是“最近的晋升/降级/回滚审计决策日志”，不是运行时推断值。

### 2. 你当前看到的这些最近晋升，真实含义是什么

我核对了 `storage/phase3_evolution/decisions.jsonl`，你看到的这些记录全部是：

1. `action = PROMOTE`
2. `from_status = candidate`
3. `to_status = active`
4. `reason_codes = ["INITIAL_RUNTIME_BASELINE", "MANUAL_OVERRIDE"]`

这说明当前这批“最近晋升”并不是“自动评估后晋升”，而是“系统初始化时直接设为基线 active”的启动审计记录。

### 3. 正常晋升的逻辑和原理是什么

如果走正常自动演进路径，逻辑应该是：

1. `register_candidate()` 注册新候选。
2. `update_metrics()` 持续写入 `sharpe_30d / max_drawdown_30d / win_rate_30d / ab_lift`。
3. `run_cycle()` 调用 `_run_promotions()`。
4. `_run_promotions()` 调 `PromotionGate.bulk_evaluate()`。
5. 达标的候选被 `transition()` 到下一状态。
6. 如果最终晋升到 `active`，同 family 的旧 active 会先被切成 `paused`，并记录 `_prev_active` 以便未来回滚。

这才是 Evolution 真正的“自动晋升闭环”。

### 4. 最近晋升这些内容本身有什么作用

这些晋升记录的作用主要有四个：

1. 审计：可以知道某个版本何时进入了下一阶段。
2. 溯源：一旦表现恶化，能追查是哪次晋升把它放上去的。
3. 回滚：自动回滚时需要知道上一版本链路。
4. 可视化：让控制中心能展示最近发生的状态变化。

### 5. 这样设计的目的是什么

设计目的不是“显示好看”，而是要建立一套可审计、可回放、可回滚的版本状态机。

如果没有这条决策日志，系统只知道“现在谁是 active”，但不知道“它是怎么上来的”。这对于交易系统是远远不够的。

---

## 四、最近退役为什么没有数据，什么情况下会出现

### 1. 退役逻辑在哪里

退役规则在 `modules/evolution/retirement_policy.py`。

对于 active 候选，默认触发条件是：

1. `Sharpe < 0.5` 且连续 `30` 天，触发 `DEMOTE`，状态变成 `paused`。
2. `Sharpe < 0` 且连续 `60` 天，触发 `RETIRE` 或 `ROLLBACK`。
3. 风险违规次数 `>= 5`，直接触发 `RETIRE` 或 `ROLLBACK`。
4. `max_drawdown > 0.15`，直接触发 `RETIRE` 或 `ROLLBACK`。

### 2. 什么情况下会出现退役数据

只有当以下链路都成立时，页面上才会出现退役记录：

1. 候选已经是 `active`。
2. 系统不断给这个候选写入指标和风险违规次数。
3. `run_cycle()` 被调度执行。
4. `_run_retirements()` 判断结果不是 `HOLD`。
5. 退役记录被写入 `storage/phase3_evolution/retirements.jsonl`。

### 3. 为什么当前没有退役数据

当前 `storage/phase3_evolution` 目录里没有 `retirements.jsonl`，说明至少在这份状态目录下，尚未发生任何一次被持久化的 retire/rollback 事件。

这通常意味着：

1. 当前 active 候选还没有被持续写入足够的负面指标。
2. 风险违规计数没有累计到阈值。
3. 虽然有候选很多，但自动演进周期没有实际把它们推进到“需要退役”的状态。

### 4. 为什么要这样设计

退役机制的设计目的，是防止“坏版本长时间挂在 active 上继续伤害账户”。

它本质上是一个失败保护层：

1. 表现轻微变差，先降级暂停。
2. 表现严重恶化，直接退役。
3. 如果有前任稳定版本，就优先回滚。

这比单纯“人工发现问题后再切换”更稳健，也更适合自动化交易系统。

---

## 五、最近回滚为什么没有数据，什么情况下会出现

### 1. 回滚逻辑的实现原理

自动回滚的实现也在 `modules/evolution/self_evolution_engine.py` 的 `_run_retirements()` 中。

流程是：

1. 当前 active 候选触发退休条件。
2. `RetirementPolicy` 判断如果 `auto_rollback_on_retire = True`，并且存在上一 active 版本，则动作不是 `RETIRE`，而是 `ROLLBACK`。
3. 当前坏版本先被切成 `paused`。
4. `_prev_active` 里记录的上一版本会被重新切回 `active`。
5. 同时写一条 `RetirementRecord`，其中带上 `rollback_to`。

### 2. 在什么情况下会有回滚记录

满足以下条件才会出现：

1. 当前候选已经是 `active`。
2. 同 family 之前确实存在一个老的 active 版本。
3. 当前 active 触发严重风险/业绩恶化。
4. 自动回滚开关打开。

### 3. 为什么当前没有回滚记录

当前没有回滚记录，原因通常是以下几种之一：

1. 还没有 active 候选触发严重退役条件。
2. 当前 active 版本虽然有前任，但还没走到真正的 rollback 分支。
3. 自动演进周期没有把这个 family 跑到退役判断。

### 4. 为什么这样设计

回滚的本质，是把“版本切换失败”从高风险人工操作变成标准化系统动作。

在交易系统里，这个设计非常有意义：

1. 可以缩短坏版本在生产环境停留的时间。
2. 可以降低回撤继续扩大的概率。
3. 可以让版本治理具备“失败恢复”能力，而不是只有“上线能力”。

### 5. 当前还有一个单独的前端缺口

这里还有一个明确问题：页面上有“手动回滚进化”按钮，但后端 `/api/v1/control` 目前只支持：

1. `stop`
2. `reset_circuit`
3. `trigger_circuit_test`

并没有实现 `rollback_evolution`。所以这个按钮目前是前端有入口、后端没有动作，属于未完成功能，而不是“只是当前没数据”。

---

## 六、历史运行 / 进化报告 为什么是 `rpt_7cdf92f5`，但其他列还是暂无

### 1. 这个报告 ID 是什么

报告由 `modules/evolution/report_builder.py` 生成，ID 规则是：

`rpt_` + 8 位随机 hex

当前 `storage/phase3_evolution/latest_report.json` 里确实存在：

`rpt_7cdf92f5`

### 2. 当前这份报告真实内容是什么

当前这份报告的内容是：

1. `total_candidates = 0`
2. `promoted = []`
3. `demoted = []`
4. `retired = []`
5. `rollbacks = []`
6. `decisions = []`
7. `active_snapshot = []`

同时 `scheduler_state.json` 里也显示这次 run 的 `candidates_evaluated = 0`。

这说明当时确实跑过一次 `run_cycle()`，但那一轮并没有评估到任何候选，所以报告是一个“空报告”。

### 3. 为什么表格里 `候选项 / 结果 / 创建时间` 还是暂无

这里不是完全没数据，而是前后端契约不一致。

前端 `apps/desktop-client/src/types/dashboard.ts` 里把 `EvolutionReport` 定义成了：

1. `report_id`
2. `created_at`
3. `candidate_id`
4. `result`
5. `summary`

但后端 `/api/v2/evolution/reports` 实际返回的是 `latest_report.json` 的原始结构，它的字段是：

1. `report_id`
2. `period_start`
3. `period_end`
4. `total_candidates`
5. `promoted`
6. `demoted`
7. `retired`
8. `rollbacks`
9. `decisions`
10. `active_snapshot`
11. `metadata`

也就是说：

1. `report_id` 能显示。
2. `candidate_id` 后端没有这个单字段，所以显示 `暂无`。
3. `result` 后端没有这个单字段，所以显示 `暂无`。
4. `created_at` 后端也没有这个字段，真正对应的其实更接近 `period_end`。

所以这里是“报告接口已经有数据，但前端表格字段设计错位了”。

### 4. 在哪些场景下这里应该显示什么内容

如果把这块契约修正好，这里理论上可以展示：

1. 报告 ID：`report_id`
2. 评估候选数：`total_candidates`
3. 本轮结果摘要：例如 `promoted=2 / retired=1 / rollbacks=1`
4. 报告时间：`period_end`
5. 甚至可展开展示 `decisions` 明细

也就是说，这里不是不能显示，而是当前前端显示模型过于简化，而且字段映射错了。

---

## 七、A/B 实验表格为什么显示成 `active_experiments / completed_experiments / experiment_ids`

### 1. A/B 实验的设计目的是什么

A/B 的目的，是让“新候选版本”不要直接替换旧版本，而是和当前 baseline 做可量化对比。

它的核心作用是回答一个问题：

`新版本是否真的比当前版本更好，且没有带来不可接受的额外风险？`

### 2. A/B 的具体实现原理

实现位于 `modules/evolution/ab_test_manager.py` 和 `apps/trader/main.py`。

核心过程是：

1. `create_ab_experiment(control_id, test_id)` 创建实验。
2. 系统对 control 和 test 分别持续记录 step PnL。
3. 达到最小样本量后执行 `evaluate()`。
4. 判定规则默认是：
   - control 样本量和 test 样本量都要 >= `100`
   - `lift = test_pnl - control_pnl >= 0`
   - `test_max_drawdown - control_max_drawdown <= 0.02`
5. 如果通过，就把 test 候选的 `ab_lift` 写回注册表，给后续 `paper -> active` 晋升门禁使用。

### 3. 当前页面里为什么对照组和实验组都是暂无

根因不是“完全拿不到数据源”，而是“页面拿错了数据结构”。

`apps/api/server.py` 里 `ab_experiments` 返回的是：

`ABTestManager.diagnostics()`

这个 diagnostics 只包含三项摘要：

1. `active_experiments`: 当前活动实验数量
2. `completed_experiments`: 已完成实验数量
3. `experiment_ids`: 当前活动实验 ID 列表

而前端 `EvolutionPage.tsx` 却把这个对象 `Object.entries(...)` 后直接当成“实验行数据”来渲染，于是就出现了：

1. 实验列：`active_experiments` / `completed_experiments` / `experiment_ids`
2. 对照组：因为 diagnostics 里没有 `control` 字段，所以显示 `暂无`
3. 实验组：没有 `treatment` 字段，所以显示 `暂无`
4. 增益：没有 `lift`，默认 `0`
5. 状态：没有 `status`，所以显示 `未知`

这说明 A/B 后端能力是有一部分的，但当前页面展示的不是实验明细，而是错误地把“诊断摘要”当成“实验列表”了。

### 4. 为什么当前看不到真正的实验明细

这里还有一层更深的原因：

1. `ABTestManager` 当前把活动实验和已完成实验都放在内存里。
2. 这些实验明细没有持久化到 `storage/phase3_evolution`。
3. snapshot 里也只暴露了 diagnostics，没有暴露 `control_id / test_id / pnl / sample / lift / reason_codes` 等详细字段。

所以就算运行中过实验，只要进程重启，很多实验上下文就丢了。

### 5. A/B 设计对项目的意义

这套设计的意义非常大：

1. 避免新模型、新参数直接裸切到 active。
2. 给 `paper -> active` 提供定量依据。
3. 给自动演进提供“收益提升”和“风险变化”的双重判据。
4. 让版本替换从主观判断变成数据驱动。

但就当前版本而言，这块“后端基础已具备，UI 和持久化还不完整”。

---

## 八、每周参数优化器为什么当前是空的

### 1. 这块设计的目的是什么

每周参数优化器的目的，是定期针对 ML 策略重新生成运行时参数工件，而不是完全依赖人工调整。

它主要服务于：

1. 更新阈值文件
2. 更新参数文件
3. 自动产出新的 params 候选
4. 为下一轮演进提供新候选来源

### 2. 具体实现原理是什么

核心逻辑在 `apps/trader/main.py`：

1. 通过 `weekly_optimization_cron` 判断当前是否到调度槽位。
2. 如果到点，调用 `_maybe_start_weekly_ml_params_optimization()`。
3. 启动后台线程 `_run_weekly_ml_params_optimization(slot_id)`。
4. 线程里对目标 ML strategy：
   - 拉取 OHLCV
   - 构建 DataFrame
   - 调 `optimize_params_from_dataframe(...)`
   - 输出运行时工件路径
   - 注册新的 params 候选
5. 结束后通过 `SelfEvolutionEngine.record_weekly_params_optimizer_finish(...)` 把运行状态和审计写入 state store。

### 3. 为什么当前 `runs=[]` 且 `state={}`

这是因为当前 `storage/phase3_evolution` 目录下根本没有：

1. `weekly_params_optimizer_state.json`
2. `weekly_params_optimizer_runs.jsonl`

也就是说，在当前这份状态目录下，这个任务连“开始记录”都没有发生。

如果它曾经启动过，哪怕最后失败，也至少应该留下 state 或 runs 记录。

因此当前最可能的情况是：

1. 运行进程没有正好活到 cron 触发时刻。
2. 这份状态目录是在周任务之前才创建的。
3. 当前进程没有真正进入该调度分支。

### 4. 这块的 cron 配置有无问题

代码和配置里都能看到默认值：

`weekly_optimization_cron: "0 3 * * 0"`

也就是每周日 `03:00 UTC`。

所以这块不是“没配置”，而是“当前状态目录下没有出现过实际触发记录”。

### 5. 对项目的作用和意义

每周参数优化器的意义，在于把“参数调优”变成系统内生能力，而不是完全依赖人手调参。

它的实际价值是：

1. 自动生成新参数候选。
2. 给 Evolution 提供持续的新鲜实验对象。
3. 减少模型和市场环境漂移带来的滞后。
4. 让参数更新能进入正式的候选治理流程，而不是散落在脚本和人工操作里。

---

## 九、当前版本已经实现了什么，还缺什么

### 1. 已经实现并且可用的部分

1. 候选注册表：有。
2. 候选状态机：有。
3. Promotion Gate：有。
4. Retirement Policy：有。
5. 审计日志 `decisions.jsonl`：有。
6. 退役记录机制：有。
7. A/B 管理器核心逻辑：有。
8. 每周参数优化器核心逻辑：有。
9. 进化报告生成器：有。

### 2. 当前版本的关键缺口

1. 当前 active 列表主要来自启动期 baseline 晋升，不是完整自动演进结果。
2. A/B 实验明细没有持久化，页面只能拿到摘要。
3. A/B 表格前端把 diagnostics 误当实验明细渲染。
4. 报告 API 只返回最新一份报告，不是报告历史。
5. 报告前端字段和后端返回结构不匹配。
6. 手动回滚按钮没有对应的后端实现。
7. 每周参数优化器当前还没有在这份状态目录下留下实际运行记录。

---

## 十、哪些问题是可以解决的

下面这些问题是明确可以解决，而且改动边界比较清晰的：

### 1. 进化报告表格显示异常

可以解决。

修复方式：

1. 前端 `EvolutionReport` 类型改成匹配后端真实返回结构。
2. 表格改显示 `report_id / total_candidates / promoted-retired-rollbacks 摘要 / period_end`。
3. 如果要做“历史运行”，后端需要从 `latest_report.json` 扩展成真正的报告历史列表，而不是只返回最新一份。

### 2. A/B 表格显示异常

可以解决。

修复方式：

1. 后端 snapshot 不应只返回 diagnostics，而应返回活动实验列表和已完成实验列表。
2. 每个实验至少要包含：`experiment_id / control_id / test_id / control_samples / test_samples / lift / status / reason_codes`。
3. 若要跨重启可追溯，需要把 completed results 落盘到 state store。

### 3. 手动回滚按钮无效

可以解决，但复杂度比前两个高。

原因是当前前端已经发 `rollback_evolution`，但后端没有这个 action，而且 `SelfEvolutionEngine` 也没有公开的“手动回滚最近一次 family”的对外接口。

要真正实现，需要至少补：

1. 一个明确的 rollback API 动作。
2. 一套确定回滚目标版本的规则。
3. 必要的审计日志落盘。

### 4. 周优化器当前为空

可以解决，但首先要区分“没触发”还是“触发后无目标”。

建议方案：

1. 在 snapshot 中额外暴露 cron 表达式和 next due 信息。
2. 在程序启动时就创建一个初始 state，让页面至少知道它已配置但未运行。
3. 若需要主动验证，可人工触发一次周优化任务并落盘 run 记录。

---

## 十一、针对你这些页面问题的最终判断

### 1. 活跃候选项

当前页面显示的数据是真实存在的，但当前这批 active 主要代表“运行基线”，不是完整自动优选胜出者。

### 2. 最近晋升

当前页面有真实审计数据，但它们主要是初始化基线强制晋升，不应误读为“已经经过完整 A/B 和门禁的自动晋升结果”。

### 3. 最近退役 / 最近回滚

机制已经有，但当前状态目录下没有触发记录，所以为空是合理的。

### 4. 进化报告

后端有报告文件，但前端字段映射错误，因此看起来像“几乎全空”。

### 5. A/B 实验

核心逻辑有，但当前页面不是在展示实验明细，而是在错误展示诊断摘要；同时实验明细没有持久化，所以这块目前只能算“半实现”。

### 6. 每周参数优化器

代码已经具备，但当前状态目录下没有任何运行痕迹，因此当前页面为空不是前端 bug，而是这条任务事实上还没有在这份状态上跑起来。

### 7. 手动回滚按钮

这是当前版本最明确的“前端有入口、后端未实现”的功能缺口。

---

## 十二、建议的后续修复优先级

如果要把 Evolution 工作区真正补完整，建议按下面顺序推进：

1. 先修“进化报告表格”的前后端契约。
2. 再修“A/B 实验表格”，让它展示真实实验明细而不是 diagnostics。
3. 补 `rollback_evolution` 的后端动作，或先移除/禁用该按钮避免误导。
4. 给周优化器增加更明确的运行状态可观测性，并主动触发一次验证。
5. 最后再考虑清理 `candidates.json` 中过多历史 candidate 的展示噪声，例如按 family 折叠或分页。

---

## 十三、与当前本地数据一致的事实摘要

截至本次 review：

1. `storage/phase3_evolution/candidates.json` 统计结果为：`active = 11`、`candidate = 222`。
2. `storage/phase3_evolution/decisions.jsonl` 中最近晋升全部带有 `INITIAL_RUNTIME_BASELINE` 和 `MANUAL_OVERRIDE`。
3. `storage/phase3_evolution/latest_report.json` 存在，但当前是一份空报告 `rpt_7cdf92f5`。
4. `storage/phase3_evolution/scheduler_state.json` 显示只跑过一次空周期，且 `candidates_evaluated = 0`。
5. `storage/phase3_evolution` 目录当前没有 `retirements.jsonl`。
6. `storage/phase3_evolution` 目录当前也没有 `weekly_params_optimizer_state.json` 和 `weekly_params_optimizer_runs.jsonl`。
7. `/api/v1/control` 当前不支持 `rollback_evolution`。

以上就是当前 Evolution 工作区最接近真实实现状态的解释。