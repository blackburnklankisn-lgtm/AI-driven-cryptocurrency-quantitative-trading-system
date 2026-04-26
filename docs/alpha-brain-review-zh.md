# Alpha Brain 工作区代码核查说明

## 1. 结论摘要

`Alpha Brain` 页面当前展示的内容，主要来自两条链路：

1. 市场状态识别链路：`MarketRegimeDetector -> HybridRegimeScorer -> RegimeState`
2. 策略编排链路：`AlphaRuntime -> StrategyOrchestrator -> GatingEngine -> OrchestrationDecision`

再往下叠加一条机器学习辅助链路：

3. 自适应学习链路：`MLPredictor -> ContinuousLearner -> WalkForwardTrainer`

本次代码核查后的核心结论有 6 点：

1. `主导市场状态 = 未知`、`置信度 = 0.0%` 在理论上是允许的，但“概率全是 0.0%”通常不是正常 scorer 输出，更像是快照没有拿到有效 `RegimeState` 时的回退值。
2. `门控动作 = 未知` 并不是 GatingEngine 的合法动作之一，而是 `Alpha Brain` 快照在拿不到最新编排决策时使用的前端/接口回退值。
3. `状态稳定 = 否` 的合法来源有两种：要么最近 5 次 regime 不一致，要么缓存记录还不足 5 条。少于 5 条时，系统也会直接返回 `False`。
4. 页面上“决策链：编排器”并不是后端真实返回的动态字段，而是当前页面写死的表现方式。当前版本并没有把“当前究竟是哪条决策链”做成后端枚举暴露出来。
5. `AI 分析` 目前确实可以接外部 LLM，但只有在环境变量配置了 `GOOGLE_API_KEY` 时才会触发；未配置时会一直停留在 `Waiting for AI analysis...`。
6. `持续学习器` 当前是已实现且已接入的。它会先加载本地已有模型作为 `init_loaded`，然后在满足定时、性能退化、概念漂移三类触发条件时重训，并在条件满足时热替换线上模型和阈值。

## 2. `Alpha Brain` 页面各区域的数据从哪里来

当前页面展示逻辑位于：

- `apps/desktop-client/src/pages/AlphaBrainPage.tsx`

后端快照构建位于：

- `apps/api/server.py::_build_alpha_brain_snapshot()`

这个快照的字段来源大致如下：

1. `dominant_regime / confidence / regime_probs`
   - 来自 trader 运行时缓存的 `_latest_regime_state`，若为空则回退 `_current_regime`

2. `is_regime_stable`
   - 来自 trader 运行时缓存的 `_latest_regime_stable`

3. `orchestrator.gating_action / weights / block_reasons / selected_results`
   - 来自 `_latest_orchestration_decision`

4. `continuous_learner`
   - 来自 `_continuous_learners` 中每个 learner 的 `get_model_version_info()` 与 `get_optimal_thresholds()`

5. `ai_analysis`
   - 来自 `_last_ai_analysis`

也就是说，这个页面不是直接去算一次市场状态，而是读 trader 在运行过程中维护好的实时缓存。

## 3. 主导市场状态为什么会显示“未知”，置信度为什么是 0.0%

### 3.1 这些字段的数据来源

页面中的：

- `主导市场状态`
- `置信度`
- `牛市 / 熊市 / 震荡 / 高波动 概率`

本质上都来自同一个对象：`RegimeState`

`RegimeState` 定义了这些字段：

- `bull_prob`
- `bear_prob`
- `sideways_prob`
- `high_vol_prob`
- `confidence`
- `dominant_regime`

其中 `dominant_regime` 的理论合法类别只有 5 个：

1. `bull`
2. `bear`
3. `sideways`
4. `high_vol`
5. `unknown`

页面中文分别对应：

1. 牛市
2. 熊市
3. 震荡
4. 高波动
5. 未知

### 3.2 RegimeState 是怎么计算出来的

真实运行链路是：

1. 主循环收到新 K 线事件
2. 取该 symbol 的 `DataKitchen regime_features`，若没有则回退原始 OHLCV
3. 调用 `MarketRegimeDetector.update_from_frame(...)` 或 `update(...)`
4. detector 先通过 `RegimeFeatureSource` 提取结构化特征
5. 再交给 `HybridRegimeScorer.score(...)` 输出 `RegimeState`
6. 最后缓存到 `_current_regime`、`_symbol_regimes[event.symbol]`、`_latest_regime_state`

### 3.3 它依赖哪些输入数据

