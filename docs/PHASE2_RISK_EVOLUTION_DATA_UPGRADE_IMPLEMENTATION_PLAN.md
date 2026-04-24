# Phase 2: 风控进化 + 数据升级详细实施计划

> 来源基线：[AI_QUANT_TRADER_EVOLUTION_STRATEGY.md](./AI_QUANT_TRADER_EVOLUTION_STRATEGY.md)
>
> 关联前置阶段：[PHASE1_CORE_BRAIN_IMPLEMENTATION_PLAN.md](./PHASE1_CORE_BRAIN_IMPLEMENTATION_PLAN.md)
>
> 目标阶段：Phase 2（6 周）
>
> 阶段目标：从“固定风控 + 单一 OHLCV 技术面”升级为“自适应风控矩阵 + 实时风险守卫 + 链上/情绪数据接入 + 多维 Alpha 融合”。

---

## 一、计划定位

本计划不是概念性蓝图，而是面向当前代码基线的 Phase 2 落地实施方案。它遵循三个硬性设计要求：

1. **风控模块化**：新增风险能力必须在现有 [modules/risk](../modules/risk) 之上分层演进，禁止把止损、追踪止盈、DCA、Kill Switch 直接散落进策略或执行层。
2. **外部数据可降级**：链上数据、情绪数据必须是“可用时增益、缺失时降级”的能力，禁止把外部 API 依赖变成主交易链路单点故障。
3. **决策可回放**：每一笔被放行、降权、阻断的交易，都必须能在日志和 trace 中回答三个问题：为什么允许、为什么阻断、用了哪些数据源。

---

## 二、当前代码锚点与实施假设

### 2.1 已有能力

当前仓库已经具备 Phase 2 的关键基础设施：

| 能力 | 现状锚点 | 结论 |
|------|---------|------|
| 硬约束风控 | [modules/risk/manager.py](../modules/risk/manager.py) | 已有单币种仓位、回撤熔断、单日亏损、连续亏损等最后防线 |
| 仓位计算 | [modules/risk/position_sizer.py](../modules/risk/position_sizer.py) | 已支持 fixed risk / vol target / fractional Kelly，可作为自适应仓位底座 |
| 历史数据下载 | [modules/data/downloader.py](../modules/data/downloader.py) | 已有 CCXT OHLCV 下载与断点续传能力，可扩展为多源数据入口 |
| 特征中台 | [modules/alpha/ml/data_kitchen.py](../modules/alpha/ml/data_kitchen.py) | 已具备特征契约、诊断、训练/推理统一入口，可扩展外部数据视图 |
| 运行时骨架 | [modules/alpha/runtime](../modules/alpha/runtime) | `StrategyContext` 已预留 `risk_snapshot`，说明风险信息进入策略链路已有挂点 |
| 多模型融合容器 | [modules/alpha/ml/ensemble.py](../modules/alpha/ml/ensemble.py)、[modules/alpha/ml/meta_learner.py](../modules/alpha/ml/meta_learner.py) | 已支持单维技术面模型投票，为 Phase 2 多维 Alpha 融合提供容器 |

### 2.2 当前主要问题

| 问题 | 现状锚点 | 风险 |
|------|---------|------|
| 风控仍以静态硬阈值为主 | [modules/risk/manager.py](../modules/risk/manager.py) | 能挡灾，但不能随波动率、环境、置信度动态调节 |
| 风控信息缺少结构化快照 | [modules/alpha/contracts/strategy_context.py](../modules/alpha/contracts/strategy_context.py) | 目前 `risk_snapshot` 是宽松字典，难以让策略和编排器稳定消费 |
| 缺少自适应退出规划 | 当前没有专门 stoploss/trailing/DCA 规划模块 | 入场和风控参数仍易回到硬编码 |
| 缺少实时预算与操作级熔断 | 当前只有组合熔断，没有独立 Budget Checker / Kill Switch | 可能在异常行情或外部依赖异常时继续下单 |
| 数据维度仍以 OHLCV 为主 | [modules/data/downloader.py](../modules/data/downloader.py)、[modules/alpha/ml/data_kitchen.py](../modules/alpha/ml/data_kitchen.py) | 链上与情绪信号尚未接入，Alpha 维度不够宽 |
| MetaLearner 仍是单维技术面容器 | [modules/alpha/ml/meta_learner.py](../modules/alpha/ml/meta_learner.py) | 还不能表达“技术面 + 链上 + 情绪”三层 Alpha 的 source-aware 融合 |

