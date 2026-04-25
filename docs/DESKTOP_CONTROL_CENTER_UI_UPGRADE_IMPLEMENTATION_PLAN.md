# AI_QUANT_TRADER_EVOLUTION_STRATEGY — 桌面交易控制台 UI 升级实施计划

更新时间：2026-04-24

## 1. 计划目标

将当前桌面端从“旧版单页监控板”升级为围绕 **Alpha Brain、Evolution、Risk Matrix、Data Fusion** 的桌面交易控制台，使前端 UI 与本次 AI_QUANT_TRADER_EVOLUTION_STRATEGY 技术升级后的能力保持一致。

本计划的目标不是换皮，而是完成以下三类升级：

1. **信息架构升级**：从单页监控视图升级为多工作区控制台
2. **数据契约升级**：把后端已实现的核心能力聚合为前端可消费的结构化快照与实时事件流
3. **交互能力升级**：从“看系统是否在运行”升级为“理解系统为何这样决策、当前风险处于何种状态、演进引擎如何工作”

---

## 2. 当前现状与核心差距

### 2.1 当前桌面端现状

当前桌面端主要集中在单文件实现：

- `apps/desktop-client/src/App.tsx`

当前页面主要展示：

1. 总净值
2. 三个价格 ticker
3. 单一 K 线图
4. 持仓列表
5. 熔断状态
6. 控制按钮（stop/reset circuit）
7. Gemini 文本分析
8. 审计日志流

当前后端面向桌面端开放的主要接口集中在：

- `GET /api/v1/status`
- `GET /api/v1/klines`
- `POST /api/v1/control`
- `WS /api/v1/ws/status`
- `WS /api/v1/ws/logs`

### 2.2 与升级后技术方案的差距

根据 `docs/AI_QUANT_TRADER_EVOLUTION_STRATEGY.md`，本次系统升级后的核心能力包括：

1. **Market Regime Detector**：市场环境感知器
2. **Strategy Orchestrator**：策略编排器
3. **Self-Evolution Engine**：自进化引擎
4. **Continuous Learner**：持续学习与模型阈值更新
5. **Risk Matrix**：预算检查、Kill Switch、DCA、追踪止损、ROI 阶梯止盈、冷却期保护
6. **Data Fusion**：行情、订单簿、链上数据、情绪数据、订阅健康状态

当前 UI 的主要缺口：

1. **没有 Alpha Brain 视图**：看不到 regime 概率分布、dominant regime、confidence、稳定性
2. **没有策略编排结果视图**：看不到各策略权重、门控决策、阻断原因
3. **没有演进工作台**：看不到 candidate、shadow、paper、active、retired 生命周期状态
4. **没有 Risk Matrix 面板**：看不到 budget、kill switch、cooldown、DCA、出场计划
5. **没有数据健康面板**：看不到行情订阅健康、重连次数、数据源 freshness
6. **没有结构化执行视图**：订单、成交、滑点、手续费、paper 交易链路仍隐含在日志里
7. **没有针对升级后能力的聚合 API**：后端能力存在，但前端没有结构化快照可直接消费

---

## 3. 升级原则

本次 UI 升级遵循以下原则：

1. **控制台优先，不做营销页**
   前端定位是交易控制中心，不是展示页。

2. **结构化优先，不依赖大段自然语言解释**
   AI 文本分析保留，但降级为解释层；核心判断必须以结构化指标、状态图和决策卡片呈现。

3. **先数据契约，后组件堆叠**
   不在现有 `status` 接口上持续追加字段；改为统一设计 Dashboard Snapshot 与领域快照。

4. **工作区拆分优先于单页加卡片**
   不继续在单页上无限增加信息块，而是构建可扩展工作区导航和领域页面。

5. **实时态与历史态分离**
   即时状态使用 WebSocket/事件流；趋势、历史、诊断使用 REST 快照与历史列表。

6. **以 paper 模式为首要适配对象**
   先完成 paper 模式下的可视化闭环，后续再兼容 live 模式差异。

---

## 4. 目标信息架构

### 4.1 顶层工作区

桌面控制台升级后建议采用以下 6 个核心工作区：

1. **Overview**
2. **Alpha Brain**
3. **Evolution**
4. **Risk Matrix**
5. **Data Fusion**
6. **Execution & Audit**

### 4.2 工作区职责定义

#### A. Overview

用于全局态势总览，优先级最高。

展示内容：

