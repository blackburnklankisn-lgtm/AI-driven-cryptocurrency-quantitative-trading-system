# Phase 3: 高级策略 + 自进化详细实施计划

> 来源基线：[AI_QUANT_TRADER_EVOLUTION_STRATEGY.md](./AI_QUANT_TRADER_EVOLUTION_STRATEGY.md)
>
> 关联前置阶段：[PHASE1_CORE_BRAIN_IMPLEMENTATION_PLAN.md](./PHASE1_CORE_BRAIN_IMPLEMENTATION_PLAN.md)
>
> 关联前置阶段：[PHASE2_RISK_EVOLUTION_DATA_UPGRADE_IMPLEMENTATION_PLAN.md](./PHASE2_RISK_EVOLUTION_DATA_UPGRADE_IMPLEMENTATION_PLAN.md)
>
> 目标阶段：Phase 3（8 周）
>
> 阶段目标：从“Bar 级择时 + 单向信号 + 人工维护模型/策略”升级为“实时订单簿微观结构 + Avellaneda 做市 + RL 策略代理 + Self-Evolution Engine 自动演进闭环”。

---

## 一、计划定位

本计划不是概念性愿景，而是面向当前代码基线的 Phase 3 落地实施方案。它遵循四个硬性设计要求：

1. **极致模块化**：实时数据、做市、RL、自进化必须拆成独立目录与稳定契约，禁止继续把新逻辑堆进 [apps/trader/main.py](../apps/trader/main.py) 或单个“万能类”。
2. **充足调试日志**：必须能通过日志复盘“一个 tick / 一个 quote / 一个 RL action / 一次策略升降级”为什么发生、由谁触发、最终结果是什么。
3. **Shadow/Paper 优先**：Phase 3 的新能力默认先在 replay / paper / shadow 路径验真，禁止一上来直连 live 发单。
4. **风险守卫不旁路**：做市策略、RL Agent、自进化调度均不能绕过 Phase 2 已有的 `BudgetChecker`、`KillSwitch`、`RiskManager`。

---

## 二、当前代码锚点与实施假设

### 2.1 已有能力

当前仓库已经具备 Phase 3 的若干关键基础设施：

| 能力 | 现状锚点 | 结论 |
|------|---------|------|
| 时间主控数据入口 | [modules/data/feed.py](../modules/data/feed.py) | 已明确抽象“回测与实盘接口一致”，可作为实时流数据层的上位接口 |
| 事件总线机制 | [core/event.py](../core/event.py) | 已有事件驱动骨架，可扩展 `OrderBookEvent` / `QuoteEvent` / `PolicyEvent` |
| 执行网关 | [modules/execution/gateway.py](../modules/execution/gateway.py) | 已支持 `paper` / `live` 模式，适合做市与 RL shadow 验证 |
| 持续学习基础 | [modules/alpha/ml/continuous_learner.py](../modules/alpha/ml/continuous_learner.py) | 已具备重训触发、A/B 对比和模型切换雏形，可扩展为 Self-Evolution 子能力 |
| 模型注册与版本管理 | [modules/alpha/ml/model_registry.py](../modules/alpha/ml/model_registry.py) | 可作为 Phase 3 模型/策略/Policy 版本仓的基线 |
| 风险守卫 | [modules/risk](../modules/risk) | 已有 `AdaptiveRiskMatrix`、`BudgetChecker`、`KillSwitch`，可作为 Phase 3 统一安全底座 |
| 多维 Alpha 容器 | [modules/alpha/ml/meta_learner_v2.py](../modules/alpha/ml/meta_learner_v2.py)、[modules/alpha/ml/omni_signal_fusion.py](../modules/alpha/ml/omni_signal_fusion.py) | 已经能消费 `technical` / `onchain` / `sentiment`，可为 RL 和微观结构扩容 |

### 2.2 当前主要缺口

| 问题 | 现状锚点 | 风险 |
|------|---------|------|
| 没有真实的订单簿/成交流模块 | 当前仅有 [modules/data/feed.py](../modules/data/feed.py) 的接口说明 | 做市与微观结构 Alpha 无输入来源 |
| 没有订单簿缓存与序列一致性保护 | 当前无 `depth cache` / `sequence gap` 处理 | WebSocket 一旦乱序或丢包，做市报价会基于脏数据 |
| 没有独立做市策略层 | 当前 [modules/alpha/strategies](../modules/alpha/strategies) 仅为 bar 级方向策略 | 无法实现双边报价、库存偏斜、maker quote 生命周期 |
| 没有 RL 训练环境与推理协议 | 当前仓库无 `environment` / `policy` / `reward engine` 模块 | RL Agent 无法安全接入、验证和回滚 |
| 没有统一的策略晋升/淘汰调度层 | 当前 `ContinuousLearner` 只覆盖模型级重训 | “策略演进”仍然依赖人工操作，无法闭环 |
| 现有回测以 K 线驱动为主 | 当前 DataFeed 发布 `KlineEvent` | 做市与 tick/RL 需要 replay / microstructure 仿真能力 |

