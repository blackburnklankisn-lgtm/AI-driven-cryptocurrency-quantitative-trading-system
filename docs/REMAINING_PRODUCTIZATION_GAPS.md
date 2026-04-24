# AI_QUANT_TRADER_EVOLUTION_STRATEGY 剩余未 100% 产品化能力清单

更新时间: 2026-04-24

本文档只记录在“计划文档中每一条能力都已 100% 产品化并完成真实联调”这一验收口径下，当前仍然存在的真实缺口。模块存在但未接入 runtime、未形成自动闭环、或仅有脚本/测试而未成为默认产品能力，均视为未 100% 完成。

## 已不再列为缺口的能力

以下能力已经具备 runtime 接线和验证证据，因此不再列入剩余清单：

1. Phase 1 Regime + Orchestrator 主链路接入
2. Phase 2 AdaptiveRiskMatrix + BudgetChecker + KillSwitch 主链路接入
3. Phase 3 paper/shadow + realtime feed + HTX 公共行情接入
4. Portfolio / Rebalancer / ContinuousLearner 热替换 / PerformanceAttributor 主链路接入
5. 外部低频数据 `onchain` + `sentiment` 的真实 provider、配置、对齐、特征透传与主链路合并

## 剩余缺口

### P0. 参数进化没有形成“周级自动优化 -> A/B 验证 -> runtime 默认消费”的完整闭环

计划文档承诺：

1. `AI_QUANT_TRADER_EVOLUTION_STRATEGY.md` 将自进化的第一维定义为“参数进化: Optuna 每周优化策略参数”。
2. `PHASE1_CORE_BRAIN_IMPLEMENTATION_PLAN.md` 的 W7 明确要求 “Predictor 可在不改代码的情况下加载新阈值”。

当前真实状态：

1. `scripts/optimize_phase1_params.py` 已能离线产出 `threshold_*.json` 与 `best_params_*.json`。
2. `apps/trader/main.py` 已能在 runtime 启动时加载 `models/{symbol}_threshold.json` / `models/{symbol}_best_params.json`，并在主循环中自动监听 alias 工件变化后 rollout 新参数候选。
3. `optuna` 现已纳入项目依赖，周级优化与 CLI 优化默认都走同一套托管依赖。
4. 风控参数参与 Optuna 优化、BacktestEngine 级统一参数搜索、以及非 ML 策略参数的统一参数闭环仍未落地。

本轮已推进：

1. runtime 侧开始优先加载 `models/{symbol}_threshold.json` 与 `models/{symbol}_best_params.json`。
2. `scripts/optimize_phase1_params.py` 开始额外写出稳定别名文件，供 runtime 直接消费。
3. `LiveTrader` 已为参数候选增加独立于 model candidate 的 binding slot，避免继续复用单 `strategy_id -> candidate_id` 映射而造成双归因。
4. 参数候选现已可注册为 `CandidateType.PARAMS`，保存 `thresholds + trainer model_type/model_params` 的 runtime snapshot，并在 `SelfEvolutionEngine` 的 `active_snapshot` 下恢复默认阈值与训练参数。
5. `LiveTrader` 主循环现已会检测 `models/{symbol}_threshold.json` / `models/{symbol}_best_params.json` 的稳定 alias 工件变化，并在检测到新工件时自动 rollout 到 runtime，切换 params owner 槽位并开始真实样本采集。
6. `scripts/optimize_phase1_params.py` 已抽出 `optimize_params_from_dataframe()` 可复用入口；`LiveTrader` 主循环现已根据 `weekly_optimization_cron` 触发周级后台优化任务，使用真实 `gateway.fetch_ohlcv()` 数据构建 DataFrame，写出 alias 工件，并把最近一次调度状态持久化到 `storage/phase3_param_optimization`。

仍未 100% 的部分：

1. 风控参数和非 ML 策略参数尚未接入同一优化闭环。
2. `scripts/optimize_phase1_params.py` 的 CLI 默认已要求真实数据；合成数据仅保留为显式 `--allow-synthetic-data` 开发开关，不再是默认 fallback。

建议下一步：

1. 把风控参数与非 ML 策略参数纳入同一参数优化入口，而不是只覆盖 ML 阈值/模型训练参数。
2. 若希望进一步收紧口径，可以彻底删除显式合成数据开关，只保留真实数据路径。

### P0. Self-EvolutionEngine 仍未形成自动候选注册与指标回灌闭环

计划文档承诺：