1. 当前运行状态：running/stopped
2. 交易模式：paper/live
3. 当前交易所
4. 账户净值、当日盈亏、峰值净值、当前回撤
5. 当前 dominant regime、confidence、regime 稳定性
6. 风险等级摘要
7. 当前策略总权重分布摘要
8. 实时持仓与名义敞口摘要
9. 最新告警和异常摘要

#### B. Alpha Brain

用于展示 AI 核心决策链路。

展示内容：

1. Regime 概率分布：bull/bear/sideways/high_vol
2. dominant regime 与 confidence
3. 最近 regime 切换历史
4. Strategy Orchestrator 最终权重
5. 门控动作：allow/reduce/block
6. 被阻断策略及原因
7. Continuous Learner 当前模型版本、阈值、最近重训时间
8. 模型版本历史与活跃版本
9. AI 文本分析解释区

#### C. Evolution

用于展示自进化引擎生命周期。

展示内容：

1. candidate / shadow / paper / active / retired 数量
2. 最近晋升、降级、淘汰记录
3. A/B 测试实验状态与 lift
4. 周度参数优化运行记录
5. 当前活跃策略或模型版本
6. 回滚历史与手动回滚入口
7. 演进报告列表

#### D. Risk Matrix

用于展示升级后的智能风控矩阵。

展示内容：

1. Circuit Breaker 状态、原因、冷却剩余时间
2. Daily PnL、Consecutive Losses、Max Drawdown
3. Budget Checker 当前预算占用与剩余额度
4. Kill Switch 状态、触发原因
5. Cooldown 状态
6. DCA 规划层数与触发条件
7. Exit Planner 当前止损 / 追踪止盈 / ROI 阶梯参数
8. 波动率自适应仓位摘要

#### E. Data Fusion

用于展示全维度数据融合层状态。

展示内容：

1. 价格源 freshness
2. OrderBook / Trade 订阅状态
3. SubscriptionManager 健康状态、重连次数、最近心跳
4. On-chain 数据健康度
5. Sentiment 数据健康度
6. 数据源 freshness 评分与 stale/partial/fresh 状态
7. 最近数据缺失与回补情况

#### F. Execution & Audit

用于展示执行链路与可审计行为。

展示内容：

1. 当前 open orders / recent fills
2. Paper 模式成交、滑点、手续费
3. 持仓明细与未实现盈亏
4. 审计事件时间线
5. 风控拦截事件
6. 控制动作（stop/reset/trigger circuit test）

---

## 5. 目标视觉与交互方案

### 5.1 布局方案

建议由当前单页改为以下布局：

1. **左侧导航栏**：工作区切换
2. **顶部健康条**：运行状态、mode、exchange、regime、risk、feed health
3. **中央主画布**：当前工作区主视图
4. **右侧上下文抽屉**：AI 分析、告警、帮助、最近事件

### 5.2 视觉语义系统

建议采用语义化配色，而不是当前通用蓝色单调方案：

1. Bull：青绿
2. Bear：珊瑚红
3. Sideways：琥珀黄
4. High Vol：橙红
5. Risk Alert：高亮红
6. Evolution：电蓝
7. Data Healthy：亮青
8. Data Stale：灰黄

### 5.3 交互规则

1. 所有核心卡片支持 drill-down
2. 所有状态变化支持查看最近 N 条历史
3. 阻断原因必须可展开查看明细
4. 风险告警必须在全局顶部可见
5. 演进事件必须支持按 candidate_id / owner / type 过滤
6. 日志流不再承担唯一解释职责，只做审计回放

---

## 6. 前端技术重构方案

### 6.1 当前问题

当前 `apps/desktop-client/src/App.tsx` 是单文件聚合实现，不适合承载控制台级别复杂度。

### 6.2 目标目录结构

建议重构为：

```text
apps/desktop-client/src/
  app/
    AppShell.tsx
    routes.tsx
  pages/
    OverviewPage.tsx
    AlphaBrainPage.tsx
    EvolutionPage.tsx
    RiskMatrixPage.tsx
    DataFusionPage.tsx
    ExecutionAuditPage.tsx
  features/
    overview/
    alpha-brain/
    evolution/
    risk/
    data-fusion/
    execution/
    audit/
  components/
    layout/
    charts/
    cards/
    badges/
    tables/
    timeline/
  hooks/
    useDashboardSocket.ts
    useDashboardSnapshot.ts
    useRiskStream.ts
    useEvolutionStream.ts
    useDataHealthStream.ts
  services/
    api.ts
    ws.ts
  types/
    dashboard.ts
    regime.ts
    evolution.ts
    risk.ts
    data-health.ts
```

