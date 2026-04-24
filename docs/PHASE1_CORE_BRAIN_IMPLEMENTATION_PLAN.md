# Phase 1: 核心大脑升级详细实施计划

> 来源基线：[AI_QUANT_TRADER_EVOLUTION_STRATEGY.md](./AI_QUANT_TRADER_EVOLUTION_STRATEGY.md)
> 
> 目标阶段：Phase 1（8 周）
> 
> 阶段目标：从“单一 ML 预测 + 主循环直连”升级为“多策略标准接口 + 模块化特征中台 + 市场环境感知 + 动态策略编排 + 自动阈值校准 + 多模型集成”。

---

## 一、计划定位

本计划不是概念性路线图，而是面向当前代码基线的落地实施方案。它遵循两个硬性设计要求：

1. **极致模块化**：新增能力必须拆成稳定边界、低耦合、可替换模块，禁止继续把 Phase 1 逻辑堆进 [apps/trader/main.py](../apps/trader/main.py)。
2. **充足调试日志**：必须能通过日志复盘“某根 K 线进入系统后，经历了哪些模块、得到了什么中间结果、为何最终发出或放弃交易信号”。

---

## 二、当前代码锚点与实施假设

### 2.1 已有能力

当前仓库已经具备 Phase 1 的部分基础设施：

| 能力 | 现状锚点 | 结论 |
|------|---------|------|
| 策略基类 | [modules/alpha/base.py](../modules/alpha/base.py) | 可作为统一策略协议的基础 |
| 规则策略 | [modules/alpha/strategies](../modules/alpha/strategies) | 已有 MACross / Momentum，可迁移到统一接口 |
| ML 特征工程 | [modules/alpha/ml/feature_builder.py](../modules/alpha/ml/feature_builder.py) | 已有 60+ 特征生成能力，但还不是 DataKitchen 中台 |
| ML 推理器 | [modules/alpha/ml/predictor_v2.py](../modules/alpha/ml/predictor_v2.py) | 已有增量缓存、阈值注入、持仓同步能力 |
| 持续学习 | [modules/alpha/ml/continuous_learner.py](../modules/alpha/ml/continuous_learner.py) | 已有模型热替换基础 |
| 组合配置 | [modules/portfolio/allocator.py](../modules/portfolio/allocator.py) | 已有静态组合分配能力，可为后续策略编排提供基线 |

### 2.2 当前主要问题

| 问题 | 现状锚点 | 风险 |
|------|---------|------|
| 主循环承担了过多业务编排 | [apps/trader/main.py](../apps/trader/main.py) | 新增 Phase 1 功能后会继续膨胀，难以扩展和调试 |
| ML 能力存在，但缺少“中台化”组织 | [modules/alpha/ml](../modules/alpha/ml) | 难以叠加 DataKitchen、Regime、MetaLearner |
| 策略之间没有统一的上下文契约 | 当前策略各自消费 KlineEvent | 后续接入 Regime / Orchestrator 会变成横向耦合 |
| 日志虽然较多，但缺少贯穿式 trace | 现有 [modules/alpha/ml/predictor_v2.py](../modules/alpha/ml/predictor_v2.py) 与 [apps/trader/main.py](../apps/trader/main.py) | 难以验证整条运行链路 |

### 2.3 实施假设

Phase 1 的正确做法不是新增一批功能后再往主循环里接，而是先拆出运行时编排骨架，再把现有能力迁移进去。