### 2.3 实施假设

Phase 3 的正确做法不是一次性引入四大复杂能力后再试图拼起来，而是按“数据面 → 策略面 → 代理面 → 演进面”的顺序逐层落地：

1. 先建立实时订单簿与 replay/recovery 机制。
2. 再让做市策略建立在稳定的订单簿快照和库存风控之上。
3. RL Agent 第一版先做“受约束的策略代理”，不直接放权给无限动作空间。
4. 最后把现有 `ContinuousLearner`、`ModelRegistry`、paper 模式执行结果统一编排成 Self-Evolution Engine。

**便宜验证点**：

1. [modules/data/feed.py](../modules/data/feed.py) 已明确“回测与实盘对上层接口一致”，说明实时流层可以按同样思想扩展，而不必重写整个上层运行时。
2. [modules/execution/gateway.py](../modules/execution/gateway.py) 已支持 `paper` 模式，说明做市与 RL 可以先在真实执行协议下做 shadow/paper 验证。
3. [modules/alpha/ml/continuous_learner.py](../modules/alpha/ml/continuous_learner.py) 已经有重训触发与 A/B 逻辑，说明 Self-Evolution Engine 不需要从零开始。
4. [modules/risk](../modules/risk) 已有预算、熔断与 adaptive risk 模块，说明 Phase 3 不应新建第二套风控系统。

---

## 三、Phase 3 的范围定义

### 3.1 本阶段必须交付

1. 实时数据层 `RealtimeFeed` + 订单簿缓存 `DepthCache`
2. 标准化订单簿快照、成交流、微观结构特征视图
3. Avellaneda 做市策略 `MarketMakingStrategy`
4. 库存管理、双边报价、maker quote 生命周期与 replay/paper 验证链路
5. RL 交易代理 `TradingPolicyAgent`（PPO v1）
6. `SelfEvolutionEngine`：参数调优、候选策略 A/B、策略晋升/降级/暂停闭环
7. Phase 3 统一 trace 与 observability 扩展

### 3.2 本阶段明确不做

1. 不做 HFT 级撮合、纳秒级延迟优化、共址部署。
2. 不做“在线探索直接作用于 live 账户”的 RL；live 仅允许 shadow 或受限 paper 晋升后的策略。
3. 不做自动生成源代码或自修改策略代码；自进化只允许调整参数、权重、启停状态和版本切换。
4. 不做跨交易所智能路由与最佳执行网络；Phase 3 先专注单交易所稳定性。
5. 不把 Avellaneda 做市逻辑塞进执行网关；执行层仍只负责标准下单接口。

---

## 四、目标架构

### 4.1 Phase 3 目标分层

