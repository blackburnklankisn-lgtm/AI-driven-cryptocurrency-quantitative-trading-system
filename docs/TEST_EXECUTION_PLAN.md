# AI_QUANT_TRADER_EVOLUTION_STRATEGY — Paper 模拟盘全功能测试与验证执行计划

更新时间：2026-04-24

## 1. 测试目标

在 paper 模拟盘模式下，对 AI_QUANT_TRADER_EVOLUTION_STRATEGY 技术方案全量功能进行自动化测试与验证，确保：

1. 所有已实现功能符合设计规格和生产级要求
2. 测试代码覆盖率 ≥ 90%
3. 全部 1061+ 测试用例通过，无回归

---

## 2. 覆盖率基线与差距分析

**当前状态（2026-04-24 基线）：**
| 指标 | 数值 |
|---|---|
| 总测试用例数 | 1061 |
| 通过率 | 100% |
| 总语句数 | 23,379 |
| 已覆盖语句 | 20,333 |
| **总体覆盖率** | **87%** |

**主要覆盖率缺口（按影响排序）：**

| 模块 | 当前覆盖率 | 缺失行数 | 优先级 |
|---|---|---|---|
| `scripts/optimize_phase1_params.py` | 23% | 128 | 中（脚本逻辑） |
| `modules/risk/position_sizer.py` | 46% | 28 | P0 — 核心风控 |
| `modules/execution/gateway.py` | 52% | 95 | P0 — 执行网关/Paper模式 |
| `modules/data/realtime/ws_client.py` | 56% | 201 | P1 — WebSocket |
| `modules/data/storage.py` | 69% | 24 | P1 — 存储层 |
| `modules/risk/state_store.py` | 74% | 22 | P0 — 状态持久化 |
| `modules/data/realtime/subscription_manager.py` | 73% | 43 | P1 — 订阅管理 |
| `modules/evolution/state_store.py` | 80% | 28 | P0 — 演进状态 |
| `modules/evolution/self_evolution_engine.py` | 85% | 40 | P0 — 演进引擎 |
| `modules/risk/manager.py` | 85% | 24 | P0 — 风控管理器 |
| `modules/data/sentiment/providers.py` | 89% | 38 | P1 — 情绪Provider |

**目标：**
- 总体覆盖率从 87% → 90%+
- 需新增覆盖约 **700 条语句**
- 新增测试用例约 **200 个**

---

## 3. 执行计划（分步实施）

### 第一步：现有测试完整运行基线确认 ✅ 已完成
- 运行 `pytest tests/ -q` → 1061 passed
- 运行覆盖率 → 87% 基线确认

### 第二步：编写新测试（本次实施重点）

#### 2a. Paper 模拟盘核心路径测试 — `test_paper_trading_integration.py`
涵盖：
- `CCXTGateway` paper 模式下单/撤单/查询/余额
- Paper 市价单含滑点成交
- Paper 限价单成交
- Paper 余额不足拒单
- Paper 持仓不足拒单
- Paper 多轮买卖生命周期
- `PositionSizer` 全部 4 种方法（fixed_notional / fixed_risk / volatility_target / fractional_kelly）
- `PositionSizer` 硬性上限约束
- 负 Kelly 返回 0
- 止损距离接近 0 返回 0

#### 2b. 风控状态持久化测试 — `test_risk_state_store.py`
涵盖：
- `StateStore.save/load/delete/keys/wipe/diagnostics`
- 原子写入（tmp 文件替换）
- 文件不存在时返回 None
- 损坏 JSON 文件的容错处理
- 多 key 隔离
- `datetime` 序列化/反序列化 round-trip
- 并发线程安全

#### 2c. 演进引擎状态存储测试 — `test_evolution_state_store.py`
涵盖：
- `EvolutionStateStore` 全部接口
- `append_decision / load_decisions`（JSONL 追加 + 加载最新 N 条）
- `append_retirement / load_retirements`
- `save_report / load_report`（原子覆写）
- `save/load_scheduler_state`
- `save/load_weekly_params_optimizer_state`
- `append/load_weekly_params_optimizer_runs`
- `diagnostics`
- 文件不存在时的容错路径
- 损坏文件的异常处理

#### 2d. Parquet 存储层测试 — `test_parquet_storage.py`
涵盖：
- `ParquetStorage.write/read/list_available/get_latest_timestamp`
- 空 DataFrame 跳过写入
- 增量追加去重
- 时间范围过滤（since/until）
- 文件不存在返回 None
- 时间戳 UTC 转换