### 2.3 实施假设

Phase 2 的正确做法不是把更多规则塞进 `RiskManager`，也不是把更多特征直接扔给一个模型，而是：

1. 保留 `RiskManager` 作为最终守门员。
2. 在其上游新增 `AdaptiveRiskMatrix`、`BudgetChecker`、`KillSwitch` 等“可组合的前置决策层”。
3. 将链上与情绪数据按独立 source 采集、校验、对齐、注入 DataKitchen。
4. 让 `MetaLearner v2` 消费的是“各维度 Alpha 信号”，而不是直接消费杂糅原始字段。

**便宜验证点**：

1. [modules/alpha/contracts/strategy_context.py](../modules/alpha/contracts/strategy_context.py) 已有 `risk_snapshot` 字段，说明风险信息进入策略运行时不需要重建主骨架。
2. [modules/data/downloader.py](../modules/data/downloader.py) 与 [modules/alpha/ml/data_kitchen.py](../modules/alpha/ml/data_kitchen.py) 已经解耦，说明外部数据入口可以独立扩展而不污染训练/推理逻辑。
3. [modules/risk/manager.py](../modules/risk/manager.py) 已经是订单准入的统一入口，说明 Budget Checker / Kill Switch 可以叠加为前置审核层，而不需要重构执行网关主职责。

---

## 三、Phase 2 的范围定义

### 3.1 本阶段必须交付

1. 自适应风控矩阵 `AdaptiveRiskMatrix`
2. 追踪止损、ROI 阶梯止盈、冷却期与 DCA 规划能力
3. `BudgetChecker` + `KillSwitch` 实时风控层
4. 链上数据采集、缓存、特征构建与 freshness 降级机制
5. 情绪数据采集、缓存、特征构建与 freshness 降级机制
6. 多维 Alpha 融合器 `OmniSignalFusion` 与 `MetaLearner v2`
7. 风控与多源数据 trace 机制

### 3.2 本阶段明确不做

1. 不做 WebSocket order book / tick 微观结构层（留到 Phase 3）
2. 不做 Avellaneda 做市策略与库存管理（留到 Phase 3）
3. 不做 RL Agent 训练与在线学习（留到 Phase 3）
4. 不全面重写执行层；只增加必要 hook 和前置审核能力

---

## 四、目标架构

### 4.1 Phase 2 目标分层

```
apps/trader/main.py
    └─ 只负责装配 Phase 1 + Phase 2 高层入口

modules/risk/
    ├─ manager.py                # 现有最后防线（保留）
    ├─ position_sizer.py         # 现有仓位计算底座（保留）
    ├─ adaptive_matrix.py        # 自适应风险决策中枢
    ├─ exit_planner.py           # 止损 / 追踪止盈 / ROI 阶梯止盈
    ├─ dca_engine.py             # DCA 加仓规划与预算约束
    ├─ budget_checker.py         # 下单前预算预检
    ├─ kill_switch.py            # 实时熔断 / 操作级紧急停机
    ├─ cooldown.py               # 冷却期管理
    ├─ snapshot.py               # 结构化 RiskSnapshot / RiskPlan
    └─ state_store.py            # Kill Switch / 预算 / 冷却状态持久化

modules/data/
    ├─ downloader.py             # 现有 OHLCV 下载器（保留）
    ├─ onchain/
    │   ├─ providers.py          # 链上 API 适配层
    │   ├─ collector.py          # 采集与重试
    │   ├─ cache.py              # 本地缓存 / freshness 判断
    │   └─ feature_builder.py    # OnChain 特征视图构建
    ├─ sentiment/
    │   ├─ providers.py          # 情绪 / 资金费率 / OI / 多空比适配层
    │   ├─ collector.py          # 采集与重试
    │   ├─ cache.py              # 本地缓存 / freshness 判断
    │   └─ feature_builder.py    # Sentiment 特征视图构建
    └─ fusion/
        ├─ alignment.py          # 多源时间对齐
        ├─ freshness.py          # TTL / lag / stale 降级
        └─ source_contract.py    # SourceFrame / SourceFreshness

modules/alpha/ml/
    ├─ data_kitchen.py           # 增强：支持 technical/onchain/sentiment 视图
    ├─ feature_contract.py       # 增强：记录外部 source 依赖与 freshness 策略
    ├─ ensemble.py               # 保留：单 source 内部多模型投票
    ├─ meta_learner.py           # 增强：source-aware MetaLearner v2
    └─ omni_signal_fusion.py     # 多维 Alpha 融合中枢

modules/alpha/contracts/
    ├─ strategy_context.py       # 保留：风险快照进入策略上下文
    ├─ strategy_result.py        # 保留
    ├─ ensemble_types.py         # 保留：ModelVote / MetaSignal
    └─ alpha_source_types.py     # SourceSignal / FusionDecision / FreshnessState
```