Regime 计算主要使用下面这类特征：

1. 收益率特征
   - `return_1`
   - `return_5`
   - `return_20`
   - `ret_roll_mean_20`
   - `ret_roll_std_20`

2. 趋势特征
   - `price_vs_sma20`
   - `price_vs_sma50`
   - `adx`

3. 动量特征
   - `rsi_14`

4. 波动率特征
   - `atr_pct`
   - `bb_width`

5. 成交量特征
   - `volume_ratio`

这些数据要么来自：

1. `DataKitchen` 已经计算好的 `regime_features` 视图
2. 或者直接来自最近一段 OHLCV 数据，再由 `RegimeFeatureSource` 现场计算

### 3.4 scorer 如何决定当前是什么市场状态

`HybridRegimeScorer` 的逻辑不是一个黑盒深度模型，而是可解释的“规则 + 统计”混合模型：

1. 先计算高波动分数
   - 依据 `ATR%`、`布林带宽度`、`收益率标准差`

2. 再计算趋势方向分数
   - 依据 `ADX`、价格偏离 `SMA20/SMA50` 的方向与幅度

3. 再用动量修正
   - 依据 `RSI` 和 `ret_roll_mean_20`

4. 再组合成四个概率
   - `bull_prob`
   - `bear_prob`
   - `sideways_prob`
   - `high_vol_prob`

5. 取最高概率作为 `dominant_regime`

6. 再用“最高概率 - 第二高概率”的差值作为 `confidence`

### 3.5 为什么会出现 `unknown`

`unknown` 有两种来源：

#### A. 合法的 `unknown`

当以下情况出现时，detector/scorer 本来就会返回 `unknown`：

1. K 线数量不足
   - `RegimeFeatureSource` 至少需要 30 根 bar 才能产出有效特征

2. 特征无效
   - 特征构建失败
   - regime_features 为空
   - OHLCV 不足或异常

3. 虽然四类概率算出来了，但 `confidence < 0.45`
   - 这是 `HybridRegimeScorer` 的 `confidence_floor`
   - 也就是说，系统认为“虽然有最大类，但最大类优势不够明显”，于是把 dominant 强制降级成 `unknown`

#### B. 非理想的 `unknown`

如果页面上看到：

- `dominant_regime = unknown`
- `confidence = 0.0`
- 并且四类概率都是 `0.0%`

那么这往往不是 scorer 的正常输出。

原因是：

1. scorer 在合法 unknown 场景下，返回的是四类概率各 `0.25`
2. 页面应该显示成 `25.0% / 25.0% / 25.0% / 25.0%`
3. 如果四类都变成 `0.0%`，更像是快照根本没拿到有效 `regime` 对象，只能对 `None` 做字段回退

因此：

- `unknown + 0.0 confidence` 是可能合理的
- `四类概率全 0.0%` 通常不应被理解成模型真实评分结果，而应优先怀疑快照拿到的是空对象或早期错误版本的字段接线

## 4. 门控动作为什么会显示“未知”，状态稳定为什么会显示“否”

### 4.1 门控动作的数据来源

页面里的 `门控动作` 来自：

- `snapshot.orchestrator.gating_action`

后端取值方式是：

- 如果 `_latest_orchestration_decision.gating.action` 存在，就取它的 `.value`
- 否则回退为字符串 `"unknown"`

所以 `门控动作 = 未知` 并不是 GatingEngine 真正产出的门控动作类别，而是“当前没有最新编排决策对象可读”的接口回退值。

### 4.2 为什么会拿不到 gating_action

常见原因有 3 类：

1. 启动后尚未处理到第一根有效 K 线
   - `_latest_orchestration_decision` 还没被回填

2. AlphaRuntime 这轮没成功跑到 orchestrator
   - 例如上游流程异常

3. 使用的是早期版本快照
   - 早期代码曾存在“运行中有 decision，但快照侧读不到或没缓存好”的问题

### 4.3 GatingEngine 合法动作有哪些

当前实现里，合法的门控动作只有 4 类：

1. `ALLOW`
2. `REDUCE`
3. `BLOCK_BUY`
4. `BLOCK_ALL`

所以理论上页面应显示的动作类别只有这 4 类，而不是 `未知`。

### 4.4 各类门控动作分别代表什么

1. `ALLOW`
   - 正常放行
   - 策略结果可继续进入后续编排和风控链

2. `REDUCE`
   - 不是“禁止交易”
   - 而是把仓位/权重缩小
   - 在 orchestrator 里通过 `reduce_factor` 作用到权重

