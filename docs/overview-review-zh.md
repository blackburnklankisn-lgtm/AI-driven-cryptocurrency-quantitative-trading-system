# 总览页面代码核查说明

## 1. 结论摘要

本次针对“总览”页面的关键字段做了代码级核查，结论分两类：

1. 有些值是当前运行状态下的合理结果。
2. 也有几项值并不是策略真的输出了“未知”或“没有数据”，而是总览快照读取了错误的运行时字段，导致前端拿到的是回退值。

已经确认并修复的总览接线问题有：

- `dominant_regime / regime_confidence / is_regime_stable` 原本没有稳定读取到真实运行中的最新 regime 缓存。
- `feed_health` 原本优先读的是不存在或未使用的 `_subscription_manager`，而当前运行路径实际使用的是 `_phase3_subscription_manager`。
- `strategy_weight_summary` 原本依赖 `_latest_orchestration_decision`，但运行主循环里没有把最新编排结果缓存进去，因此常年为空。

因此，用户之前看到的以下几项：

- 主导市场状态：未知
- 置信度：0.0%
- 稳定：否
- 数据源健康：状态未知
- 策略权重汇总：暂无编排权重数据

其中至少前四项和权重项，不能直接理解为“策略真实判断就是未知”，而要区分是否为快照字段接错。

## 2. 总览各字段的真实来源与含义

### 2.1 主导市场状态为什么会显示“未知”

总览快照本应展示当前最新的 `RegimeState.dominant_regime`。

运行时真实的市场状态更新路径是：

- 主循环收到 K 线事件。
- 调用 `MarketRegimeDetector.update(...)` 或 `update_from_frame(...)`。
- 返回 `RegimeState`。
- 写入 `self._current_regime`，并按 symbol 写入 `self._symbol_regimes[event.symbol]`。

也就是说，运行时真正被持续更新的是 `_current_regime` 和 `_symbol_regimes`。

但是原来的总览快照读的是 `_latest_regime_state`。如果这个字段没有被正确回填，就会退回到默认值，最终前端看到的就是 `unknown`。

需要注意两种“未知”来源：

1. 合理的 unknown
   - `MarketRegimeDetector` 在样本不足或特征无效时，确实会返回 `dominant_regime="unknown"`、`confidence=0.0`。
   - 例如冷启动、历史 K 线不足、特征未构造成功时，这种 unknown 是正常的。

2. 非合理的 unknown
   - 这次总览页面里，更大的问题是快照读错了字段。
   - 即使运行时已经在 `_current_regime` 里拿到了状态，总览也可能因为读的是未更新字段而显示 unknown。

本次已修复为优先读取真实最新缓存。

### 2.2 为什么置信度会是 0.0%

前端显示的置信度来自 `RegimeState.confidence`。

这个值理论上有两种来源：

1. 检测器真实输出了 0.0
   - 在 `RegimeState` 默认定义中，`confidence=0.0`。
   - `MarketRegimeDetector` 冷启动或特征无效时会返回 `_UNKNOWN_REGIME`，其置信度就是 `0.0`。

2. 总览读到了错误字段后回退成 0.0
   - 原总览代码使用 `_latest_regime_state`，没有拿到对象时会把 confidence 回退成 `0.0`。

因此，之前看到的 `0.0%` 不能简单判定为“模型确信市场未知”，更可能是快照字段没接到真实运行态。

### 2.3 为什么“稳定”显示“否”

`稳定` 语义上对应最近若干根 bar 的 regime 是否保持一致。

`MarketRegimeDetector.is_stable` 的判断逻辑是：最近 5 根 bar 的 `dominant_regime` 是否一致。

所以“否”可能有两种情况：

1. 真实不稳定
   - 最近 5 根 bar 的 regime 在切换。
   - 这种情况下显示“否”合理。

2. 总览读取路径错误
   - 原总览读的是 `trader._regime_detector.is_stable`。
   - 但当前运行结构里，regime detector 实际是按 symbol 维护在 `_phase1_regime_detectors` 中，并不是单一的 `_regime_detector`。
   - 这就导致总览容易直接回退为 `False`。

本次已修复为在主循环中缓存最新一次的稳定性，并由总览直接读取该缓存。

### 2.4 为什么风险等级显示“正常”

这个字段当前实现非常简单，不是一个综合风险引擎评分，而只是总览的轻量级展示逻辑。

当前规则是：

- 如果组合熔断已触发，则 `critical`
- 否则如果回撤 `drawdown_pct >= 0.1`，则 `elevated`
- 其他情况一律 `normal`

因此，“风险等级：正常”只说明：

- 当前没有触发组合熔断
- 当前回撤没有达到 10%

它并不代表：

- 数据源健康一定正常
- 市场 regime 一定稳定
- 买入条件一定允许
- adaptive risk 一定会放行

也就是说，这个字段目前只是“组合层面简化风险灯”，不是全链路风险判定。

### 2.5 为什么会出现“数据源未知 / 重连 0 / 健康未知”

总览里的 `feed_health` 理论上应该来自实时订阅管理器 `SubscriptionManager.diagnostics()`。