### 4.2 模块化硬规则

1. `main.py` 不允许直接 import 链上/情绪 provider SDK，也不允许直接写风控规则。
2. `RiskManager` 仍然只做最终审核，不直接承担止损规划、DCA 策略或外部数据采集。
3. `BudgetChecker` 与 `KillSwitch` 不依赖具体策略实现，只消费结构化订单请求、风险状态和运行时健康状态。
4. `data/onchain` 与 `data/sentiment` 只输出规范化 source frame，不直接产出交易动作。
5. `MetaLearner v2` 只消费统一 `SourceSignal`，不直接感知底层 provider 返回的原始 payload。
6. 所有日志统一走 `core.logger.get_logger()`，禁止新模块自建日志体系。

---

## 五、核心接口设计

### 5.1 风险快照

```python
@dataclass(frozen=True)
class RiskSnapshot:
    current_drawdown: float
    daily_loss_pct: float
    consecutive_losses: int
    circuit_broken: bool
    kill_switch_active: bool
    budget_remaining_pct: float
    cooldown_symbols: dict[str, datetime]
    last_updated_at: datetime
```

### 5.2 自适应风控输出

```python
@dataclass(frozen=True)
class RiskPlan:
    allow_entry: bool
    position_scalar: float
    stop_loss_pct: float | None
    trailing_trigger_pct: float | None
    trailing_callback_pct: float | None
    take_profit_ladder: list[float]
    dca_levels: list[float]
    cooldown_minutes: int
    block_reasons: list[str]
    debug_payload: dict[str, Any]
```

### 5.3 外部数据源输入

```python
@dataclass(frozen=True)
class SourceFrame:
    source_name: str
    frame: pd.DataFrame
    timestamp_col: str
    freshness_ttl_sec: int
    lag_tolerance_sec: int
    metadata: dict[str, Any]

@dataclass(frozen=True)
class SourceFreshness:
    source_name: str
    is_fresh: bool
    lag_sec: int
    degrade_reason: str | None
```
```

### 5.4 多维 Alpha 信号

```python
@dataclass(frozen=True)
class SourceSignal:
    source_name: Literal["technical", "onchain", "sentiment"]
    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float
    score: float
    freshness_ok: bool
    weight: float
    debug_payload: dict[str, Any]

@dataclass(frozen=True)
class FusionDecision:
    final_action: Literal["BUY", "SELL", "HOLD"]
    final_confidence: float
    dominant_source: str
    source_signals: list[SourceSignal]
    debug_payload: dict[str, Any]
