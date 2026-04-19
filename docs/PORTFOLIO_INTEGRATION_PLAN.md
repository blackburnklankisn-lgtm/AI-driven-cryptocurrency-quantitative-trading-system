# 组合管理 & 连续学习 接入优化方案

> 生成日期: 2026-04-19
> 状态: 实施中

---

## 一、背景

系统中已实现但未接入主循环的 4 个模块：

| # | 模块 | 文件 | 状态 |
|---|------|------|------|
| 1 | PortfolioAllocator（组合配置器） | `modules/portfolio/allocator.py` | 4 种配置方法，未接入 |
| 2 | MeanVarianceOptimizer（均值方差优化器） | `modules/portfolio/optimizer.py` | Markowitz 优化，未接入 |
| 3 | PortfolioRebalancer（再平衡器） | `modules/portfolio/rebalancer.py` | 定期+漂移再平衡，未接入 |
| 4 | ContinuousLearner（连续学习器） | `modules/alpha/ml/continuous_learner.py` | 自动重训练，未接入 |

---

## 二、发现的 6 个架构优化点

### 2.1 预加载数据严重不足

**现状**：`main.py` 中 `preload_bars = 50`

**问题**：
- MLPredictorV2 需要 `min_buffer_size=300` 才能开始推理 → 目前需等 ~250 小时(≈10天)
- ContinuousLearner 需要 `min_bars_for_retrain=400` → 预加载 50 根毫无意义
- Allocator 的 `lookback_bars=60` 也需要足够的收益率历史

**方案**：将 `preload_bars` 增大到 **500**。HTX API `fetch_ohlcv` 支持 `limit=1000`，500 根 1h K 线只需 3 次 API 调用（每品种 1 次），启动延迟约 3-5 秒。

---

### 2.2 主循环缺少收益率跟踪

**现状**：`_main_loop_step()` 只记录 `_latest_prices`，不计算每根 K 线收益率。

**问题**：`PortfolioAllocator.update_return(symbol, period_return)` 需要周期收益率。

**方案**：新增 `_prev_closes` dict，每根 K 线计算 `(close - prev_close) / prev_close`。

---

### 2.3 订单管道缺少再平衡入口

**现状**：订单只有一条路径：策略 → `_process_order_request()` → RiskManager → OrderManager

**问题**：`RebalanceOrder` 和 `OrderRequestEvent` 是两个不同的数据结构。

**方案**：增加 `_process_rebalance_orders()` 方法，将 `RebalanceOrder` 转换为 `OrderRequestEvent` 并复用风控→发单管道。`strategy_id` 设为 `"portfolio_rebalancer"`，再平衡订单不经过 PositionSizer（qty 已由 Allocator 精确计算）。

---

### 2.4 策略信号与再平衡可能冲突

**现状**：每根 K 线先让 7 个策略各自独立发信号，无全局协调。

**问题**：MACross 对 ETH 发出卖出 vs Rebalancer 判定 ETH 低配需买入 → 矛盾。

**方案**：采用 **"策略信号 → 组合层裁决 → 执行"** 三阶段架构：
- 再平衡触发时，再平衡订单优先，策略信号暂缓
- 未触发再平衡时，策略信号正常通过

---

### 2.5 system.yaml 缺少组合管理和连续学习的配置节

**现状**：配置文件只有 `exchange`、`data`、`risk`、`logging` 四个节。

**方案**：扩展两个配置节：`portfolio` 和 `continuous_learning`。

---

### 2.6 PerformanceAttributor 完全未接入

**现状**：代码完整但零接入。`_on_fill()` 不记录归因数据。

**问题**：ContinuousLearner 需要预测准确率反馈；Allocator 动量加权需要策略维度的收益率。

**方案**：在 `_on_fill()` 中接入 `record_trade()`，在 `_update_account_snapshot()` 中接入 `record_price()`。

---

## 三、执行计划

| 步骤 | 优化项 | 优先级 | 改动量 | 收益 |
|:---:|--------|:------:|:------:|------|
| 1 | 扩展 system.yaml + config.py 配置节 | 前置 | ~40 行 | 后续所有模块接入可配置化 |
| 2 | 预加载 50→500 根 K 线 | 前置 | ~5 行 | ML 策略启动即可推理 |
| 3 | 主循环增加收益率跟踪 | 前置 | ~10 行 | Allocator/Optimizer 数据前置 |
| 4 | 接入 PerformanceAttributor | P0 | ~30 行 | 策略评估基础设施 |
| 5 | 接入 ContinuousLearner | P0 | ~50 行 | ML 模型自适应进化 |
| 6 | 接入 Allocator + Rebalancer（含冲突裁决） | P1 | ~80 行 | 组合级资本配置 |

步骤 1-3 是所有后续接入的**共同前置条件**。4 和 5 可并行（互不依赖），最后做 6。

---

## 四、涉及文件清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `configs/system.yaml` | 修改 | 增加 portfolio / continuous_learning 配置节 |
| `core/config.py` | 修改 | 增加 PortfolioConfig / ContinuousLearningConfig dataclass |
| `apps/trader/main.py` | 修改 | 主要集成点：预加载、收益率、归因、CL、再平衡 |
| `modules/portfolio/__init__.py` | 不变 | 已导出所有需要的类 |
| `modules/alpha/ml/continuous_learner.py` | 不变 | 代码已完成 |
| `modules/alpha/ml/predictor_v2.py` | 可能微调 | 增加 CL 模型热替换钩子 |

---

## 五、风险与回退

- **预加载 500 根**：若 HTX API 限速导致启动变慢 → 加 `time.sleep(0.5)` 间隔或降至 300 根
- **再平衡与策略冲突**：采用"再平衡优先"简单规则，避免复杂仲裁逻辑
- **ContinuousLearner 重训耗时**：在后台线程执行，不阻塞主循环；失败时保留旧模型
- **所有变更均可通过 system.yaml 配置开关禁用**（`portfolio.enabled: false` / `continuous_learning.enabled: false`）