当前系统在运行时实际使用的是：

- `self._phase3_subscription_manager`

其 `diagnostics()` 能返回：

- `health`
- `exchange`
- `subscribed_symbols`
- `reconnect_count`
- `last_heartbeat_at`
- `ws_state`

所以如果系统已经启动了 realtime feed，正常不应该长期显示 `unknown`。

之前之所以显示 `未知 / 重连 0`，主要原因是总览快照读取的是：

- `_subscription_manager`

而不是当前真实使用的：

- `_phase3_subscription_manager`

这属于快照实现问题，不是订阅管理器本身没实现。

另一个需要区分的点：

- 如果 phase3 realtime feed 本来就没启动，或者启动失败，显示 unknown 是合理的。
- 但当前 `configs/system.yaml` 中 `phase3.enabled=true`、`realtime_feed_enabled=true`、`provider=htx`，所以在当前配置下，如果系统启动正常，总览更应该展示真实 feed 诊断信息，而不是 unknown。

本次已修复为优先读取真实启用中的 `_phase3_subscription_manager`。

### 2.6 为什么“告警：暂无活动告警”

当前总览页面里的 `alerts` 实现非常有限，只做了两个来源：

1. 组合熔断原因
   - 如果 `risk_manager.is_circuit_broken()` 为真，则把 `circuit_reason` 放进 alerts。

2. 数据源退化
   - 如果 `feed_health.health == "degraded"`，则追加一条 `Feed degraded`。

所以目前总览页真正实现的告警类型只有 2 类，不是一个完整告警中心。

这意味着很多用户以为应该提示的情况，目前其实不会在总览里出现，例如：

- regime unknown
- regime 低置信度
- regime unstable
- adaptive risk 拒单
- budget 不足
- kill switch 激活
- 某个策略持续无信号
- 某个策略被 gating block

这些信息在系统里有些已经存在于内部决策或日志里，但当前总览快照并没有把它们整理成 alerts 返回给前端。

所以“暂无活动告警”只能解释为：

- 当前没有触发组合熔断
- 当前 feed_health 也没有被判成 degraded

它不代表系统所有层面都没有问题。

### 2.7 为什么持仓为 0 是合理的

`持仓（0）` 来自运行时的 `_positions` 汇总。

如果当前还没有任何订单真正走到成交/记账阶段，那么持仓为 0 完全合理。

尤其本系统的买单触发并不是“策略有 BUY 信号就一定成交”，中间还有多层过滤：

1. 策略先产出 `OrderRequestEvent`
2. Alpha Brain 编排器做 regime/gating/冲突解决/权重计算
3. `_process_order_request()` 再做 kill switch、adaptive risk、risk plan、budget、risk manager 校验
4. 只有全部通过的请求才会提交到下单层

所以只要这些链路里任意一层还没有放行，持仓仍然会保持 0。

### 2.8 为什么“策略权重汇总”为空

`strategy_weight_summary` 来自编排器的 `decision.weights`。

理论上该字段在以下情况下会有值：

- 某根 bar 上至少有一个或多个策略产出了可编排的结果
- `StrategyOrchestrator.orchestrate(...)` 被调用
- 返回的 `OrchestrationDecision.weights` 非空
- 总览快照能拿到最新一次 `OrchestrationDecision`

之前为空的原因有两种：

1. 合理为空
   - 当前没有策略信号
   - 或者策略都被过滤掉了
   - 或者没有产生任何需要编排的 directional result

2. 实现导致为空
   - 总览读取 `_latest_orchestration_decision`
   - 但主循环里原先没有把最新 `decision` 缓存到这个字段
   - 这就导致总览长期拿不到最新编排权重

本次已在主循环中补充最新编排决策缓存，因此只要确实有策略结果，`strategy_weight_summary` 就有机会正常显示。

## 3. 买入 / 卖出到底在什么条件下触发

这一部分需要分为“策略层信号触发”和“最终下单触发”两层理解。

### 3.1 策略层信号触发

当前默认注册的策略包括：

1. MA Cross 策略
2. Momentum 策略
3. 如果 `models/` 下存在已训练模型，还会额外注册 ML Predictor 策略

#### 3.1.1 MA Cross 默认触发条件

针对每个默认 symbol（`BTC/USDT`、`ETH/USDT`、`SOL/USDT`），系统会注册：

- `fast_window=10`
- `slow_window=30`
- `use_ema=True`
- `adx_filter=True`
- `volume_filter=True`

买入条件：

- 快线金叉慢线
- 当前未持仓
- 成交量过滤通过
- ADX 通过开仓阈值（默认 25）

卖出条件：

- 快线死叉慢线
- 当前持有多头
- 卖出不受 ADX 限制

也就是说，MA Cross 不是只看均线交叉，还叠加了趋势强度和成交量确认。

#### 3.1.2 Momentum 默认触发条件

针对每个默认 symbol，系统会注册：

- `roc_window=10`
- `roc_entry_pct=2.0`
- `rsi_window=14`
- `rsi_upper=70`
- `rsi_lower=30`