```

---

## 六、详细实施任务拆解

## 6.1 W9-W10：自适应风控矩阵 `AdaptiveRiskMatrix`

### 目标

把当前风控从“只会拦截的静态守门员”，升级为“会根据市场环境、信号置信度、波动率、回撤状态动态规划仓位与退出路径的风险决策层”。

### 具体任务

1. 新增 `modules/risk/adaptive_matrix.py`
2. 新增 `modules/risk/exit_planner.py`
3. 新增 `modules/risk/dca_engine.py`
4. 新增 `modules/risk/cooldown.py`
5. 新增 `modules/risk/snapshot.py`
6. 从现有 [modules/risk/manager.py](../modules/risk/manager.py) 提取运行时状态，构造成结构化 `RiskSnapshot`
7. 在现有 [modules/risk/position_sizer.py](../modules/risk/position_sizer.py) 之上组合四类 sizing 方法：
   - fixed risk
   - volatility target
   - fractional Kelly
   - confidence scalar
8. 输出 `RiskPlan`，但 **W9-W10 先只落“规划层”**，不直接改写执行层下单行为
9. 将 `RiskPlan` 摘要注入 `StrategyContext.risk_snapshot`，让策略和编排器可以读取

### 实施策略

第一版 `AdaptiveRiskMatrix` 采用“规则 + 参数化”方式，不在 W9-W10 就引入复杂的优化器：

1. 基于 `RegimeState` 调整基础止损宽度
2. 基于 `signal_confidence` 调整仓位比例
3. 基于 `current_drawdown` 与 `daily_loss_pct` 缩放风险预算
4. 基于波动率输出 trailing stop 和 ROI ladder
5. 只在 `dominant_regime` 与置信度同时满足条件时启用 DCA

### 交付物

| 交付物 | 验收标准 |
|--------|---------|
| `AdaptiveRiskMatrix` | 给定风险状态、环境和信号后能稳定输出 `RiskPlan` |
| `RiskSnapshot` | `StrategyContext` 中的风险信息不再是松散字典 |
| `ExitPlanner` | 能产出止损、追踪止盈、ROI 阶梯止盈计划 |
| `DCAEngine` | 能在预算约束内给出最多 N 层加仓计划 |

### 必打日志

| 级别 | 标签 | 内容 |
|------|------|------|
| INFO | `[RiskMatrix]` | 风控矩阵初始化、启用项、配置版本 |
| DEBUG | `[RiskMatrix]` | regime、confidence、drawdown、volatility、position_scalar |
| DEBUG | `[ExitPlan]` | stop_loss_pct、trailing 参数、ROI 阶梯 |
| DEBUG | `[DCA]` | DCA 层级、触发价格、预算占用 |
| WARNING | `[RiskMatrix]` | 风险状态缺失、降级为静态风控 |

### 验收标准

1. 相同输入下 `RiskPlan` 输出可稳定断言。
2. 低置信或高回撤时，仓位和止损计划明显收缩。
3. Risk Plan 可以被 trace 回放，而不是停留在隐式参数里。

---

## 6.2 W11：`BudgetChecker` + `KillSwitch` 实时风控层

### 目标

在当前组合熔断之外，新增“下单前预算预检”和“实时操作级停机开关”，确保系统在异常行情、数据陈旧、API 退化、连续拒单等情况下能快速自保。

### 具体任务

1. 新增 `modules/risk/budget_checker.py`
2. 新增 `modules/risk/kill_switch.py`
3. 新增 `modules/risk/state_store.py`
4. `BudgetChecker` 在 `RiskManager.check()` 之前运行，负责：
   - 预算占用校验
   - 最小下单单位 / 手续费 / 预估滑点预留
   - DCA 预算封顶
   - 已用风险预算与日内剩余预算核算
5. `KillSwitch` 独立监控以下信号：
   - 实时回撤超阈值
   - 日内亏损超阈值
   - 连续订单拒绝或成交失败过多
   - 数据源 freshness 失效
   - 外部 provider 错误率异常
6. 提供人工解锁与自动冷却恢复两类恢复机制
7. 状态持久化，避免重启后隐式解除风险状态

### 关键解耦要求

1. `KillSwitch` 不依赖具体策略类，只消费运行状态、错误计数和风险指标。
2. `BudgetChecker` 不决定交易方向，只决定“预算是否足够”。
3. `RiskManager` 保持最后审核者角色，避免职责被吞没。

### 必打日志

| 级别 | 标签 | 内容 |
|------|------|------|
| INFO | `[Budget]` | 预算检查结果、剩余预算、预计占用 |
| INFO | `[KillSwitch]` | 激活 / 解除、原因、触发阈值 |
| DEBUG | `[RiskGuard]` | 订单前预算明细、手续费/滑点预留 |
| WARNING | `[KillSwitch]` | 数据源 stale、API 失败率过高、拒单过多 |
| ERROR | `[KillSwitch]` | 未能持久化风险状态或状态不一致 |

### 验收标准

1. Kill Switch 激活后，一个 loop 内必须阻止新的买入订单。
2. 数据源 stale 触发的停机原因能在日志中直接读到。
3. 手工重置与自动冷却恢复都可单测覆盖。

---

## 6.3 W12：链上数据接入 `OnChainAlpha`

### 目标

引入独立的链上数据采集与特征构建层，不把外部 API 拉取逻辑塞进 DataKitchen 或策略本体。

### 具体任务

1. 新增 `modules/data/onchain/providers.py`
2. 新增 `modules/data/onchain/collector.py`
3. 新增 `modules/data/onchain/cache.py`
4. 新增 `modules/data/onchain/feature_builder.py`
5. 新增 `modules/data/fusion/freshness.py`
6. 新增 `modules/data/fusion/alignment.py`
7. 第一版接入以下规范化链上特征：
   - active_addresses_change
   - exchange_inflow_ratio
   - whale_tx_count_ratio
   - stablecoin_supply_ratio
   - miner_reserve_change
   - nvt_proxy
8. 将链上 source frame 按 K 线索引对齐后注入 DataKitchen，输出 `onchain_features`
9. freshness 机制必须显式输出：fresh / stale / missing / partial

### 数据约束

1. 链上数据往往低频，禁止直接 forward fill 到无限远。
2. 每个字段必须有独立 TTL，不能用全局一刀切 freshness。
3. 同步时必须保留原始采集时间戳，防止未来数据泄漏。

### 必打日志

| 级别 | 标签 | 内容 |
|------|------|------|
| INFO | `[OnChain]` | provider 初始化、采集频率、字段数 |
| DEBUG | `[OnChain]` | 每个 source 的最新时间戳、拉取耗时、命中缓存情况 |
| DEBUG | `[SourceFreshness]` | lag_sec、ttl_sec、degrade_reason |
| DEBUG | `[SourceAlign]` | 对齐前后样本数、缺失比例 |
| WARNING | `[OnChain]` | source stale、字段缺失、API 降级 |

### 验收标准

1. 外部链上 source 缺失时系统降级到 technical-only，不崩溃。
2. freshness 判定与时间对齐规则可单测稳定断言。
3. DataKitchen 可输出 `onchain_features` 和对应诊断视图。

---

## 6.4 W13：情绪数据接入 `SentimentAlpha`

### 目标

引入与链上层对称的情绪/资金面特征层，把“恐慌贪婪、资金费率、持仓偏斜、情绪变化”纳入独立 Alpha 维度。

### 具体任务

1. 新增 `modules/data/sentiment/providers.py`
2. 新增 `modules/data/sentiment/collector.py`
3. 新增 `modules/data/sentiment/cache.py`
4. 新增 `modules/data/sentiment/feature_builder.py`
5. 第一版支持以下统一字段：
   - fear_greed_index
   - funding_rate_zscore
   - long_short_ratio_change
   - open_interest_change
   - liquidation_imbalance
   - sentiment_score_ema
6. 将情绪 source frame 注入 DataKitchen，输出 `sentiment_features`
7. 对文本/新闻类情绪先只保留接口，不在 W13 追求复杂 NLP 模型

### 实施策略

第一版优先接“结构化、更新频率稳定”的情绪代理指标：资金费率、持仓量、多空比、恐慌贪婪指数。文本新闻与社交舆情先作为后续可扩展接口保留，不在 Phase 2 强行引入。

### 必打日志

| 级别 | 标签 | 内容 |
|------|------|------|
| INFO | `[Sentiment]` | provider 初始化、source 数量、采集周期 |
| DEBUG | `[Sentiment]` | fear_greed / funding / OI / long_short 最新值 |
| DEBUG | `[SourceFreshness]` | freshness 判断结果 |
| WARNING | `[Sentiment]` | 指标回源失败、字段缺失、fallback 到 neutral |
| ERROR | `[Sentiment]` | source schema 与预期契约不一致 |

### 验收标准

1. 情绪 source 缺失时系统回退到 neutral，而不是生成伪信号。
2. DataKitchen 可输出 `sentiment_features` 并保留 freshness 元信息。
3. 所有时间对齐和缺失值回填策略都有明确诊断输出。

---

## 6.5 W14：多维 Alpha 融合 `OmniSignalFusion` + `MetaLearner v2`

### 目标

把 Phase 1 的单维技术面模型投票，升级为“技术面 + 链上 + 情绪”三层 Alpha 信号融合框架，为 Phase 3 的 order book / RL Agent 留出容器。

### 具体任务

1. 新增 `modules/alpha/ml/omni_signal_fusion.py`
2. 新增 `modules/alpha/contracts/alpha_source_types.py`
3. 扩展 [modules/alpha/ml/meta_learner.py](../modules/alpha/ml/meta_learner.py)，使其支持 source-aware 融合
4. 第一版支持三类 source：
   - `technical`：沿用 Phase 1 DataKitchen + Ensemble + MetaLearner
   - `onchain`：独立轻量模型或规则评分器
   - `sentiment`：独立轻量模型或规则评分器
5. `OmniSignalFusion` 融合时必须考虑：
   - source freshness
   - source recent performance
   - regime affinity
   - risk state（高风险时压低外部 source 权重）
6. 输出统一 `FusionDecision`：
   - `final_action`
   - `final_confidence`
   - `dominant_source`
   - `source_signals`
   - `debug_payload`

### 实施策略

第一版只做可解释的 weighted fusion，不直接上复杂 stacking：

1. 各 source 先独立输出 `SourceSignal`
2. freshness 不通过的 source 直接置零权重
3. recent performance 不佳的 source 自动降权
4. 最终通过 weighted average / weighted vote 输出 `FusionDecision`

### 必打日志

| 级别 | 标签 | 内容 |
|------|------|------|
| INFO | `[OmniFusion]` | 激活 source 列表、融合策略、最终行动 |
| DEBUG | `[SourceAlpha]` | 每个 source 的 action、confidence、freshness、weight |
| DEBUG | `[MetaV2]` | 加权明细、dominant source、最终得分 |
| WARNING | `[OmniFusion]` | source 缺席、source stale、自动降级 technical-only |
| ERROR | `[MetaV2]` | source signal schema 不一致 |

### 验收标准

1. 任一 source 缺席时系统可降级运行。
2. `FusionDecision` 能清楚解释为什么选择 BUY / SELL / HOLD。
3. 与现有 Phase 1 技术面链路兼容，不要求重写 `MLPredictorV2`。

---

## 七、日志与可观测性设计

## 7.1 日志目标

Phase 2 的日志必须满足三类问题定位：

1. **风险定位**：为什么这笔交易被允许、缩仓或阻断？
2. **数据定位**：链上/情绪特征是否新鲜、是否对齐、是否被降级？
3. **融合定位**：最终信号到底主要来自技术面、链上还是情绪？

### 7.2 统一日志字段

所有 Phase 2 模块必须尽量带上以下字段：

| 字段 | 含义 |
|------|------|
| `trace_id` | 单根 bar 或单次下单的全链路 ID |
| `loop_seq` | 主循环序号 |
| `symbol` | 交易对 |
| `regime` | 当前主导市场环境 |
| `risk_mode` | 当前风险模式（normal/reduced/blocked） |
| `source_name` | technical/onchain/sentiment |
| `source_lag_sec` | source 到当前时刻的滞后秒数 |
| `budget_remaining_pct` | 剩余风险预算比例 |
| `kill_switch_active` | Kill Switch 是否激活 |
| `planned_stop_pct` | 计划止损比例 |
| `position_scalar` | 自适应缩放后的仓位系数 |
| `elapsed_ms` | 模块耗时 |

### 7.3 日志层级规范

| 级别 | 允许内容 |
|------|---------|
| DEBUG | 风控中间变量、freshness 判定、source 权重、融合细节 |
| INFO | 生命周期、配置版本、状态切换、最终风险决策 |
| WARNING | 降级运行、source stale、预算不足、Kill Switch 触发 |
| ERROR | 状态持久化失败、schema 不一致、无法继续运行的错误 |

### 7.4 Trace 机制扩展

建议在现有 Phase 1 trace 基础上扩充以下结构：

```json
{
  "trace_id": "BTCUSDT-20260423T120000Z-001245",
  "loop_seq": 1245,
  "symbol": "BTC/USDT",
  "regime": {"dominant": "sideways", "confidence": 0.62},
  "risk_snapshot": {
    "drawdown": 0.021,
    "daily_loss_pct": 0.008,
    "kill_switch_active": false,
    "budget_remaining_pct": 0.74
  },
  "risk_plan": {
    "position_scalar": 0.72,
    "stop_loss_pct": 0.018,
    "trailing_trigger_pct": 0.015,
    "take_profit_ladder": [0.02, 0.04, 0.06]
  },
  "source_freshness": {
    "technical": {"is_fresh": true, "lag_sec": 0},
    "onchain": {"is_fresh": true, "lag_sec": 1800},
    "sentiment": {"is_fresh": false, "lag_sec": 10800, "degrade_reason": "ttl_exceeded"}
  },
  "fusion": {
    "dominant_source": "technical",
    "final_action": "BUY",
    "final_confidence": 0.68
  }
}
```

### 7.5 配置项建议

在 [core/config.py](../core/config.py) 和 `configs/system.yaml` 中新增：

```yaml
phase2:
  enabled: true
  adaptive_risk_enabled: true
  budget_checker_enabled: true
  kill_switch_enabled: true
  onchain_enabled: true
  sentiment_enabled: true
  risk_trace_enabled: true
  source_trace_enabled: true
  risk_trace_sample_rate: 1.0
  external_source_ttl_sec:
    onchain: 7200
    sentiment: 3600
  kill_switch:
    max_api_error_rate: 0.3
    max_rejected_orders: 5
    max_data_staleness_sec: 10800
  adaptive_risk:
    max_position_scalar: 1.0
    min_position_scalar: 0.2
    dca_max_legs: 3
    dca_budget_pct: 0.25
    trailing_trigger_pct: 0.015
    trailing_callback_pct: 0.006