```
apps/trader/main.py
    └─ 只负责 Phase 1 + Phase 2 + Phase 3 高层装配与生命周期管理

modules/data/
    ├─ feed.py                      # 现有 K 线时间主控（保留）
    ├─ fusion/                      # 现有多源对齐（保留）
    └─ realtime/
        ├─ ws_client.py             # 交易所 WebSocket 适配层
        ├─ subscription_manager.py  # 订阅、重连、心跳管理
        ├─ depth_cache.py           # 订单簿增量合并与序列一致性
        ├─ trade_cache.py           # 最新成交流缓冲
        ├─ orderbook_types.py       # OrderBookSnapshot / TradeTick
        ├─ feature_builder.py       # 微观结构特征构建
        ├─ replay_feed.py           # tick/orderbook 回放
        └─ freshness.py             # 延迟、断流、数据健康评估

modules/alpha/
    ├─ contracts/
    │   ├─ orderbook_types.py       # 深度/成交/报价契约
    │   ├─ mm_types.py              # QuoteIntent / QuoteDecision / InventorySnapshot
    │   ├─ rl_types.py              # RLObservation / RLAction / PolicyDecision
    │   └─ evolution_types.py       # CandidateSnapshot / PromotionDecision
    ├─ strategies/                  # 现有 bar 级策略（保留）
    ├─ market_making/
    │   ├─ avellaneda_model.py      # reservation price / optimal spread
    │   ├─ quote_engine.py          # 双边报价生成
    │   ├─ inventory_manager.py     # 库存偏斜与仓位约束
    │   ├─ fill_simulator.py        # maker fill 回放/仿真
    │   ├─ quote_lifecycle.py       # 报价刷新、撤单、过期管理
    │   ├─ quote_state_store.py     # 持久化 quote 状态
    │   └─ strategy.py              # MarketMakingStrategy
    ├─ rl/
    │   ├─ environment.py           # TradingEnvironment
    │   ├─ observation_builder.py   # Phase 1/2/3 特征拼接为 RL observation
    │   ├─ reward_engine.py         # 收益、回撤、换手、费用、风险惩罚
    │   ├─ action_adapter.py        # 离散动作 -> StrategyResult / Quote bias
    │   ├─ ppo_agent.py             # PPO 训练与推理封装
    │   ├─ rollout_store.py         # 训练轨迹缓存
    │   ├─ evaluator.py             # OOS / paper / shadow 评估
    │   └─ policy_store.py          # policy 版本管理与回滚
    └─ ml/
        ├─ meta_learner_v2.py       # 保留，扩展 technical/onchain/sentiment/microstructure/rl
        └─ omni_signal_fusion.py    # 保留，扩展 source 权重管理

modules/evolution/
    ├─ self_evolution_engine.py     # 总协调器
    ├─ scheduler.py                 # 周期调度（周末优化/日更评估）
    ├─ candidate_registry.py        # 策略/模型/Policy 候选清单
    ├─ ab_test_manager.py           # paper/shadow A/B 验证
    ├─ promotion_gate.py            # 上线门禁
    ├─ retirement_policy.py         # 降权、暂停、淘汰规则
    ├─ state_store.py               # 演进状态持久化
    └─ report_builder.py            # 周报、变更摘要、回滚建议
```

### 4.2 模块化硬规则

1. `main.py` 不允许直接处理订单簿增量包、RL rollout、做市库存逻辑或策略淘汰规则。
2. `execution/gateway.py` 不允许包含 Avellaneda 公式、RL reward 或策略晋升逻辑。
3. `market_making/` 只能消费标准化 `OrderBookSnapshot`、`RiskSnapshot`、`PortfolioSnapshot`，不能直接依赖原始交易所 payload。
4. `rl/` 训练环境不得直接调用 live 执行网关；训练一律使用 replay/backtest/paper 数据。
5. `evolution/` 只能操作“版本、权重、启停、参数集”，不允许动态修改源代码文件。
6. 所有新增状态持久化统一采用 `state_store.py` 风格原子写入，禁止半写状态文件。
7. 所有日志统一走 `core.logger.get_logger()`；Phase 3 禁止新增散落的 `print()`。
8. 所有新能力必须保留 replay 接口，否则不准进入 live 前验证阶段。

---

## 五、核心接口设计

### 5.1 订单簿快照

```python
@dataclass(frozen=True)
class OrderBookSnapshot:
    symbol: str
    exchange: str
    sequence_id: int
    best_bid: float
    best_ask: float
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    spread_bps: float
    mid_price: float
    imbalance: float
    received_at: datetime
    exchange_ts: datetime | None
    is_gap_recovered: bool
    debug_payload: dict[str, Any]
```

### 5.2 做市输出

```python
@dataclass(frozen=True)
class InventorySnapshot:
    symbol: str
    base_qty: float
    quote_value: float
    inventory_pct: float
    target_inventory_pct: float
    max_inventory_pct: float
    unrealized_pnl: float
    last_updated_at: datetime


@dataclass(frozen=True)
class QuoteDecision:
    symbol: str
    bid_price: float | None
    ask_price: float | None
    bid_size: float | None
    ask_size: float | None
    reservation_price: float
    optimal_spread_bps: float
    skew_bps: float
    allow_post_bid: bool
    allow_post_ask: bool
    reason_codes: list[str]
    debug_payload: dict[str, Any]
```

### 5.3 RL 观察与动作

```python
@dataclass(frozen=True)
class RLObservation:
    symbol: str
    trace_id: str
    feature_vector: list[float]
    feature_names: list[str]
    regime: str
    risk_mode: str
    inventory_pct: float
    position_pct: float
    source_freshness: dict[str, bool]
    timestamp: datetime


@dataclass(frozen=True)
class RLAction:
    action_type: Literal["BUY", "SELL", "HOLD", "REDUCE", "WIDEN_QUOTE"]
    action_value: float
    confidence: float
    debug_payload: dict[str, Any]


@dataclass(frozen=True)
class PolicyDecision:
    policy_id: str
    policy_version: str
    action: RLAction
    reward_estimate: float | None
    safety_override: bool
    debug_payload: dict[str, Any]
```