3. `BLOCK_BUY`
   - 禁止新的 BUY 信号
   - HOLD / 平仓不一定被禁止

4. `BLOCK_ALL`
   - 阻断全部方向性信号
   - 本质上是“不给新的 BUY/SELL 通过”

因此“阻断”在这里主要是阻断策略方向信号进入后续执行链，不是简单理解为“整个系统停机”。

### 4.5 门控动作的触发条件

当前 GatingEngine 的核心规则如下：

1. `dominant_regime == unknown`
   - 触发 `regime_unknown`
   - 默认动作：`REDUCE`
   - 缩减系数：`0.5`

2. `confidence < 0.25`
   - 触发 `regime_very_low_confidence`
   - 动作：`BLOCK_ALL`

3. `0.25 <= confidence < 0.45`
   - 触发 `regime_low_confidence`
   - 动作：`REDUCE`
   - 缩减系数：`0.6`

4. `dominant_regime == high_vol` 且 `confidence >= 0.5`
   - 触发 `high_vol_block_buy`
   - 动作：`BLOCK_BUY`

5. `is_regime_stable == False`
   - 触发 `regime_unstable`
   - 动作：`REDUCE`

### 4.6 门控动作的作用和意义

门控不是为了替代风控，而是为了在“市场环境本身不清晰或不适合当前策略”时，先做一层环境级降级。

它的意义主要有 3 点：

1. 避免在识别不清的 regime 下强行执行信号
2. 在高波动或低置信环境下缩小仓位，而不是简单二元开/不开
3. 让后续权重计算和冲突解决建立在一个更稳妥的环境判断上

### 4.7 状态稳定为什么会显示“否”

`状态稳定` 的判断依据来自 `RegimeCache.is_stable(window=5)`。

规则非常明确：

1. 最近 5 条 regime 记录必须都存在
2. 并且这 5 条记录的 `dominant_regime` 必须完全一致

只有满足这两点才返回 `True`。

所以显示 `否` 可能有 3 种原因：

1. 最近 5 次 dominant 确实发生了切换
2. 最近不足 5 条有效记录，系统直接返回 `False`
3. 上游仍然在 unknown/低置信来回摆动

也就是说，`否` 不一定表示“模型坏了”，也可能只是“刚启动，还没积累满 5 次稳定判断窗口”。

## 5. 市场状态概率分布为什么会显示全 0.0%

### 5.1 正常情况下，这四个概率怎么来

四个概率来自 `HybridRegimeScorer.score(features)`。

它会输出：

1. `bull_prob`
2. `bear_prob`
3. `sideways_prob`
4. `high_vol_prob`

这些值先根据波动率、趋势、动量打分，再归一化到概率空间，最后再加一个概率下限，避免硬零。

### 5.2 当前版本下，理论上几乎不应该出现“四个都是 0”

原因是：

1. scorer 有 `prob_floor = 0.05`
2. 即使出现 unknown，内部 `_unknown_state()` 也会返回 `0.25 / 0.25 / 0.25 / 0.25`

所以严格来说：

- “四个概率都显示 0.0%”不符合 scorer 的正常输出特征

### 5.3 这通常说明什么

这通常说明不是 scorer 真算成了 0，而是页面展示时没有拿到真实 `RegimeState`。

典型原因包括：

1. 快照里 `regime` 对象为空
2. 读取了早期错误字段
3. 页面在 trader 尚未进入有效状态前就先渲染了空回退值

结论是：

- 如果看到 `25/25/25/25 + confidence 0.0 + dominant unknown`，这是可以接受的合法 unknown
- 如果看到 `0/0/0/0`，优先应判断为“快照未取到有效 regime 对象”，而不是把它理解成算法真实输出

## 6. 决策链为什么显示“编排器”，为什么权重为空，为什么阻断原因为空

### 6.1 “决策链：编排器”其实是页面写死的

当前 `AlphaBrainPage.tsx` 里，“编排器”只是一个分区标题，不是后端返回的 `decision_chain` 字段。

所以：

- 当前页面显示“编排器”，并不代表后端真的定义了一个正式字段叫“当前决策链 = 编排器”
- 它只是当前 UI 默认把 `Phase 1 StrategyOrchestrator` 当作本页的核心决策链来展示

### 6.2 当前系统里实际存在哪些决策路径

如果从运行入口来看，当前系统至少有 4 条“会产生或推动订单”的路径：