```

---

## 八、测试与验收计划

## 8.1 单元测试

新增测试文件：

1. `tests/test_adaptive_risk_matrix.py`
2. `tests/test_budget_checker.py`
3. `tests/test_kill_switch.py`
4. `tests/test_onchain_features.py`
5. `tests/test_sentiment_features.py`
6. `tests/test_omni_signal_fusion.py`

现有必须回归：

1. [tests/test_data_downloader.py](../tests/test_data_downloader.py)
2. [tests/test_execution.py](../tests/test_execution.py)
3. [tests/test_portfolio.py](../tests/test_portfolio.py)
4. [tests/test_regime_detector.py](../tests/test_regime_detector.py)
5. [tests/test_orchestration.py](../tests/test_orchestration.py)
6. [tests/test_threshold_calibrator.py](../tests/test_threshold_calibrator.py)
7. [tests/test_meta_learner.py](../tests/test_meta_learner.py)

## 8.2 联调测试

每完成一个阶段都执行：

```powershell
python -m pytest tests/test_execution.py -q
python -m pytest tests/test_data_downloader.py -q
python -m pytest tests/test_adaptive_risk_matrix.py -q
python -m pytest tests/test_budget_checker.py -q
```

Phase 2 收尾时执行：

```powershell
python -m pytest tests/ -q
```

参考当前仓库状态：2026-04-23 时 Phase 1 完成后全量测试为 `346 passed`。Phase 2 合并后必须保持全量通过，并新增针对风控与外部数据模块的覆盖。

## 8.3 运行时验收清单

1. 启动后日志可见 `RiskMatrix` / `Budget` / `KillSwitch` / `OnChain` / `Sentiment` / `OmniFusion` 六类模块日志。
2. 任一外部 source stale 时，系统自动降级，但主链路不崩溃。
3. Kill Switch 激活时，新买入请求会被拒绝且拒绝原因可追踪。
4. `RiskPlan`、`source_freshness`、`FusionDecision` 都能进入 trace。
5. 关闭 `phase2.enabled` 后，系统可回退到纯 Phase 1 行为。

---

## 九、实施顺序与依赖关系

### 9.1 依赖图

```
W9-10 AdaptiveRiskMatrix / RiskSnapshot
  └─ W11 BudgetChecker / KillSwitch