### 6.3 状态管理建议

建议采用轻量结构化状态，不继续把所有状态堆在 `App.tsx`：

1. 实时状态：页面级 hooks + context
2. 快照查询：统一 API service
3. 控制动作：集中 action service
4. 领域类型：集中定义 TS types

如果后续页面进一步复杂，可引入 Zustand；当前阶段先保持轻量即可。

---

## 7. 后端接口与数据契约升级计划

### 7.1 当前接口问题

当前 `GET /api/v1/status` 与 `WS /api/v1/ws/status` 只适合旧版监控板，不适合新控制台。

问题如下：

1. 字段过于扁平
2. 缺少领域分组
3. 缺少 regime、orchestrator、evolution、data health 等关键数据
4. 风险数据不完整
5. 缺少历史事件与快照接口

### 7.2 目标接口设计

建议新增以下接口，不破坏旧接口兼容：

#### A. Dashboard 聚合快照

1. `GET /api/v2/dashboard/overview`
2. `GET /api/v2/dashboard/alpha-brain`
3. `GET /api/v2/dashboard/evolution`
4. `GET /api/v2/dashboard/risk-matrix`
5. `GET /api/v2/dashboard/data-fusion`
6. `GET /api/v2/dashboard/execution`
7. `GET /api/v2/dashboard/snapshot`（一次性聚合全量）

#### B. 实时事件流

1. `WS /api/v2/ws/dashboard`
2. `WS /api/v2/ws/risk`
3. `WS /api/v2/ws/evolution`
4. `WS /api/v2/ws/data-health`
5. `WS /api/v2/ws/execution`

#### C. 历史与明细接口

1. `GET /api/v2/evolution/reports`
2. `GET /api/v2/evolution/decisions`
3. `GET /api/v2/evolution/retirements`
4. `GET /api/v2/risk/events`
5. `GET /api/v2/execution/fills`
6. `GET /api/v2/execution/orders`
7. `GET /api/v2/data/freshness`

### 7.3 前端所需关键快照字段

#### Overview Snapshot

建议字段：

1. `status`
2. `mode`
3. `exchange`
4. `equity`
5. `daily_pnl`
6. `peak_equity`
7. `drawdown_pct`
8. `positions_summary`
9. `dominant_regime`
10. `regime_confidence`
11. `risk_level`
12. `feed_health`

#### Alpha Brain Snapshot

建议字段：

1. `regime_probs`
2. `dominant_regime`
3. `confidence`
4. `is_regime_stable`
5. `orchestrator.gating_action`
6. `orchestrator.weights`
7. `orchestrator.block_reasons`
8. `continuous_learner.active_version`
9. `continuous_learner.thresholds`
10. `continuous_learner.last_retrain_at`
11. `ai_analysis`

#### Evolution Snapshot

建议字段：

1. `candidate_counts_by_status`
2. `active_candidates`
3. `latest_promotions`
4. `latest_retirements`
5. `latest_rollbacks`
6. `ab_experiments`
7. `weekly_params_optimizer`
8. `last_report_meta`

#### Risk Matrix Snapshot

建议字段：

1. `circuit_broken`
2. `circuit_reason`
3. `circuit_cooldown_remaining_sec`
4. `daily_pnl`
5. `consecutive_losses`
6. `budget_remaining_pct`
7. `kill_switch`
8. `cooldown`
9. `dca_plan`
10. `exit_plan`
11. `position_sizing_mode`

#### Data Fusion Snapshot

建议字段：

1. `price_feed_health`
2. `subscription_manager`
3. `orderbook_health`
4. `trade_feed_health`
5. `onchain_health`
6. `sentiment_health`
7. `freshness_summary`
8. `stale_fields`

---

## 8. 分阶段实施计划

### 阶段 0：方案固化与契约冻结

目标：

1. 固化页面信息架构
2. 确认 v2 dashboard API 契约
3. 冻结核心字段命名

输出：

1. 本计划文档
2. dashboard snapshot 字段清单
3. 前端页面路由与组件树草案

完成标准：

1. 页面结构不再反复改动
2. 后端与前端对新增接口命名达成一致

### 阶段 1：后端聚合快照层

目标：

1. 在 `apps/api/server.py` 基础上增加 v2 dashboard 聚合接口
2. 把已有模块状态整理为前端可直接使用的结构化对象

重点工作：