1. `phase1`
   - `AlphaRuntime -> StrategyOrchestrator -> _process_order_request`
   - 这是 `Alpha Brain` 页真正对应的主链路

2. `legacy_fallback`
   - 当 AlphaRuntime 异常时，直接回退成逐个策略 `on_kline()` 调用

3. `phase3_rl`
   - Phase3 RL 决策直接构造订单请求，再进入 `_process_order_request`

4. `portfolio_rebalancer`
   - 组合再平衡单独走再平衡提交流程

所以“系统中存在多少种决策链”如果按当前代码路径理解，至少可以识别出以上 4 类。

但当前 `Alpha Brain` 页面只覆盖其中第 1 类，不会把其他链路动态显示出来。

### 6.3 为什么权重显示“暂无权重数据”

权重来自：

- `_latest_orchestration_decision.weights`

只有在以下条件满足时，权重才会非空：

1. `AlphaRuntime` 本轮确实产出了 `strategy_results`
2. orchestrator 成功执行
3. 有策略结果通过门控/冲突解决，进入 `resolved_results`
4. 权重计算后 `weights` 非空

因此显示为空通常有 3 类原因：

1. 本轮根本没有策略信号
2. 策略有信号，但都被 `BLOCK_ALL` 或冲突解决过滤掉了
3. 快照侧没有拿到最新 decision

### 6.4 为什么阻断原因显示“无阻断原因”

阻断原因来自：

- `orchestrator.block_reasons`

这个列表为空，不等价于“系统什么都没发生”。

它只表示：

1. 本轮 orchestrator 没记录到任何 block reason
2. 或者当前根本没有可读的最新 orchestrator decision

### 6.5 orchestrator 会记录哪些阻断/降级原因

常见来源包括：

1. `regime_unknown`
2. `regime_very_low_confidence`
3. `regime_low_confidence`
4. `high_vol_block_buy`
5. `regime_unstable`
6. 冲突解决导致的 loser suppression
7. 历史表现折扣
8. 高回撤导致的全局权重缩减

### 6.6 “阻断”到底阻断了什么

这里的“阻断”主要阻断的是：

- 策略方向信号进入后续执行链

不是说：

- WebSocket 被阻断
- 系统停机
- 风控熔断等同于 orchestrator 阻断

要注意区分：

1. orchestrator 阻断
   - 环境和编排层面的信号降级/屏蔽

2. risk_manager / kill_switch / adaptive_risk 拒单
   - 风控和执行层面的拒绝

当前版本的 `Alpha Brain` 页面主要展示的是第 1 类，不是第 2 类。

## 7. 自适应机器学习区域应该如何理解

### 7.1 “当前模型：AI分析”并不是准确的系统语义

从页面实现看，当前页面实际上并没有把“模型类型/模型名称”单独展示出来。

它展示的是两块并列内容：

1. 左侧 `持续学习器`
   - 版本
   - 最近重训时间
   - 学习器数量
   - 阈值

2. 右侧 `AI 分析`
   - `_last_ai_analysis` 文本

所以如果界面视觉上让人理解成“当前模型 = AI分析”，那是 UI 表达不够清晰，不是后端真的把模型名写成了 AI 分析。

当前版本还没有把：

- `model_type`
- `trainer_model_type`
- `threshold_source`
- `model_path`

这些信息直接暴露到 `Alpha Brain` 页面。

### 7.2 当前是否接了 LLM

当前代码里，确实接了外部 LLM 分析能力。

接入方式：

1. 读取环境变量 `GOOGLE_API_KEY`
2. 如果存在，则导入 `google.generativeai`
3. 使用模型：`gemini-1.5-flash`
4. 每小时整点附近触发一次 `_run_ai_analysis()`

Prompt 的内容也比较简单：

1. 固定 symbol：`BTC/USDT`
2. 读取最近 10 根 K 线的 close 序列
3. 让 Gemini 用一小段文字解释当前市场情绪和风险提示

### 7.3 如果没有配置 API Key，会发生什么

如果没有配置 `GOOGLE_API_KEY`：

1. `_run_ai_analysis()` 直接 return
2. 不会调用任何外部 LLM
3. `_last_ai_analysis` 会一直保持初始化值：
   - `Waiting for AI analysis...`

所以页面显示这句话，通常意味着：

- 当前没有配置有效的 Gemini API Key
- 或者定时调用尚未跑到、或调用失败

### 7.4 API Key 是什么

代码里只明确了：

- 使用的环境变量名称是 `GOOGLE_API_KEY`