### 5.4 自进化调度输出

```python
@dataclass(frozen=True)
class CandidateSnapshot:
    candidate_id: str
    candidate_type: Literal["model", "strategy", "policy", "params"]
    owner: str
    version: str
    sharpe_30d: float
    max_drawdown_30d: float
    win_rate_30d: float
    ab_lift: float | None
    status: Literal["candidate", "shadow", "paper", "active", "paused", "retired"]
    debug_payload: dict[str, Any]


@dataclass(frozen=True)
class PromotionDecision:
    candidate_id: str
    action: Literal["PROMOTE", "HOLD", "DEMOTE", "RETIRE", "ROLLBACK"]
    reason_codes: list[str]
    effective_at: datetime
    debug_payload: dict[str, Any]
```

---

## 六、详细实施任务拆解

## 6.1 W15-W16：实时数据层 + 订单簿微观结构

### 目标

建立稳定的实时流数据基础设施，使系统不仅能看 K 线，还能消费订单簿深度、成交流与微观结构特征，为做市与 RL 提供可回放、可降级、可诊断的数据面。

### 具体任务

1. 新增 `modules/data/realtime/ws_client.py`
2. 新增 `modules/data/realtime/subscription_manager.py`
3. 新增 `modules/data/realtime/depth_cache.py`
4. 新增 `modules/data/realtime/trade_cache.py`
5. 新增 `modules/data/realtime/orderbook_types.py`
6. 新增 `modules/data/realtime/feature_builder.py`
7. 新增 `modules/data/realtime/replay_feed.py`
8. 在 `core/event.py` 扩展：
   - `OrderBookEvent`
   - `TradeTickEvent`
   - `FeedHealthEvent`
9. 为实时数据层加入 fallback：
   - WebSocket 断开时快速重连
   - 序列号缺口时触发快照回补
   - 断流超时后发出 freshness degraded 信号
10. 产出第一版微观结构特征：
   - top_of_book_spread_bps
   - order_imbalance_top_n
   - micro_price
   - trade_flow_imbalance
   - book_pressure_ratio
   - mid_price_return_1s/5s

### 数据约束

1. 订单簿增量必须基于 `sequence_id` 合并，检测 gap 时禁止继续向策略层发布“看起来完整”的假快照。
2. `replay_feed.py` 必须支持“按录制顺序回放 order book + trade tick”，否则 RL 和做市回测不可验证。
3. `feature_builder.py` 只构建特征，不做交易动作判断。
4. 必须保留 `received_at` 与 `exchange_ts` 双时间戳，用于 latency 与乱序诊断。

### 关键解耦要求

1. 交易所 WebSocket 原始包解析只允许出现在 `ws_client.py` 或其 adapter 子类中。
2. `DepthCache` 不知道交易逻辑；它只负责订单簿状态一致性。
3. 微观结构特征构建只输出 `SourceFrame` 或标准化 DataFrame，不直接输出 BUY/SELL。
4. `ReplayFeed` 要与真实实时流保持同一事件契约，禁止做一套“训练专用假接口”。

### 必打日志

| 级别 | 标签 | 内容 |
|------|------|------|
| INFO | `[RealtimeFeed]` | 连接建立、订阅列表、重连完成、回补完成 |
| DEBUG | `[DepthCache]` | sequence_id、gap 检测、快照大小、best bid/ask |
| DEBUG | `[MicroAlpha]` | spread_bps、imbalance、micro_price、特征耗时 |
| WARNING | `[RealtimeFeed]` | 心跳丢失、断流超时、fallback 到 snapshot 或 replay |
| ERROR | `[DepthCache]` | 序列回补失败、快照不一致、无可恢复数据 |

### 验收标准

1. 任一 order book gap 都会被明确日志标记，且策略层不会拿到脏快照。
2. replay 与 live 事件结构一致，可复用于做市与 RL 测试。
3. 订单簿断流时系统降级到技术面/低频数据而不是崩溃。

---

## 6.2 W17-W18：Avellaneda 做市策略 + 库存管理

### 目标

引入可受控、可回放、可风控的做市策略，提供震荡市和高流动性环境下的基础收益层，同时避免库存失衡与单边暴露失控。

### 具体任务