**便宜验证点**：当前 [apps/trader/main.py](../apps/trader/main.py#L1491) 已经直接初始化 `MLPredictorV2`、`ContinuousLearner`，并在 [apps/trader/main.py](../apps/trader/main.py#L382) 附近直接喂数据，说明主循环就是当前最重的耦合点。Phase 1 计划必须优先解决这一点。

---

## 三、Phase 1 的范围定义

### 3.1 本阶段必须交付

1. 统一策略接口与策略注册中心
2. DataKitchen 特征中台 v1
3. 市场环境感知器 `MarketRegimeDetector`
4. 策略编排器 `StrategyOrchestrator`
5. Walk-Forward 阈值校准与 Optuna 参数优化管线
6. 多模型集成 `MetaLearner`
7. 贯穿式调试日志与可回放 trace 机制

### 3.2 本阶段明确不做

1. 不做 WebSocket order book 实时数据接入（留到 Phase 2）
2. 不做 Avellaneda 做市（留到 Phase 3）
3. 不做 RL Agent 训练（留到 Phase 3）
4. 不直接重构执行层 / 风控层的主职责，只增加必要接口

---

## 四、目标架构

### 4.1 Phase 1 目标分层

```
apps/trader/main.py
    └─ 只负责启动、装配、生命周期管理

modules/alpha/runtime/
    ├─ alpha_runtime.py           # Phase 1 运行时总协调器
    ├─ bar_context_builder.py     # 构建每根 K 线的统一上下文
    ├─ strategy_registry.py       # 策略注册、启停、发现
    ├─ signal_pipeline.py         # 策略执行 -> 编排 -> 输出
    └─ trace_recorder.py          # 贯穿式 trace 记录

modules/alpha/contracts/
    ├─ strategy_protocol.py       # 统一策略协议
    ├─ strategy_context.py        # 策略输入上下文
    ├─ strategy_result.py         # 策略输出结构
    ├─ regime_types.py            # RegimeState / RegimeSnapshot
    └─ ensemble_types.py          # Meta signal / model vote

modules/alpha/regime/
    ├─ detector.py                # MarketRegimeDetector
    ├─ feature_source.py          # Regime 所需特征提取
    ├─ scorer.py                  # HMM + rule hybrid scoring
    └─ cache.py                   # Regime 历史缓存

modules/alpha/orchestration/
    ├─ strategy_orchestrator.py   # 动态权重分配
    ├─ policy.py                  # Affinity Matrix / 冲突规则
    ├─ performance_store.py       # 近期 Sharpe / hit rate / drawdown
    └─ gating.py                  # 环境不明确时的降级规则

modules/alpha/ml/
    ├─ data_kitchen.py            # DataKitchen 中台入口
    ├─ feature_pipeline.py        # 特征流水线组合
    ├─ feature_selectors.py       # PCA / 去相关 / 特征筛选
    ├─ threshold_calibrator.py    # Youden's J / OOS 阈值校准
    ├─ ensemble.py                # ModelEnsemble
    ├─ meta_learner.py            # 多模型融合
    ├─ model_registry.py          # 模型版本 / 激活 / 回滚
    └─ diagnostics.py             # ML 诊断输出
```

### 4.2 模块化硬规则

1. `main.py` 不允许直接 import `MarketRegimeDetector`、`MetaLearner`、`Optuna` 训练逻辑。
2. 策略不允许直接读取全局 Trader 状态；一律通过 `StrategyContext` 输入。
3. Orchestrator 不直接依赖具体模型实现，只消费 `StrategyResult`。
4. DataKitchen 不知道“交易动作”，只负责数据准备和诊断输出。
5. 日志实现统一走 `core.logger.get_logger()`，禁止各模块自建日志格式。

---

## 五、核心接口设计

### 5.1 统一策略协议

```python
class StrategyProtocol(Protocol):
    strategy_id: str
    symbol: str
    timeframe: str

    def on_bar(self, context: StrategyContext) -> StrategyResult: ...
    def sync_position(self, quantity: float) -> None: ...
    def health_snapshot(self) -> dict: ...
```

### 5.2 统一策略上下文

```python
@dataclass
class StrategyContext:
    loop_seq: int
    trace_id: str
    symbol: str
    timeframe: str
    kline_event: KlineEvent
    latest_prices: dict[str, float]
    feature_frame: pd.DataFrame | None
    regime: RegimeState | None
    portfolio_snapshot: dict[str, Any]
    risk_snapshot: dict[str, Any]
    debug_enabled: bool
```

### 5.3 统一策略输出

```python
@dataclass
class StrategyResult:
    strategy_id: str
    symbol: str
    action: Literal["BUY", "SELL", "HOLD"]
    confidence: float
    score: float
    reason_codes: list[str]
    debug_payload: dict[str, Any]
```

### 5.4 编排器输入输出

```python
@dataclass
class OrchestrationInput:
    regime: RegimeState
    strategy_results: list[StrategyResult]
    performance_snapshot: dict[str, Any]
    equity: float
    current_drawdown: float

@dataclass
class OrchestrationDecision:
    selected_results: list[StrategyResult]
    weights: dict[str, float]
    block_reasons: list[str]
    debug_payload: dict[str, Any]
```

---

## 六、详细实施任务拆解

## 6.1 W1-W2：统一策略接口 + 运行时解耦

### 目标

把策略接入、上下文构建、结果收集从 [apps/trader/main.py](../apps/trader/main.py) 中拆出去，形成 Phase 1 的运行时骨架。

### 具体任务

1. 新增 `modules/alpha/contracts/strategy_protocol.py`
2. 新增 `modules/alpha/contracts/strategy_context.py`
3. 新增 `modules/alpha/contracts/strategy_result.py`
4. 新增 `modules/alpha/runtime/strategy_registry.py`
5. 新增 `modules/alpha/runtime/bar_context_builder.py`
6. 新增 `modules/alpha/runtime/signal_pipeline.py`
7. 将现有 MACross / Momentum / MLPredictorV2 迁移为统一 `on_bar()` 接口
8. `main.py` 只保留：
   - 启动配置加载
   - 依赖实例化
   - `AlphaRuntime` 装配
   - 事件循环入口

### 交付物

| 交付物 | 验收标准 |
|--------|---------|
| `StrategyProtocol` | 三类策略都能实现统一接口 |
| `StrategyRegistry` | 可按 symbol / type 注册、枚举、启停策略 |
| `BarContextBuilder` | 每根 bar 能产出完整 `StrategyContext` |
| `SignalPipeline` | 能汇总多个 `StrategyResult` |

### 必打日志

| 级别 | 标签 | 示例 |
|------|------|------|
| INFO | `[AlphaRuntime]` | 运行时初始化完成，注册策略数、symbol 数 |
| INFO | `[StrategyRegistry]` | 策略注册 / 卸载 / 启停 |
| DEBUG | `[BarContext]` | loop_seq、trace_id、symbol、是否有 feature/regime |
| DEBUG | `[SignalPipeline]` | 本 bar 参与策略列表、输出结果数量 |
| WARNING | `[StrategyRegistry]` | 重复注册、找不到策略、上下文缺失 |

### 验证

1. `pytest tests/test_ml_alpha.py -q`
2. 新增 `tests/test_alpha_runtime.py`
3. 验证日志中同一 `trace_id` 能串起：`BarContext -> Strategy -> SignalPipeline`

---

## 6.2 W3-W4：DataKitchen 特征中台 v1

### 目标

在保留现有 [modules/alpha/ml/feature_builder.py](../modules/alpha/ml/feature_builder.py) 的基础上，上移为可组合的数据准备中台，支持：

1. 特征流水线编排
2. PCA / 去相关 / 缺失值诊断
3. 训练与推理共用同一份特征契约
4. 为 RegimeDetector 和 MetaLearner 提供统一特征源

### 具体任务

1. 新增 `modules/alpha/ml/data_kitchen.py`
2. 新增 `modules/alpha/ml/feature_pipeline.py`
3. 新增 `modules/alpha/ml/feature_selectors.py`
4. 将当前 `MLFeatureBuilder` 作为 DataKitchen 的一个 stage，而不是最终入口
5. 增加 `FeatureContract`：
   - `feature_names`
   - `train_only_features`
   - `online_available_features`
6. 支持输出三份视图：
   - `alpha_features`
   - `regime_features`
   - `diagnostic_features`

### 充足日志要求

| 级别 | 标签 | 必须打印的字段 |
|------|------|---------------|
| INFO | `[DataKitchen]` | stage 数量、最终特征数、是否开启 PCA/去相关 |
| DEBUG | `[DataKitchen]` | 每个 stage 输入列数、输出列数、耗时 |
| DEBUG | `[FeatureDiag]` | NaN 比例、被剔除列、低方差列、相关性裁剪结果 |
| WARNING | `[DataKitchen]` | 某阶段输出空特征、关键列缺失 |
| ERROR | `[DataKitchen]` | 推理期特征与训练期签名不一致 |

### 验证

1. 新增 `tests/test_data_kitchen.py`
2. 保持 [tests/test_ml_alpha.py](../tests/test_ml_alpha.py) 通过
3. 增加一份 feature snapshot 文件，用于比对训练/推理签名

---

## 6.3 W5：市场环境感知器 `MarketRegimeDetector`

### 目标

引入独立的 Regime 子模块，不把环境识别塞进策略或 Predictor 里。

### 具体任务

1. 新增 `modules/alpha/regime/detector.py`
2. 新增 `modules/alpha/regime/feature_source.py`
3. 新增 `modules/alpha/regime/scorer.py`
4. 新增 `modules/alpha/contracts/regime_types.py`
5. 第一版采用“规则 + 统计”混合实现，不在第一周就强依赖完整 HMM 训练器
6. 输出统一 `RegimeState`：
   - `bull_prob`
   - `bear_prob`
   - `sideways_prob`
   - `high_vol_prob`
   - `confidence`
   - `dominant_regime`

### 实施策略

先做 `HybridRegimeDetectorV1`：

1. 波动率聚类
2. 收益趋势评分
3. ADX / RSI / volume trend 修正
4. 后续再替换为 HMM 实现，不影响调用方

### 日志要求

| 级别 | 标签 | 内容 |
|------|------|------|
| INFO | `[Regime]` | detector 启动、配置、更新频率 |
| DEBUG | `[Regime]` | 各特征原始值、各子评分、最终概率分布 |
| DEBUG | `[RegimeShift]` | dominant regime 发生切换时打印旧值/新值 |
| WARNING | `[Regime]` | 特征不足导致降级为 `unknown` |

### 验收标准

1. 连续输入 bar 时能稳定输出 `RegimeState`
2. 低样本时明确输出降级原因
3. `RegimeState` 可被单测稳定断言

---

## 6.4 W6：策略编排器 `StrategyOrchestrator`

### 目标

将“策略谁优先、权重如何调、何时整 bar 放弃交易”的决策从 `main.py` 和策略内部剥离。

### 具体任务

1. 新增 `modules/alpha/orchestration/strategy_orchestrator.py`
2. 新增 `modules/alpha/orchestration/policy.py`
3. 新增 `modules/alpha/orchestration/performance_store.py`
4. 新增 `modules/alpha/orchestration/gating.py`
5. Orchestrator 接收：
   - `RegimeState`
   - `StrategyResult[]`
   - 近期策略表现
   - 当前净值与回撤
6. 输出：
   - 最终被允许进入执行层的策略结果
   - 权重分配
   - 阻断原因
   - debug payload

### 关键解耦要求

1. 策略本身只对市场给出“建议”，不直接决定最终下单。
2. Orchestrator 不读 OHLCV 明细，只处理结构化结果。
3. PerformanceStore 只保存统计值，不依赖具体策略对象。

### 必打日志

| 级别 | 标签 | 内容 |
|------|------|------|
| INFO | `[Orchestrator]` | 当前 regime、启用策略数、最终通过策略数 |
| DEBUG | `[Orchestrator]` | 每个策略的原始 score、调整后权重、拒绝原因 |
| DEBUG | `[Policy]` | affinity matrix 命中项、gating 结果 |
| WARNING | `[Orchestrator]` | regime 低置信、全部阻断、性能数据缺失 |

### 验收标准

1. 同一根 bar 上，多个策略冲突时能稳定产出统一结果
2. 高波动/低置信 regime 下触发降仓或禁入
3. 所有决策都能在日志中回放

---

## 6.5 W7：Walk-Forward 阈值校准 + Optuna 自动优化

### 目标

把当前 Predictor 里的硬编码阈值 `0.60 / 0.40` 和策略手调参数，迁移为可训练、可追踪、可回滚的优化结果。

### 具体任务

1. 新增 `modules/alpha/ml/threshold_calibrator.py`
2. 新增 `modules/alpha/ml/model_registry.py`
3. 新增 `modules/alpha/ml/diagnostics.py`
4. 新增 `scripts/optimize_phase1_params.py`
5. 在 `WalkForwardTrainer` 上增加：
   - OOS 概率分布导出
   - Youden's J 最优阈值计算
   - 每折最优阈值统计
6. 为 MACross / Momentum / MLPredictor 的关键参数定义搜索空间

### 日志要求

| 级别 | 标签 | 内容 |
|------|------|------|
| INFO | `[Optuna]` | study 启动、trial 数、超时、目标函数 |
| DEBUG | `[OptunaTrial]` | trial 参数、分折得分、平均得分 |
| INFO | `[Threshold]` | 每折最佳阈值、最终阈值、样本外指标 |
| INFO | `[ModelRegistry]` | 新参数/阈值发布、版本号、回滚点 |
| WARNING | `[Optuna]` | trial 失败、参数越界、结果退化 |

### 验收标准

1. 输出一份结构化阈值文件和参数文件
2. Predictor 可在不改代码的情况下加载新阈值
3. 失败 trial 不影响主版本模型

---

## 6.6 W8：多模型集成 `MetaLearner`

### 目标

把单一模型提升为“可并行评估、可投票、可解释”的轻量集成框架，为 Phase 2 链上/情绪特征接入做好容器。

### 具体任务

1. 新增 `modules/alpha/ml/ensemble.py`
2. 新增 `modules/alpha/ml/meta_learner.py`
3. 新增 `modules/alpha/contracts/ensemble_types.py`
4. 第一版支持：
   - LightGBM
   - RandomForest
   - LogisticRegression 或 XGBoost（二选一，按依赖情况）
5. MetaLearner 输出：
   - `final_action`
   - `final_confidence`
   - `model_votes`
   - `dominant_model`
   - `debug_payload`

### 日志要求

| 级别 | 标签 | 内容 |
|------|------|------|
| INFO | `[Meta]` | 激活模型列表、融合方式（vote/stacking） |
| DEBUG | `[MetaVote]` | 各模型概率、阈值、票数、最终结果 |
| WARNING | `[Meta]` | 某模型缺席、某模型输出 NaN、自动降级单模型 |

### 验收标准

1. 单模型缺失时系统可降级运行
2. Meta 结果可解释，能打印 vote 细节
3. 保持与现有 `MLPredictorV2` 的兼容接入

---

## 七、日志与可观测性设计

## 7.1 日志目标

必须满足两类问题定位：

1. **运行逻辑验证**：某根 bar 为何没有出单？
2. **问题定位**：某根 bar 从特征到编排中哪一步数据不对？

### 7.2 统一日志字段

所有 Phase 1 模块必须尽量带上以下字段：

| 字段 | 含义 |
|------|------|
| `trace_id` | 单根 bar 的全链路 ID |
| `loop_seq` | 主循环序号 |
| `symbol` | 交易对 |
| `strategy_id` | 策略标识 |
| `model_version` | 模型版本 |
| `regime` | 当前主导环境 |
| `confidence` | 置信度 |
| `elapsed_ms` | 模块耗时 |

### 7.3 日志层级规范

| 级别 | 允许内容 |
|------|---------|
| DEBUG | 中间变量、特征维度变化、概率分布、策略裁决细节 |
| INFO | 生命周期、版本切换、关键里程碑、最终决策 |
| WARNING | 降级运行、配置缺失、数据不足、单模块失败但系统未中断 |
| ERROR | 无法继续的错误，必须带 traceback |

### 7.4 Trace 机制

建议新增 `modules/alpha/runtime/trace_recorder.py`，每根 bar 形成一份轻量 trace：

```json
{
  "trace_id": "BTCUSDT-20260423T120000Z-001245",
  "loop_seq": 1245,
  "symbol": "BTC/USDT",
  "feature_signature": "dk_v1_202604",
  "regime": {"dominant": "sideways", "confidence": 0.62},
  "strategy_results": [...],
  "orchestration": {...},
  "final_action": "BUY"
}
```

### 7.5 配置项建议

在 [core/config.py](../core/config.py) 和 `configs/system.yaml` 中新增：

```yaml
phase1:
  enabled: true
  debug_enabled: true
  trace_enabled: true
  trace_sample_rate: 1.0
  trace_dump_dir: ./logs/phase1_traces
  debug_symbols: [BTC/USDT, ETH/USDT]
  emit_feature_diagnostics: true
  emit_regime_details: true
  emit_orchestrator_details: true
```

---

## 八、测试与验收计划

## 8.1 单元测试

新增测试文件：

1. `tests/test_alpha_runtime.py`
2. `tests/test_data_kitchen.py`
3. `tests/test_regime_detector.py`
4. `tests/test_strategy_orchestrator.py`
5. `tests/test_threshold_calibrator.py`
6. `tests/test_meta_learner.py`

现有必须回归：

1. [tests/test_ml_alpha.py](../tests/test_ml_alpha.py)
2. [tests/test_portfolio.py](../tests/test_portfolio.py)

## 8.2 联调测试

每完成一个阶段都执行：

```powershell
uv run pytest tests/test_ml_alpha.py -q
uv run pytest tests/test_portfolio.py -q
uv run pytest tests/test_alpha_runtime.py -q
uv run pytest tests/test_data_kitchen.py -q
```

Phase 1 收尾时执行：

```powershell
uv run pytest -q
```

参考仓库记忆：2026-04-23 时全量测试为 `185/185 passed`，Phase 1 合并后应保持全量通过并新增针对新增模块的测试覆盖。

## 8.3 运行时验收清单

1. 启动后日志可见 `AlphaRuntime` / `StrategyRegistry` / `DataKitchen` / `Regime` / `Orchestrator` / `Meta` 六类模块日志。
2. 单根 bar 可通过同一个 `trace_id` 回溯完整链路。
3. Regime 缺失、特征不足、单模型失效时，系统可降级但不崩溃。
4. 新阈值和新模型可以热切换，并能回滚到上一个版本。

---

## 九、实施顺序与依赖关系

### 9.1 依赖图

```
W1-2 统一接口/运行时骨架
  └─ W3-4 DataKitchen
      ├─ W5 RegimeDetector
      ├─ W7 ThresholdCalibrator / Optuna
      └─ W8 MetaLearner
  └─ W6 StrategyOrchestrator

W5 + W6 + W8
  └─ AlphaRuntime Phase 1 最终接线
```

### 9.2 禁止反向依赖

1. `contracts/` 不依赖 `runtime/`。
2. `regime/` 不依赖 `orchestration/`。
3. `ml/` 不依赖 `apps/trader/`。
4. `main.py` 只依赖 `AlphaRuntime` 高层入口。

---

## 十、风险与回退策略

| 风险 | 影响 | 缓解 |
|------|------|------|
| 拆模块后主循环行为变化 | 可能导致现有策略行为漂移 | 每个阶段保留旧入口开关，双通路对比日志 |
| DataKitchen 输出签名变化 | 旧模型无法推理 | 引入 `FeatureContract` + 版本签名检查 |
| Regime 误判频繁 | Orchestrator 输出不稳定 | 低置信环境直接降级为静态权重 |
| MetaLearner 过度复杂 | 调试困难 | 第一版先做 vote / weighted average，不上复杂 stacking |
| 日志过量 | 影响性能与查阅 | 增加 `trace_sample_rate` 与 `debug_symbols` |

### 回退原则

1. 每周交付必须可由配置开关禁用。
2. 新模块合入前保留旧链路一周对照日志。
3. 所有模型、阈值、参数都必须版本化。
4. 任何热切换失败立即回滚上一个稳定版本。

---

## 十一、第一批落地动作

为保证 Phase 1 真正开始，而不是继续停留在方案层，建议按以下顺序启动实现：

1. 先落 `contracts/ + runtime/` 骨架，不先写 HMM、不先写 Meta。
2. 把 `MLPredictorV2`、MACross、Momentum 迁移到统一 `StrategyProtocol`。
3. 让 `main.py` 只通过 `AlphaRuntime` 调策略。
4. 然后再接 `DataKitchen` 和 `RegimeDetector`。
5. 等运行链路稳定、trace 打通后，再接 `Orchestrator` 和 `Optuna`。

这是 Phase 1 的正确启动顺序。先拆运行时骨架，后叠加智能能力，才能保持模块稳定和调试可控。

---

## 十二、阶段完成标准

Phase 1 只有同时满足以下条件才算完成：

1. `main.py` 不再承担策略编排细节。
2. 三类策略共享统一输入输出契约。
3. DataKitchen 成为训练与推理的统一特征入口。
4. Regime + Orchestrator + MetaLearner 能串成完整链路。
5. 单根 bar 的运行逻辑可以通过 `trace_id` 全程回放。
6. 新增测试通过，且全量测试不回退。

一旦这六项成立，Phase 1 就不是“增加几个模块”，而是完成了 AI 核心大脑的第一次体系化升级。