1. 增加 snapshot builder 层
2. 为 regime / orchestrator / evolution / risk / data health 提供聚合函数
3. 设计 v2 WebSocket 推送载荷
4. 保留 v1 旧接口兼容

输出：

1. `/api/v2/dashboard/*`
2. `/api/v2/ws/*`
3. TS 对应类型草案

完成标准：

1. 前端无需自行拼装底层模块字段
2. 所有页面都有对应的 snapshot API

### 阶段 2：桌面端壳层与导航重构

目标：

1. 从单页改为控制台壳层
2. 建立左侧导航 + 顶部健康条 + 主内容区 + 右侧上下文面板

重点工作：

1. 拆分 `App.tsx`
2. 建立 `AppShell`
3. 配置页面路由与导航高亮
4. 建立全局状态上下文

输出：

1. 新桌面控制台布局骨架
2. 可切换工作区导航

完成标准：

1. 不再使用单文件承载全部 UI
2. 新页面可独立开发与测试

### 阶段 3：Overview 工作区上线

目标：

1. 优先完成总控态势面板
2. 替换旧版首页核心功能

重点工作：

1. 全局净值卡
2. 风险摘要卡
3. regime 概览卡
4. 实时持仓与暴露摘要
5. 最新告警与异常列表
6. K 线主图保留，但升级为可叠加状态标记

输出：

1. OverviewPage

完成标准：

1. 用户进入应用后可在一个页面快速了解系统全局状态

### 阶段 4：Alpha Brain 工作区上线

目标：

1. 把技术升级中最核心的智能决策链条前置可视化

重点工作：

1. regime 概率分布图
2. dominant regime 与稳定性视图
3. orchestrator 权重矩阵
4. 门控决策卡
5. block reasons 列表
6. Continuous Learner 版本、阈值、重训信息
7. AI 分析抽屉

输出：

1. AlphaBrainPage

完成标准：

1. 用户可以看到系统为何偏向某类策略
2. 用户可以理解信号为何被阻断或降权

### 阶段 5：Evolution 工作区上线

目标：

1. 让自进化引擎从后台机制升级为可观测、可追踪的前台能力

重点工作：

1. candidate 生命周期面板
2. promotion / demotion / retirement 时间线
3. A/B 实验面板
4. weekly params optimizer 状态卡
5. 回滚入口和回滚历史
6. 报告列表

输出：

1. EvolutionPage

完成标准：

1. 用户能够查看每个候选是如何演进的
2. 用户能够确认演进行为具备生产可解释性

### 阶段 6：Risk Matrix 工作区上线

目标：

1. 把升级后的多层风控从日志中抽离，形成专用控制面板

重点工作：

1. circuit breaker 状态卡
2. cooldown 计时器
3. budget checker 预算使用图
4. kill switch 状态卡
5. DCA 规划视图
6. exit planner 追踪止盈 / ROI ladder 可视化
7. 风控事件时间线

输出：

1. RiskMatrixPage

完成标准：

1. 用户能明确看到风控系统当前是否允许开仓、为何阻断、何时恢复

### 阶段 7：Data Fusion 工作区上线

目标：

1. 将行情、订单簿、链上、情绪、订阅健康状态统一纳入可视化

重点工作：

1. SubscriptionManager 健康卡
2. feed heartbeat 与 reconnect 统计
3. freshness summary 看板
4. on-chain / sentiment source health 卡片
5. stale / partial / fresh 字段表

输出：

1. DataFusionPage

完成标准：

1. 用户可以快速判断当前数据是否可靠、是否降级、是否存在盲区

### 阶段 8：Execution & Audit 工作区上线

目标：

1. 完成从 signal 到 paper fill 的可视化闭环

重点工作：

1. recent orders / fills
2. paper 成交滑点与手续费摘要
3. 持仓明细与未实现盈亏
4. 审计日志流结构化筛选
5. 控制动作记录

输出：

1. ExecutionAuditPage

完成标准：

1. 用户可完整回放交易与控制动作

### 阶段 9：联调、回归与打包发布

目标：

1. 完成前后端联调
2. 保证桌面端 build、打包、回归测试通过

重点工作：

1. 前端组件测试
2. API 合约测试
3. Electron 打包验证
4. 关键页面截图验收
5. Paper 模式集成回归

输出：

1. 新版桌面程序
2. 构建产物
3. UI 验收报告

完成标准：

1. 桌面端可执行程序和安装包正常运行
2. 所有关键页面可访问、数据完整、交互可用

---

## 9. 页面级实施清单

### 9.1 第一优先级页面