但实际 secret 值不在仓库代码中，也不应该在核查文档里展开。

结论应理解为：

1. 当前支持接 Gemini
2. 需要用户在环境中配置 `GOOGLE_API_KEY`
3. 代码审查无法也不应该给出真实密钥值

## 8. 最近重训、版本历史、训练原理应该如何理解

### 8.1 持续学习器多久训练一次

当前默认配置位于 `configs/system.yaml`：

- `retrain_every_n_bars = 500`
- `min_accuracy_threshold = 0.55`
- `drift_significance = 0.05`
- `drift_check_window = 100`
- `min_bars_for_retrain = 400`

因此重训不是“固定按时钟每多久一次”，而是按 K 线数量和状态触发：

1. 定时触发
   - 每累计 500 根 bar 触发一次

2. 性能退化触发
   - 近期预测精度低于 `0.55`

3. 概念漂移触发
   - 对近期 vs 参考期特征做 KS 检验
   - 若显著漂移特征比例超过阈值，则触发重训

此外，在样本数不足 `400` 根 bar 时，不会触发重训。

### 8.2 它是如何训练的

`ContinuousLearner` 的训练核心不是在线 SGD，而是周期性重做一轮 `WalkForwardTrainer.train(...)`。

训练流程大致是：

1. 把当前 OHLCV 缓冲区转成 DataFrame
2. 构建 ML 特征矩阵
3. 生成监督学习标签
4. 做简化版 Walk-Forward 训练
   - `n_splits = 3`
   - `test_size = max(50, len(df)//10)`
   - `min_train_size = max(150, len(df)//4)`

5. 计算 OOS 指标
   - `accuracy`
   - `f1`
   - 等

6. 计算自适应阈值
   - `optimal_buy_threshold`
   - `optimal_sell_threshold`

7. 生成新版本 ID，并保存模型
8. 与当前活跃版本比较
9. 如果新版本显著更优，则切换活跃模型

### 8.3 它的训练原理是什么

本质上是：

1. 用历史 K 线构造监督学习特征
2. 用未来收益/方向生成标签
3. 用 Walk-Forward 做时序外样本验证
4. 用 OOS 指标判断新模型质量
5. 用自适应阈值把概率输出转成买卖信号

所以这不是单纯“再拟合一次”，而是一个“时序验证 + 阈值校准 + 版本切换”的闭环。

### 8.4 `init_loaded -> 20260425_082954_d6bf18` 是什么意思

这说明至少发生了两件事：

1. 启动时先把本地已有模型加载为初始活跃版本 `init_loaded`
2. 后续 learner 又训练出了一个新版本 `20260425_082954_d6bf18`

因此，回答你的问题：

- 是的，这证明至少已经训练过一版新的 learner 版本，而不只是“加载了本地旧模型”

### 8.5 最近一版训练的东西在哪里看

从代码逻辑看，重训后的模型会保存到：

- `./models/model_<version_id>.pkl`

因此你可以从两个位置核查：

1. 运行时页面/接口
   - learner `active_version`
   - `get_model_version_info()`

2. 文件系统
   - `models/` 目录中的 `model_*.pkl`

当前工作区里实际已经能看到多份训练产物，例如：

- `models/model_20260425_082954_d6bf18.pkl`
- `models/model_20260425_165006_15be7b.pkl`

需要注意：

- 文件系统里“最新生成的模型文件”不一定等于“当前运行中的 active_version”
- 页面显示的是该运行进程内 learner 当前认定的活跃版本

### 8.6 训练效果体现在哪里

训练效果主要体现在 4 个地方：

1. 新模型的 OOS 指标是否更好
2. 新模型是否被切换为 active
3. 预测信号是否随新模型变化
4. buy/sell 阈值是否被重新校准并注入 predictor

也就是说，训练的效果不是单独看“生成了个文件”，而是看：

- 是否完成模型切换
- 是否完成阈值更新
- 是否让后续信号和收益表现更优

## 9. `buy: 0.6000 / sell: 0.4000` 代表什么，有什么作用

### 9.1 这两个数值的含义

对 `MLPredictor` 来说，模型输出的是“买入概率” `buy_proba`。

然后通过这两个阈值把概率映射成交易动作：

1. 当 `buy_proba >= buy_threshold`
   - 且当前没有持仓
   - 触发 BUY

2. 当 `buy_proba <= sell_threshold`
   - 且当前已经持仓
   - 触发 SELL

因此：