1. 新增 `modules/alpha/market_making/avellaneda_model.py`
2. 新增 `modules/alpha/market_making/quote_engine.py`
3. 新增 `modules/alpha/market_making/inventory_manager.py`
4. 新增 `modules/alpha/market_making/fill_simulator.py`
5. 新增 `modules/alpha/market_making/quote_lifecycle.py`
6. 新增 `modules/alpha/market_making/quote_state_store.py`
7. 新增 `modules/alpha/market_making/strategy.py`
8. 新增 `modules/alpha/contracts/mm_types.py`
9. 把 `MarketMakingStrategy` 接到 `StrategyProtocol` 运行时链路上
10. 将做市订单统一经过：
    - `BudgetChecker`
    - `KillSwitch`
    - `RiskManager`
    - `CCXTGateway` paper/live

### 第一版策略边界

1. 第一版先专注单交易对单交易所。
2. 第一版先做现货 inventory 管理，不直接覆盖复杂永续合约对冲。
3. 第一版先支持双边 quote 和库存 skew，不做跨 venue hedge。
4. 第一版默认通过 paper/replay 验证，只有通过门禁后才允许 live 小流量启用。

### 做市核心逻辑

1. 以 mid price + inventory skew 计算 reservation price
2. 以波动率、成交强度、订单簿厚度、风险 aversion 计算 optimal spread
3. 根据库存偏斜动态决定：
   - 是否禁用 bid
   - 是否禁用 ask
   - 是否扩大某一侧 spread
4. quote 超时、mid 大幅移动、订单被部分成交时执行 refresh/cancel/repost
5. maker fill 事件进入状态机，更新库存、PnL、quote freshness 和下一轮 skew

### 关键解耦要求

1. Avellaneda 公式只允许在 `avellaneda_model.py` 中实现，禁止散落到执行层或策略总线中。
2. `fill_simulator.py` 必须独立于 live 执行，供 replay/backtest/paper 共用。
3. `inventory_manager.py` 不直接下单，只输出库存风险建议。
4. `quote_lifecycle.py` 只负责 quote 状态机，不做风险审核。

### 必打日志

| 级别 | 标签 | 内容 |
|------|------|------|
| INFO | `[Avellaneda]` | 策略初始化、参数版本、启停状态 |
| DEBUG | `[QuoteEngine]` | reservation_price、spread_bps、bid/ask、刷新原因 |
| DEBUG | `[Inventory]` | inventory_pct、skew_bps、目标库存偏差 |
| DEBUG | `[FillSim]` | maker fill 判定、成交价、quote age、滑点 |
| WARNING | `[QuoteRisk]` | 库存过重、单边暴露、spread 异常放宽 |
| ERROR | `[QuoteLifecycle]` | 撤单失败、重复 quote、状态机不一致 |

### 验收标准

1. 做市策略不会绕过风险守卫或直接写执行网关。
2. inventory 超阈值时能自动收紧一侧或暂停一侧报价。
3. replay/paper 环境下可以稳定复现 quote 生命周期与 fill 行为。

---

## 6.3 W19-W21：RL 交易代理 `TradingPolicyAgent`

### 目标

引入第一版可控的 RL Agent，让系统能够在统一 observation 和受限 action 空间中学习更细粒度的择时/降仓/做市偏置，而不是直接交给黑盒无限制下单。

### 第一版策略定位

RL Agent 第一版**不是**执行层霸权代理，而是“受约束的策略代理器”：

1. 方向模式：输出 `BUY` / `SELL` / `HOLD` / `REDUCE`
2. 做市偏置模式：输出 `WIDEN_QUOTE` / `NARROW_QUOTE` / `BIAS_BID` / `BIAS_ASK`
3. 所有 RL 动作都要过 `ActionAdapter` 映射为现有运行时能理解的标准动作
4. 所有 RL 动作都必须经过风险守卫二次审核

### 具体任务

1. 新增 `modules/alpha/rl/environment.py`
2. 新增 `modules/alpha/rl/observation_builder.py`
3. 新增 `modules/alpha/rl/reward_engine.py`
4. 新增 `modules/alpha/rl/action_adapter.py`
5. 新增 `modules/alpha/rl/ppo_agent.py`
6. 新增 `modules/alpha/rl/rollout_store.py`
7. 新增 `modules/alpha/rl/evaluator.py`
8. 新增 `modules/alpha/rl/policy_store.py`
9. 新增 `modules/alpha/contracts/rl_types.py`
10. 扩展 `MetaLearner v2` / `OmniSignalFusion`，支持 `microstructure` 和 `rl` source

### 训练/验证路径

1. 训练输入由 `observation_builder.py` 聚合：
   - technical/onchain/sentiment/microstructure 特征
   - regime 信息
   - risk snapshot
   - inventory/position 状态
2. reward 要显式考虑：
   - realized pnl
   - unrealized pnl change
   - fees + slippage
   - drawdown penalty
   - turnover penalty
   - kill switch / rule violation penalty