W9-10 RiskSnapshot
  └─ W12 OnChainAlpha
  └─ W13 SentimentAlpha

W12 + W13
  └─ W14 OmniSignalFusion / MetaLearner v2

Phase 1 Runtime + DataKitchen + MetaLearner
  └─ Phase 2 全阶段接线
```

### 9.2 禁止反向依赖

1. `modules/data/onchain` 和 `modules/data/sentiment` 不依赖 `apps/trader`。
2. `modules/risk/kill_switch.py` 不依赖具体策略类。
3. `modules/alpha/ml/omni_signal_fusion.py` 不依赖具体 provider SDK。
4. `RiskManager` 不反向依赖 `AdaptiveRiskMatrix` 的内部实现细节。
5. `main.py` 只装配高层入口，不直接实现风控规则和 source 对齐逻辑。

---

## 十、风险与回退策略

| 风险 | 影响 | 缓解 |
|------|------|------|
| AdaptiveRiskMatrix 过于激进 | 可能导致频繁缩仓或过度交易 | 第一版保持规则透明，可配置回退到静态风控 |
| DCA 误用 | 下跌中不断摊平，放大风险 | 仅在 regime 与 confidence 同时满足条件时启用，强制预算封顶 |
| 外部数据 stale 或断流 | 可能误导模型或让链路阻塞 | freshness 显式降级，默认退回 technical-only |
| 时间对齐失误 | 产生未来数据泄漏 | 所有 source 保留原始采集时间戳，统一 alignment 单测覆盖 |
| Kill Switch 误触发 | 错失交易机会 | 增加手动重置与冷却恢复，并保留原因审计 |
| 配置项膨胀 | 调试和运营复杂度升高 | 把 Phase 2 配置集中在单一命名空间下并提供默认值 |

### 回退原则

1. 每周交付必须可由配置开关禁用。
2. `AdaptiveRiskMatrix` 与 `KillSwitch` 上线前保留一周旁路日志观察。
3. 外部数据 source 缺失时必须自动降级，而不是阻塞主交易链路。
4. 新增风险状态和 source 依赖都必须版本化。
5. 任一 Phase 2 子模块失败时，可单独回退到 Phase 1 链路。

---

## 十一、第一批落地动作

为保证 Phase 2 真正开始，而不是继续停留在方案层，建议按以下顺序启动实现：

1. 先落 `RiskSnapshot + AdaptiveRiskMatrix` 骨架，不先接外部数据。
2. 把止损、追踪止盈、DCA 先做成“规划层输出”，不立即侵入执行逻辑。
3. 在现有 `RiskManager` 之前接入 `BudgetChecker` 与 `KillSwitch`。
4. 然后接 `OnChain` 采集与 freshness/对齐层。
5. 再接 `Sentiment` 采集与特征层。
6. 等 source 视图稳定后，再落 `OmniSignalFusion + MetaLearner v2`。

这是 Phase 2 的正确启动顺序。先稳住风险与 source 契约，再叠加 Alpha 宽度，才能保持调试可控和回退简单。

---

## 十二、阶段完成标准

Phase 2 只有同时满足以下条件才算完成：

1. 风控不再只依赖静态阈值，`AdaptiveRiskMatrix` 能输出结构化 `RiskPlan`。
2. `BudgetChecker` 与 `KillSwitch` 可以独立阻断下单，并能审计触发原因。
3. DataKitchen 可以统一接入 technical / onchain / sentiment 三类 source。
4. 任一外部 source stale 或缺失时，系统能自动降级运行。
5. `OmniSignalFusion` 能输出可解释的 source-aware 最终信号。
6. 新增测试通过，且全量测试不回退。

一旦这六项成立，Phase 2 就不是“多加几条风控规则和两个数据源”，而是完成了交易系统从静态技术面模型向自适应、多源、可回放决策系统的第二次体系化升级。