买入条件：

- 当前未持仓
- `ROC > 2.0%`
- `RSI < 70`

卖出条件：

- 当前持仓
- `ROC < -2.0%`
- `RSI > 30`

它本质上是“上涨动量够强且不过热才买，反向动量够强且不极端超卖才卖出平仓”。

#### 3.1.3 ML Predictor 条件

如果本地 `models/` 下存在对应模型文件，系统会额外注册 ML Predictor。

其触发门槛依赖：

- 运行时工件中的 `buy_threshold`
- 运行时工件中的 `sell_threshold`
- 如果工件缺失，则回退到配置默认阈值

因此 ML 策略的买卖不是写死的固定百分比，而是取决于当前模型及其阈值工件。

### 3.2 编排层与风险层的放行条件

即使策略已经发出 BUY/SELL，最终也未必能下单。

真实链路如下：

1. `strategy.on_kline(event)` 先产出 `OrderRequestEvent`
2. `AlphaRuntime.process_bar(...)` 汇总各策略输出
3. `StrategyOrchestrator.orchestrate(...)` 执行：
   - regime gating
   - 冲突解决
   - 高波动做多限制
   - 权重归一化
   - block reason 汇总
4. 对于被选中的结果，才进入 `_process_order_request(...)`
5. `_process_order_request(...)` 再执行：
   - kill switch 检查
   - adaptive risk 检查
   - risk plan 检查
   - budget 检查
   - risk manager 检查
6. 只有全部通过后，订单才会真正提交

因此“策略触发了买点”和“账户里真的出现仓位”之间，还隔着完整的编排和风控链。

## 4. Regime / Gating 对触发的影响

编排器对 regime 的要求很明确，不是所有 regime 都会正常放行。

当前 gating 逻辑包含以下关键规则：

1. `dominant_regime == "unknown"`
   - 触发 `regime_unknown`
   - 根据配置执行 unknown regime 动作，通常是阻断或降权

2. `confidence` 极低
   - 触发 `regime_very_low_confidence`
   - 直接 `BLOCK_ALL`

3. `confidence` 偏低
   - 触发 `regime_low_confidence`
   - 进入 `REDUCE`

4. `high_vol` 且置信度达到阈值
   - 触发 `high_vol_block_buy`
   - 禁止做多

5. regime 不稳定
   - 触发 `regime_unstable`
   - 进入 `REDUCE`

这说明即使策略本身给了 BUY，编排层也可能因为 regime unknown、低置信度、不稳定或高波动直接压制信号。

## 5. 如果某些功能当前未实现，原因是什么，能不能解决

### 5.1 已确认未完全实现的部分

#### A. 总览告警体系并不完整

当前只实现了：

- 熔断告警
- feed degraded 告警

没有把内部大量已有状态整理成用户可读的 alerts。

这不是底层没有数据，而是总览快照没有展开这些数据。

结论：可以解决。

可行方向：

- 把 gating 的 `triggered_rules`
- adaptive risk 的 block reasons
- risk plan 的阻断原因
- kill switch 状态
- feed stopped / reconnect 异常次数

一起整理成结构化 alerts 返回前端。

#### B. 风险等级语义过于粗糙

当前只看熔断和回撤，无法反映更完整的实时风险状态。

结论：可以解决。

可行方向：

- 将 `kill_switch_active`
- `budget_remaining_pct`
- `daily_loss_pct`
- `feed_health`
- `regime_confidence`

纳入综合评分，形成更贴近交易现实的风险等级。

#### C. 总览不是完整交易解释器

当前总览只给结果快照，不给“为什么没有下单”的解释闭环。

结论：可以解决。

可行方向：

- 增加最近一次被 block 的 strategy_id
- 最近一次 block reason
- 最近一次 risk rejection stage
- 最近一次 allowed signal / submitted order

这样前端就能直接解释“为什么现在没仓位”。

### 5.2 本次已经直接修复的问题

本次已经落地修复：

1. 总览 regime 读取路径修正
2. 总览稳定性读取路径修正
3. 总览 feed health 读取真实订阅管理器
4. 主循环补充最新编排决策缓存
5. Alpha Brain 快照也同步改为优先读最新真实 regime/stable 缓存

因此，修复后总览里以下字段会更接近真实运行态：

- 主导市场状态
- 置信度
- 稳定
- 数据源健康
- 策略权重汇总

## 6. 建议的测试验证方式

建议按下面顺序验证总览页面：

1. 启动 paper 模式并确认 HTX realtime feed 已建立
2. 观察总览中的 `feed_health.health` 是否不再长期为 `unknown`
3. 等待足够 K 线数据后观察 `dominant_regime / confidence / stable`
4. 在有波动时检查 `strategy_weight_summary` 是否开始出现权重
5. 对照日志确认：若策略有信号但未下单，是否是 gating / risk / budget 某一层阻断

如果下一步继续增强总览，我建议优先补两项：

1. 把 alerts 扩成结构化告警列表
2. 把“最近一次拒单原因”直接挂到总览

这样前端的解释力会明显提升。