3. 训练后先跑 OOS replay
4. OOS 通过后进入 paper/shadow
5. 未通过 promotion gate 的 RL policy 不允许替代主策略，只能作为 `SourceSignal` 辅助权重输入

### 关键解耦要求

1. `environment.py` 不直接知道交易所 API，只消费 replay/backtest/paper 环境接口。
2. `reward_engine.py` 只定义 reward，不直接更新仓位。
3. `action_adapter.py` 负责把 RL 动作映射成标准动作或 quote bias，RL policy 不直接提交订单。
4. `policy_store.py` 必须具备版本化、回滚和状态标记（candidate/shadow/active/retired）。

### 必打日志

| 级别 | 标签 | 内容 |
|------|------|------|
| INFO | `[RLPolicy]` | policy 训练完成、版本切换、shadow/paper 状态 |
| DEBUG | `[RLEnv]` | observation 维度、reward 分解、episode 边界 |
| DEBUG | `[RLAction]` | 原始动作、映射后动作、safety override |
| DEBUG | `[RLEval]` | episode return、max drawdown、turnover、win rate |
| WARNING | `[RLPolicy]` | reward 崩塌、过度换手、分布漂移、动作越界 |
| ERROR | `[RLEnv]` | observation schema 不一致、policy 载入失败、回放中断 |

### 验收标准

1. RL 训练、回测、paper、shadow 路径接口一致，且可回滚。
2. RL 动作从不直接绕过 `ActionAdapter` 与风险守卫。
3. 未通过门禁的 RL policy 只能降级为辅助信号，不会主导 live 决策。

---

## 6.4 W22：`SelfEvolutionEngine` 自进化闭环

### 目标

把 Phase 1 的持续学习、Phase 2 的多维 Alpha 与 Phase 3 的做市/RL 策略统一纳入自动评估、自动晋升、自动降权、自动暂停和自动回滚的演进闭环。

### 具体任务

1. 新增 `modules/evolution/self_evolution_engine.py`
2. 新增 `modules/evolution/scheduler.py`
3. 新增 `modules/evolution/candidate_registry.py`
4. 新增 `modules/evolution/ab_test_manager.py`
5. 新增 `modules/evolution/promotion_gate.py`
6. 新增 `modules/evolution/retirement_policy.py`
7. 新增 `modules/evolution/state_store.py`
8. 新增 `modules/evolution/report_builder.py`
9. 新增 `modules/alpha/contracts/evolution_types.py`
10. 集成以下现有能力：
    - `ContinuousLearner`
    - `ModelRegistry`
    - `MetaLearner v2`
    - `StrategyOrchestrator`
    - `BudgetChecker` / `KillSwitch`
    - `CCXTGateway` paper/shadow 结果

### 演进流程定义

1. 候选生成：
   - 新参数集
   - 新模型版本
   - 新 RL policy
   - 新策略组合权重
2. 候选先进入 `candidate`
3. 通过 replay/OOS 基线后进入 `shadow`
4. 通过 paper A/B 后进入 `active`
5. 连续表现不佳时执行：
   - `DEMOTE`
   - `PAUSE`
   - `RETIRE`
   - 必要时 `ROLLBACK`

### 自进化硬边界

1. Self-Evolution Engine 只管理版本与状态，不直接改代码。
2. 上线门禁必须同时看收益、回撤、稳定性和风险违规次数。
3. 任意自动晋升都必须留下持久化记录、变更摘要和回滚点。
4. 任意自动暂停都必须记录原因码和最近 30 天关键统计。

### 门禁建议

1. candidate -> shadow：
   - OOS Sharpe > 0.8
   - max_drawdown < 7%
   - 风险违规次数 = 0
2. shadow -> paper：
   - 7 天 shadow 行为稳定
   - 与基线相比无明显劣化
3. paper -> active：
   - A/B lift > 0
   - max_drawdown 不劣于基线
   - 订单拒绝/异常率低于阈值
4. active -> demote/retire：
   - 连续 30 天 Sharpe < 0.5 -> demote
   - 连续 60 天 Sharpe < 0 或重大风险违规 -> retire/rollback

### 必打日志

| 级别 | 标签 | 内容 |
|------|------|------|
| INFO | `[Evolution]` | 调度开始/结束、候选晋升、版本切换 |
| DEBUG | `[ABTest]` | control/test 指标、lift、样本量、显著性 |
| DEBUG | `[Promotion]` | 门禁明细、阈值判断、未通过原因 |
| DEBUG | `[Retirement]` | 降权、暂停、淘汰的触发指标 |
| WARNING | `[Evolution]` | 候选质量不足、paper 劣化、回滚触发 |
| ERROR | `[Promotion]` | 状态持久化失败、版本不一致、不可恢复异常 |