1. Overview
2. Alpha Brain
3. Risk Matrix

原因：

1. 能最直接体现本次升级价值
2. 能最快替代旧版首页
3. 能先解决“系统已升级但 UI 仍像旧版”的核心问题

### 9.2 第二优先级页面

1. Evolution
2. Data Fusion

原因：

1. 强化系统的可解释性与可观测性
2. 把后台机制前置为产品能力

### 9.3 第三优先级页面

1. Execution & Audit
2. 历史诊断与筛选工具

原因：

1. 更偏运维与审计场景
2. 可在核心控制链路完成后增强

---

## 10. 测试与验证计划

### 10.1 前端验证范围

1. 布局渲染正确
2. 路由切换正确
3. 各工作区数据加载正确
4. WebSocket 断线重连正确
5. 空状态、错误状态、降级状态正确展示
6. Electron 打包后页面正常运行

### 10.2 接口验证范围

1. v2 snapshot 字段完整
2. WebSocket 推送字段与 REST 快照一致
3. 风控、演进、数据健康字段序列化正确
4. 前端字段命名与后端一致

### 10.3 验收重点

1. 是否真正体现 Alpha Brain 决策链
2. 是否能前台观察 Self-Evolution 生命周期
3. 是否能在 UI 上快速理解 Risk Matrix 阻断原因
4. 是否能判断 Data Fusion 的健康与 stale 状态
5. 是否仍保持桌面端运行稳定与打包可用

---

## 11. 风险与对策

### 风险 1：后端能力已实现，但缺少统一快照出口

对策：

1. 增加 dashboard snapshot builder 层
2. 避免前端直接耦合多个底层模块

### 风险 2：继续在 `App.tsx` 上叠加会导致不可维护

对策：

1. 阶段 2 必须先拆壳层
2. 未完成壳层重构前，不新增大块业务 UI

### 风险 3：WebSocket 粒度不足，实时数据仍靠旧 status 通道

对策：

1. 引入 v2 领域事件流
2. 保留 v1 通道仅用于兼容

### 风险 4：页面过多、信息过载

对策：

1. Overview 页面只显示摘要
2. 详细信息放到对应工作区和抽屉中

### 风险 5：打包后路径和资源依赖变化

对策：

1. 所有 API host、资源路径统一在 service 层收口
2. Electron 打包前后都执行一轮桌面端验收

---

## 12. 交付物清单

本次前端控制台升级计划预计交付以下内容：

1. `docs/DESKTOP_CONTROL_CENTER_UI_UPGRADE_IMPLEMENTATION_PLAN.md`
2. 新版桌面端多工作区控制台 UI
3. v2 dashboard snapshot / ws 接口
4. 前端领域组件与类型系统
5. 打包后的 Electron 可执行程序与安装包
6. UI 验收与联调结果文档

---

## 13. 完成标准

满足以下条件视为本计划完成：

1. 桌面端不再是旧版单页监控板
2. 至少完成 Overview、Alpha Brain、Risk Matrix 三个核心工作区
3. Evolution 与 Data Fusion 页面可展示核心升级能力
4. 前端可直接消费结构化 snapshot，而非依赖拼接日志
5. Electron 构建、可执行程序与安装包均可运行
6. Paper 模式下 UI 可以完整反映升级后的关键能力状态

---

## 14. 推荐实施顺序

建议按以下顺序实施：

1. **先做接口契约**
2. **再拆桌面端壳层**
3. **优先上线 Overview / Alpha Brain / Risk Matrix**
4. **再补 Evolution / Data Fusion**
5. **最后收敛 Execution & Audit 与打包验收**

这是风险最低、价值最直观、最符合当前代码库现状的实施路径。

---

## 15. 结论

本次 AI_QUANT_TRADER_EVOLUTION_STRATEGY 技术升级已经让系统从传统交易器升级为具备 **市场环境感知、策略编排、自进化、智能风控、全维数据融合** 能力的交易系统。

因此，前端 UI 不能继续停留在“净值 + K 线 + 日志”的旧式监控板阶段，而必须同步升级为围绕 **Alpha Brain、Evolution、Risk Matrix、Data Fusion** 的桌面交易控制台。

本实施计划的核心价值在于：

1. 把升级后的能力从“后台逻辑”变成“用户可理解、可验证、可操作的前台产品能力”
2. 为后续继续扩展 live 模式、策略上新、演进回滚、风险解释提供稳定 UI 基座
3. 让桌面端真正成为 AI Quant Trader 的控制中心，而不是旧版运行监控页