1. `AI_QUANT_TRADER_EVOLUTION_STRATEGY.md` 明确要求“策略进化: 自动评估策略组合，淘汰劣策略、引入新策略”。
2. `PHASE3_ADVANCED_STRATEGY_SELF_EVOLUTION_IMPLEMENTATION_PLAN.md` 把 `SelfEvolutionEngine` 定义为参数调优、候选策略 A/B、晋升/降级/暂停闭环的统一协调器。

当前真实状态：

1. `SelfEvolutionEngine` 模块本身已经完成，`main.py` 也会在 Phase 3 shadow 步骤中周期性调用 `run_cycle(force=False)`。
2. 本轮新增了主链路接线：
	- ML 策略初始加载时会自动注册模型候选
	- ContinuousLearner 热替换新模型时会自动注册新的模型候选
	- Phase 3 MarketMakingStrategy 初始化时也会注册对应的 strategy 候选，并绑定到 `phase3_mm_<symbol>`
	- Phase 3 RL policy 初始化时也会注册对应的 policy 候选，并绑定到 `phase3_rl_<version>` 订单 `strategy_id`
	- `PerformanceAttributor` 已提供按策略聚合的保守指标快照，`_on_fill()` 会把 `sharpe/max_drawdown/win_rate` 回灌给当前候选
	- 真实风控拒单路径（KillSwitch / AdaptiveRisk / Budget / RiskManager）会把 `risk_violations` 计数回灌给当前候选
 	- 初始 runtime 候选会被提升为 active baseline，后续同 family 候选可自动创建 A/B 实验
 	- control 侧样本可从历史 realized sell-trade PnL bootstrap，test 侧样本会在真实卖出成交时自动写入
 	- 满足最小样本后会自动 conclude A/B，并把 `ab_lift` 回写到候选指标
	- SelfEvolutionEngine 现已保证同一 family 仅保留一个 active 候选；新 active 晋升会暂停旧 active，rollback 也会重新激活上一版候选
 	- 对 ML 候选，`run_cycle()` 返回的 active snapshot 已可同步回 `LiveTrader`，恢复对应模型对象与阈值
	- 对 RL policy 与做市候选，`run_cycle()` 返回的 active snapshot 也已可同步回 `LiveTrader`，恢复 `_phase3_ppo` / `_phase3_mm`
 	- 对做市候选，paper fill 仿真的 realized PnL 已会沉淀为历史样本，驱动保守 `sharpe/max_drawdown/win_rate` 快照、A/B control/test 样本，以及 `INVENTORY_HALT` / `RISK_BLOCKED` 类风险违规回灌
3. 因此，runtime 已不再只是“空跑调度器”，而是开始向演进引擎持续输送模型版本与真实成交表现。
4. 当前 metrics/risk 回灌已覆盖 ML 模型候选、RL policy 候选和做市候选；参数候选现已具备独立 binding slot、runtime restore 与 alias 工件变更驱动的自动 rollout，不再受单映射双归因问题阻塞。
5. A/B lift 与演进状态切换已开始闭环，且 ML/RL/MM runtime 对象恢复已打通；参数集的 runtime activation/rollback 也已具备独立 snapshot/restore 与默认 rollout 基础，周级工件生产也已由 runtime 调度器自动化。

本轮已推进（第二批）：

1. `SelfEvolutionEngine` 现已新增 weekly optimizer 调度状态与审计能力：
   - `weekly_params_optimizer_cron` 纳入 `SelfEvolutionConfig`
   - `get_due_weekly_params_optimizer_slot(now)` 负责判断当前时刻是否触发，且已内置防重入（same slot 只触发一次）
   - `record_weekly_params_optimizer_start(slot_id)` / `record_weekly_params_optimizer_finish(slot_id, ...)` 分别写入 state + 追加 audit JSONL
   - `EvolutionStateStore` 新增 `weekly_params_optimizer_state.json` 和 `weekly_params_optimizer_runs.jsonl` 双层持久化
   - `diagnostics()` 已暴露 `weekly_params_optimizer` 字段
2. `LiveTrader` 已将 weekly optimizer 状态从 `EvolutionStateStore("storage/phase3_param_optimization")` 迁移到 `SelfEvolutionEngine` 统一管理：
   - `_phase3_params_optimizer_state_store` 不再独立创建
   - `_save_phase3_params_optimizer_state` 优先通过 `evolution.save_weekly_params_optimizer_state()` 写入
   - `_maybe_start_weekly_ml_params_optimization` 优先通过 `evolution.get_due_weekly_params_optimizer_slot()` 判断 due
   - `_run_weekly_ml_params_optimization` 完成时优先通过 `evolution.record_weekly_params_optimizer_finish()` 写入 audit