#### 2e. Paper 模拟盘完整交易生命周期集成测试 — `test_paper_integration_full.py`
涵盖：
- 从策略信号到 paper 成交的完整链路
- MACrossStrategy 在 paper 模式下生成买卖信号
- 风控检查 + paper 下单 + 余额更新
- 多品种并发 paper 交易
- 演进引擎在 paper 模式下的候选注册
- Risk 层在 paper 模式下的状态持久化

### 第三步：运行新测试 + 覆盖率测量
```bash
pytest tests/ --cov=. --cov-report=term-missing --cov-report=html:docs/coverage_html -q
```

### 第四步：Bug 修复与优化
- 根据测试失败修复逻辑缺陷
- 修复边界条件处理
- 优化错误消息一致性

### 第五步：最终验收
- 覆盖率 ≥ 90%
- 所有测试 passed
- docs/coverage_html 更新

---

## 4. 功能验证矩阵

| 功能模块 | 验证场景 | 对应测试文件 |
|---|---|---|
| Phase 1 — 核心大脑 | ML预测、阈值校准、策略信号 | test_ml_alpha.py, test_alpha_strategies.py |
| Phase 1 — Regime检测 | 市场状态分类 | test_regime_detector.py |
| Phase 1 — Orchestrator | 多策略协调 | test_orchestration.py |
| Phase 2 — 风控 | KillSwitch/Budget/AdaptiveRisk | test_phase2_risk.py, test_phase2_w11.py |
| Phase 2 — 仓位计算 | 4种仓位方法+硬限制 | **test_paper_trading_integration.py（新）** |
| Phase 2 — 风控状态持久化 | JSON原子写入/读取 | **test_risk_state_store.py（新）** |
| Phase 3 — Paper模拟盘 | 全完整成交生命周期 | **test_paper_trading_integration.py（新）** |
| Phase 3 — 外部Provider | Glassnode/CryptoQuant/CryptoCompare | test_external_providers_keyed.py |
| Phase 3 — 情绪数据 | 多Provider降级合约 | test_phase2_w13.py |
| Phase 3 — 链上数据 | 多Provider特征构建 | test_phase2_w12.py |
| 自进化引擎 | 候选注册/晋升/淘汰/A-B | test_phase3_w22.py |
| 自进化引擎 — 状态存储 | JSONL追加/原子覆写 | **test_evolution_state_store.py（新）** |
| 参数优化 | Optuna周级调度审计 | test_optimize_phase1_params.py |
| 存储层 | Parquet增量写入/读取 | **test_parquet_storage.py（新）** |
| 执行网关 | Paper/Live模式隔离 | test_execution.py + **test_paper_trading_integration.py（新）** |
| 监控 | trace/metrics | test_phase3_trace_and_main.py |
| 安全 | 审计日志/OWASP | test_security_audit.py |

---

## 5. Paper 模拟盘验证关键场景

| 场景 | 预期行为 | 验证方式 |
|---|---|---|
| 市价单买入+滑点 | fill_price = market_price × (1+0.1%) | 断言 fill_price 含 0.1% 滑点 |
| 限价单买入 | fill_price = limit_price | 断言 fill_price == price 参数 |
| 余额不足拒单 | 抛出 OrderSubmissionError | pytest.raises |
| 持仓不足卖单拒单 | 抛出 OrderSubmissionError | pytest.raises |
| 多轮完整买卖 | 最终现金 = 初始 - 手续费（round-trip） | 断言 cash_after == expected |
| 风控阻断后无成交 | paper 现金不变 | 比较前后 paper_cash |
| 并发下单线程安全 | 不出现 race condition | 多线程并发测试 |

---

## 6. 测试文件输出清单

| 文件 | 位置 | 测试数 |
|---|---|---|
| `TEST_EXECUTION_PLAN.md` | `docs/` | — |
| `test_paper_trading_integration.py` | `tests/` | ~60 |
| `test_risk_state_store.py` | `tests/` | ~25 |
| `test_evolution_state_store.py` | `tests/` | ~30 |
| `test_parquet_storage.py` | `tests/` | ~20 |
| `COVERAGE_REPORT.md` | `docs/` | — |

---

## 7. 完成标准

- [ ] `pytest tests/ -q` 全部通过（0 failures, 0 errors）
- [ ] 总体覆盖率 ≥ 90%
- [ ] 关键模块（position_sizer / gateway / state_store）覆盖率 ≥ 90%
- [ ] `docs/coverage_html/index.html` 生成完整报告
- [ ] `docs/COVERAGE_REPORT.md` 记录最终覆盖率明细