### 验收标准

1. 策略/模型/RL policy 的晋升和回滚都有可审计记录。
2. 自进化不会绕过 paper/shadow 验证直接切到 live。
3. 任一自动操作都能回答：为什么发生、依据哪些指标、如何回滚。

---

## 七、日志与可观测性设计

## 7.1 日志目标

Phase 3 的日志必须能回答四类问题：

1. **实时数据定位**：订单簿是不是完整、有没有 gap、延迟是否异常？
2. **做市定位**：为什么当前 quote 是这个价格和这个宽度？为什么撤单/改单？
3. **RL 定位**：当前 observation 是什么、policy 输出了什么、被哪个安全规则覆写了？
4. **演进定位**：为什么某个候选被晋升、降权、暂停或回滚？

### 7.2 统一日志字段

所有 Phase 3 模块必须尽量带上以下字段：

| 字段 | 含义 |
|------|------|
| `trace_id` | 单次决策/单个 quote/单个 episode 的全链路 ID |
| `loop_seq` | 主循环或 replay 序号 |
| `symbol` | 交易对 |
| `exchange` | 交易所 |
| `source_name` | technical / onchain / sentiment / microstructure / rl |
| `feed_seq` | 订单簿或成交流序列号 |
| `ws_latency_ms` | WebSocket 接收延迟 |
| `spread_bps` | 当前盘口或 quote 的点差 |
| `inventory_pct` | 当前库存占资金比例 |
| `position_pct` | 当前方向性仓位占比 |
| `risk_mode` | normal / reduced / blocked |
| `kill_switch_active` | Kill Switch 是否激活 |
| `policy_version` | 当前 RL policy 版本 |
| `candidate_id` | 当前演进候选标识 |
| `decision_reason` | 核心原因码摘要 |
| `elapsed_ms` | 模块耗时 |

### 7.3 日志层级规范

| 级别 | 允许内容 |
|------|---------|
| DEBUG | sequence 合并细节、quote 计算明细、reward 分解、A/B 指标 |
| INFO | 生命周期、连接状态、策略启停、版本切换、最终决策 |
| WARNING | 断流、降级、库存超限、paper 劣化、候选被拒 |
| ERROR | 状态持久化失败、schema 不一致、回滚失败、不可恢复异常 |

### 7.4 Trace 机制扩展

建议在现有 trace 基础上扩充以下结构：

```json
{
  "trace_id": "BTCUSDT-20260423T121530Z-OB-001245",
  "loop_seq": 1245,
  "symbol": "BTC/USDT",
  "realtime_feed": {
    "sequence_id": 8845123,
    "best_bid": 65210.5,
    "best_ask": 65211.0,
    "spread_bps": 0.77,
    "imbalance": 0.18,
    "ws_latency_ms": 42
  },
  "risk_snapshot": {
    "kill_switch_active": false,
    "budget_remaining_pct": 0.62,
    "current_drawdown": 0.018
  },
  "market_making": {
    "reservation_price": 65210.2,
    "optimal_spread_bps": 4.8,
    "inventory_pct": 0.11,
    "quote_action": "REFRESH"
  },
  "rl_policy": {
    "policy_version": "ppo_v1_20260423_01",
    "action": "WIDEN_QUOTE",
    "confidence": 0.64,
    "safety_override": false
  },
  "evolution": {
    "candidate_id": "policy_ppo_v1_20260423_01",
    "status": "shadow",
    "promotion_action": "HOLD"
  }
}
```

---

## 八、测试与验证策略

### 8.1 单元测试必须覆盖

1. `DepthCache`：
   - snapshot + delta 合并
   - sequence gap 检测
   - out-of-order 包拒绝
   - best bid/ask 正确更新
2. `RealtimeFeatureBuilder`：
   - spread/micro_price/imbalance 计算
   - stale 数据降级
3. `AvellanedaModel`：
   - reservation price 方向正确
   - inventory 越重 skew 越强
   - spread 随波动率上升而扩大
4. `QuoteLifecycle`：
   - refresh/cancel/repost 状态转换
   - quote timeout
   - partial fill 更新
5. `RewardEngine`：
   - 收益、费用、换手、drawdown 惩罚分解
6. `ActionAdapter`：
   - RL action -> 标准动作映射
   - 风险覆写路径
7. `PromotionGate` / `RetirementPolicy`：
   - 晋升、降权、暂停、回滚判断