3. 统一 params target 集合 `_collect_phase3_param_optimization_targets()` 已接入：
   - ML strategies → `target_kind: ml_strategy`
   - 非 ML strategies（MACrossStrategy / MomentumStrategy）→ `target_kind: strategy_params`
   - risk 运行时（RiskManager / AdaptiveRiskMatrix / BudgetChecker / KillSwitch / PositionSizer）→ `target_kind: risk_params`
4. 非 ML 策略参数和 risk 参数已可注册为 `CandidateType.PARAMS` baseline candidate，并具备独立 snapshot / restore / rollback 调用链：
   - `_extract_strategy_params_payload()` / `_extract_risk_params_payload()` 提取 payload
   - `_register_evolution_strategy_params_candidate()` / `_register_evolution_risk_params_candidate()` 注册
   - `_restore_strategy_params_candidate_runtime_state()` / `_restore_risk_params_candidate_runtime_state()` restore
   - `_restore_params_candidate_runtime_state()` 统一分发，`_restore_evolution_slot_runtime_state()` 只调这一个入口
5. `add_strategy()` 现在会在非 ML 策略注册时自动调用 `_register_evolution_strategy_params_candidate()`

仍未 100% 的部分：

1. 非 ML 策略参数和 risk 参数的 Optuna 自动优化（即定期优化这些 target 的闭环）还没有做——目前只有 baseline 注册与 restore。
2. 参数候选的晋升门禁指标（sharpe/drawdown/win_rate）对非 ML / risk target 还没有指标回灌路径。

建议下一步：

1. 若要完成非 ML / risk params 的完整 A/B / 晋升门禁闭环，需要为 strategy_params / risk_params kind 的候选设计一条单独的指标回灌路径（例如 portfolio-level drawdown / win_rate）。
2. 若只关注 ML params 的完整闭环，当前状态已满足验收口径：cron 触发 -> 真实 OHLCV 数据 -> optuna 优化 -> runtime artifact 写入 -> candidate 注册 -> promotion/rollback -> runtime active 切换 -> audit 写入 engine state。

### ~~P1. 带密钥的外部数据 provider 仍不是"真实可用实现"~~ ✅ 已完成

> **已于当前迭代补全，不再是缺口。**

完成状态：

| Provider | 文件 | 端点 | 认证 | 状态 |
|---|---|---|---|---|
| `GlassnodeProvider` | `modules/data/onchain/providers.py` | `addresses/active_count`、`transfers_volume_*`、`mining/hash_rate_mean`、`indicators/nvt`、`market/marketcap_usd`（BTC+USDT） | query param `api_key=` | ✅ 真实实现 |
| `CryptoQuantProvider` | `modules/data/onchain/providers.py` | `btc/network-data/active-addresses`、`btc/exchange-flows/inflow`、`btc/transactions/large-transactions-count`、`btc/miner-flows/miner-reserve`、`btc/market-data/price-ohlcv`、`stablecoin/all/total-supply`、`btc/market-data/nvt-ratio` | `Authorization: Bearer {api_key}` | ✅ 真实实现 |
| `CryptoCompareProvider` | `modules/data/sentiment/providers.py` | `/social/coin/latest`（fear/greed proxy）、`/futures/v1/funding-rates/by-exchange`、`/futures/v1/open-interest/history/days`、`/v2/histohour` | `Authorization: Apikey {api_key}` | ✅ 真实实现 |

降级合约（统一）：
- 无 API Key → 全字段 `None`，`metadata={"degraded": True, "reason": "no_api_key"}`，不抛出异常
- API Key 存在但请求失败（含 429 限流 / 401 认证失败 / HTTP 错误）→ 抛出 `OnChainFetchError` / `SentimentFetchError`
- `CryptoCompareProvider` 各子请求（social / funding / OI）独立降级，单点失败不影响其余字段

测试覆盖：`tests/test_external_providers_keyed.py`（26 个测试，全部 ✅）

## 当前推荐实施顺序

1. 先补齐参数进化闭环，因为这是最明确的 P0 缺口，而且已经有脚本和 runtime 接入基础。
2. 再补 Self-EvolutionEngine 的 candidate/metrics/promotion 主链路闭环。
3. 最后视验收口径决定是否继续补全付费 provider。