- `buy_threshold = 0.6000` 表示只有当模型给出的买入概率至少达到 60% 才允许开多
- `sell_threshold = 0.4000` 表示如果模型买入概率跌到 40% 以下，且已持仓，则允许卖出平仓

### 9.2 为什么不是 0.5 / 0.5

因为系统不是按“概率大于 50% 就买”这样简单处理。

更保守的阈值意味着：

1. 开仓需要更高置信度
2. 平仓允许更早离场
3. 形成一个“中间缓冲带”
   - 0.4 到 0.6 之间保持观望/不动作

### 9.3 这两个阈值从哪里来

当前阈值来源优先级是：

1. `models/<symbol>_threshold.json`
2. `models/threshold_v1.json`
3. `models/registry.json` 中与当前模型版本对应的推荐阈值
4. 如果都没有，则回退默认值 `0.60 / 0.40`

而在持续学习重训完成后：

1. `WalkForwardTrainer` 会根据各折 OOS 结果计算 `optimal_buy_threshold`
2. `optimal_sell_threshold` 通常取 `1 - buy_threshold`，并带下限保护
3. learner 更新自己的 `_optimal_buy_threshold / _optimal_sell_threshold`
4. `_on_model_updated()` 再把这两个阈值热注入到 predictor 的 `set_thresholds()`

### 9.4 这两个阈值的作用

它们的作用有 3 个：

1. 把模型的连续概率输出变成离散交易动作
2. 控制开仓的置信门槛，减少低质量入场
3. 与卖出阈值一起形成迟滞区间，降低频繁反复开平仓

## 10. 当前版本有哪些已知缺口，可以怎么解决

### 10.1 已知缺口 A：`决策链` 不是动态字段

当前页面只是把“编排器”写死在 UI 上，并没有后端真实字段告诉前端当前链路究竟是：

- `phase1`
- `legacy_fallback`
- `phase3_rl`
- `portfolio_rebalancer`

结论：当前版本这部分没有完整实现。

可行修复方向：

1. 在 `Alpha Brain` snapshot 中新增 `decision_chain`
2. 用运行时上下文明确当前链路来源

### 10.2 已知缺口 B：页面没有直接展示模型类型和工件来源

当前页面能看到：

- 版本号
- 重训时间
- learner 数量
- 阈值

但看不到：

- `model_type`
- `model_path`
- `threshold_source`
- `params_source`

结论：当前版本这部分也没有完整实现。

可行修复方向：

1. 在 `continuous_learner` 快照中增加 `model_type`
2. 增加 `model_path`
3. 增加 `threshold_source / params_source`

### 10.3 已知缺口 C：`AI analysis` 与 `当前模型` 容易产生视觉歧义

当前实现中，AI 文本分析和模型版本信息并排显示，但没有明确语义分隔，容易让人误读成“AI分析就是当前模型”。

结论：这是 UI 表达问题，不是底层模型系统真的混在一起。

可行修复方向：

1. 把 `AI 分析` 区块更明确命名为 `LLM 市场解读`
2. 把 `当前模型` 区块增加模型类型和版本详情

## 11. 核查结论

基于当前代码，可以把你的问题归纳为下面这些判断：

1. `主导市场状态 = unknown`、`置信度 = 0.0%` 可以是合法结果，但如果四类概率同时为 0.0%，更可能是快照对象为空而非算法真实输出。
2. `门控动作 = 未知` 不是合法 gating 分类，而是“当前拿不到最新编排决策”的回退值。
3. `状态稳定 = 否` 不只表示状态切换频繁，也可能只是最近不足 5 次 regime 记录。
4. 页面上的“决策链：编排器”目前是静态展示，不是后端真实动态字段。
5. `AI analysis` 目前确实接了 Gemini，但前提是配置了 `GOOGLE_API_KEY`；否则只会显示 `Waiting for AI analysis...`。
6. `持续学习器` 当前已经实现并接入，`init_loaded -> 新版本` 的版本历史，说明系统确实已经完成过至少一次新的 learner 训练/版本产出。
7. `buy=0.6000 / sell=0.4000` 不是随便写死给页面看的数字，而是 ML 信号离散化的核心阈值；它既可能来自默认值，也可能来自阈值工件或重训后的自适应校准结果。

如果下一步继续增强 `Alpha Brain` 页面，我建议优先做 3 项：

1. 增加动态 `decision_chain` 字段
2. 增加 `model_type / model_path / threshold_source`
3. 把 `AI analysis` 与 `当前模型` 视觉拆分得更明确