### 8.2 集成测试必须覆盖

1. replay orderbook -> 做市策略 -> paper fill -> 风险守卫
2. replay observation -> RL policy -> action adapter -> FusionDecision
3. candidate -> shadow -> paper -> active 的完整演进状态机
4. WebSocket 断线重连 + snapshot recovery + 不脏写下游事件

### 8.3 回归要求

1. Phase 1 和 Phase 2 全量测试必须继续通过。
2. Phase 3 的新能力默认独立测试文件，不污染既有套件结构。
3. 所有 replay/paper/live 的契约差异必须通过测试明确覆盖，而不是依赖人工验证。

---

## 九、配置项建议

建议在 [core/config.py](../core/config.py) 与 `configs/system.yaml` 中新增：

```yaml
phase3:
  enabled: true
  realtime_feed_enabled: true
  market_making_enabled: false
  rl_agent_enabled: false
  self_evolution_enabled: false

  realtime_feed:
    reconnect_backoff_sec: 2
    heartbeat_timeout_sec: 15
    orderbook_depth_levels: 20
    snapshot_recovery_enabled: true
    max_gap_tolerance: 1

  market_making:
    risk_aversion_gamma: 0.12
    max_inventory_pct: 0.20
    quote_refresh_ms: 1500
    cancel_on_gap: true
    max_quote_age_sec: 10
    maker_only: true

  rl:
    training_enabled: false
    policy_mode: shadow
    reward_drawdown_penalty: 2.0
    reward_turnover_penalty: 0.2
    action_confidence_floor: 0.55
    max_episode_steps: 1000

  evolution:
    weekly_optimization_cron: "0 3 * * 0"
    shadow_days: 7
    paper_days: 7
    ab_min_samples: 100
    promote_min_sharpe: 0.8
    retire_max_drawdown: 0.10
    auto_rollback_enabled: true

  logging:
    realtime_debug: true
    market_making_debug: true
    rl_debug: true
    evolution_debug: true
    trace_sample_rate: 1.0
```

---

## 十、实际落地顺序建议

为了避免“全开工、全耦合、全返工”，Phase 3 的真实执行顺序建议如下：

1. 先落 `modules/data/realtime/` 与 replay 能力。
2. 再落 `OrderBookSnapshot` / `QuoteDecision` 等契约层。
3. 然后实现 `DepthCache` + `RealtimeFeatureBuilder` 的单测闭环。
4. 在 replay/paper 下实现 `MarketMakingStrategy` 与 `fill_simulator.py`。
5. 确认做市不会绕过风险守卫后，再扩展 `MetaLearner v2` 接入 `microstructure` source。
6. RL 第一版先只做 observation/reward/action adapter，不急于 live 推理。
7. 只有 replay/OOS/paper 都有结果后，才允许 `SelfEvolutionEngine` 接管候选晋升。
8. 最后才打开 Phase 3 配置项中的 `market_making_enabled`、`rl_agent_enabled`、`self_evolution_enabled`。

---

## 十一、最终验收清单

Phase 3 完成时，必须同时满足以下条件：

1. 系统能在统一事件契约下消费实时订单簿与回放订单簿。
2. 做市策略能在 replay/paper 中稳定运行并受 Phase 2 风控约束。
3. RL Agent 有完整的训练、评估、paper/shadow、回滚路径。
4. Self-Evolution Engine 能对模型/策略/policy 执行候选、晋升、降权、暂停、回滚。
5. 所有自动化行为都能在日志和 trace 中解释：为什么做、依据什么、结果如何。
6. 全量测试通过，并新增 Phase 3 的单测与集成测试集。

---

## 十二、总结

Phase 3 不是简单地“往系统里再塞一个做市策略和一个 RL 模型”。它的本质是把 AI Quant Trader 从“会预测、会风控”的系统，升级成“能看微观结构、能双边报价、能训练策略代理、还能自动演进”的系统。

真正的落地关键不在于公式和模型本身，而在于三件事：

1. **数据面必须真实、稳定、可回放**。
2. **策略面必须模块化、可降级、受风险守卫约束**。
3. **演进面必须可审计、可回滚、不能失控**。

只要这三件事成立，Phase 3 的四个子能力就能形成闭环：

- 实时订单簿提供微观结构信息优势
- 做市策略提供震荡市基础收益层
- RL Agent 提供更细粒度的策略代理与偏置控制
- Self-Evolution Engine 负责让系统持续筛选更优版本并淘汰劣版本

这才是“高级策略 + 自进化”真正可落地、可维护、可赚钱的实现方式。