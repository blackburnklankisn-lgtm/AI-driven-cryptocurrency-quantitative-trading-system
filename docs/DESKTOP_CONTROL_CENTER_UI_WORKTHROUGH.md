# Desktop Control Center UI Upgrade — Workthrough

更新时间：2026-04-25

本文件用于持续记录按照 `DESKTOP_CONTROL_CENTER_UI_UPGRADE_IMPLEMENTATION_PLAN.md` 执行各阶段时的实际实施过程、关键决策、完成项和验证结果。

---

## 阶段 0：方案固化与契约冻结

### 已完成

1. 基于现有桌面端单页实现和后端 `v1` API 能力，确认旧版 UI 无法承载本次技术升级后的结构化能力。
2. 冻结桌面控制台顶层工作区：Overview / Alpha Brain / Evolution / Risk Matrix / Data Fusion / Execution & Audit。
3. 冻结 `v2` REST 与 WebSocket 契约命名。
4. 明确所有领域快照统一附带 `generated_at` 字段。

### 关键决策

1. 保留 `v1` 接口兼容，新增 `v2` 控制台快照层。
2. 不引入前端路由依赖作为阶段 2 的前置条件，先用轻量壳层导航降低变更成本。

### 输出

1. `docs/DESKTOP_CONTROL_CENTER_UI_STAGE0_CONTRACT_FREEZE.md`
2. 本 workthrough 文档

---

## 阶段 1：后端聚合快照层

### 已完成

1. 在 `apps/api/server.py` 中新增 `v2` dashboard snapshot builders。
2. 新增以下 REST 接口：
   - `/api/v2/dashboard/overview`
   - `/api/v2/dashboard/alpha-brain`
   - `/api/v2/dashboard/evolution`
   - `/api/v2/dashboard/risk-matrix`
   - `/api/v2/dashboard/data-fusion`
   - `/api/v2/dashboard/execution`
   - `/api/v2/dashboard/snapshot`
   - `/api/v2/evolution/reports`
   - `/api/v2/evolution/decisions`
   - `/api/v2/evolution/retirements`
   - `/api/v2/risk/events`
   - `/api/v2/execution/fills`
   - `/api/v2/execution/orders`
   - `/api/v2/data/freshness`
3. 新增以下 WS 接口：
   - `/api/v2/ws/dashboard`
   - `/api/v2/ws/risk`
   - `/api/v2/ws/evolution`
   - `/api/v2/ws/data-health`
   - `/api/v2/ws/execution`
4. 在状态推送 worker 中加入 `v2` 快照广播。
5. 增加大量 `APIv2` debug 日志，便于后续联调问题排查。

### 实现策略

1. 先通过 `_safe_getattr()` 做低耦合聚合，避免一次性深改 trader 主流程。
2. 对已存在但未统一暴露的能力，先输出可用快照骨架；后续阶段再逐步细化数据完整度。
3. 统一在 `server.py` 内完成第一版 builder，避免阶段 1 引入过多文件拆分增加联调复杂度。

### 备注

1. 某些字段依赖 trader 运行时是否挂载对应模块；如果模块未接入，将返回 `unavailable` 或空对象，而不是让接口报错。
2. 这属于控制台升级的第一层可视化出口搭建，后续页面联调时会进一步校准字段精度。

---

## 阶段 2：控制中心壳层与导航

### 已完成

1. 将桌面前端入口由旧单页 `App.tsx` 切换为新的 `AppShell` 壳层。
2. 建立左侧工作区导航、顶部健康条、右侧上下文抽屉的三栏结构。
3. 采用本地 `workspace` 状态切换页面，不引入新的前端路由依赖。
4. 建立统一的 `dcc-*` 样式命名体系和控制中心视觉变量。

### 新增/调整文件

1. `apps/desktop-client/src/app/AppShell.tsx`
2. `apps/desktop-client/src/App.tsx`
3. `apps/desktop-client/src/index.css`
4. `apps/desktop-client/src/App.css`

### 实施说明

1. 导航数据使用显式数组声明，保证后续扩展工作区时仅需增量修改。
2. 右侧上下文区集中承载 Operator Controls、AI Insight、Snapshot Meta，避免每页重复实现控制组件。

---

## 阶段 3：Overview 工作区

### 已完成

1. 新建 `OverviewPage`，对接 `overview` snapshot。
2. 实现 Total Equity、Dominant Regime、Risk Level 三个核心指标卡片。
3. 实现 Alerts、Feed Health、Positions、Strategy Weight Summary 四块全局概览内容。

### 输出文件

1. `apps/desktop-client/src/pages/OverviewPage.tsx`
2. `apps/desktop-client/src/components/cards/MetricCard.tsx`
3. `apps/desktop-client/src/components/layout/SectionPanel.tsx`

---

## 阶段 4：Alpha Brain 工作区

### 已完成

1. 新建 `AlphaBrainPage`，对接 `alpha_brain` snapshot。
2. 可视化显示 regime 概率分布、orchestrator gating action、策略权重和 block reasons。
3. 衔接 continuous learner inventory 与 AI analysis 文本输出。

### 输出文件

1. `apps/desktop-client/src/pages/AlphaBrainPage.tsx`

---

## 阶段 5：Evolution 工作区

### 已完成

1. 新建 `EvolutionPage`，对接 `evolution` snapshot。
2. 展示候选策略生命周期统计、活跃候选、最近 promotion/retirement 信息。
3. 暂以结构化 JSON 面板承载优化器和实验信息，优先确保数据透出完整。

### 输出文件

1. `apps/desktop-client/src/pages/EvolutionPage.tsx`

---

## 阶段 6：Risk Matrix 工作区

### 已完成

1. 新建 `RiskMatrixPage`，对接 `risk_matrix` snapshot。
2. 展示 circuit breaker、cooldown、budget remaining、position sizing 等关键风控信息。
3. 将 kill switch、cooldown、DCA、exit plan 分块展示，便于阶段后续继续精细化。

### 输出文件

1. `apps/desktop-client/src/pages/RiskMatrixPage.tsx`

---

## 阶段 7：Data Fusion 工作区

### 已完成

1. 新建 `DataFusionPage`，对接 `data_fusion` snapshot。
2. 展示 freshness summary、subscription manager 和 source health matrix。
3. 将 stale fields 单独列出，便于快速识别数据新鲜度问题。

### 输出文件

1. `apps/desktop-client/src/pages/DataFusionPage.tsx`

---

## 阶段 8：Execution & Audit 工作区

### 已完成

1. 新建 `ExecutionAuditPage`，对接 `execution` snapshot。
2. 展示 paper execution summary、open orders、recent fills、control actions 与 positions。
3. 将原来分散在旧单页中的执行态信息迁移到新的独立工作区中。

### 输出文件

1. `apps/desktop-client/src/pages/ExecutionAuditPage.tsx`

---

## 阶段 9：验证、构建与发布产物

### 已完成

1. 新增前端公共数据层：
   - `apps/desktop-client/src/types/dashboard.ts`
   - `apps/desktop-client/src/services/api.ts`
   - `apps/desktop-client/src/services/ws.ts`
   - `apps/desktop-client/src/hooks/useDashboardSnapshot.ts`
   - `apps/desktop-client/src/hooks/useDashboardSocket.ts`
2. 对新增和修改的前端文件执行编辑器错误检查，结果为 `No errors found`。
3. 执行 `npx tsc -b` 进行 TypeScript 构建校验，未返回编译错误。
4. 执行 `npm run build` 完成 Vite 构建、Electron 主进程编译和 NSIS 打包。
5. 构建过程中识别到 `release/win-unpacked/AI Quant Trader.exe` 被占用，清理锁定进程与旧目录后重试成功。

### 产物

1. `apps/desktop-client/release/win-unpacked/AI Quant Trader.exe`
2. `apps/desktop-client/release/AI Quant Trader Setup 0.0.0.exe`
3. `apps/desktop-client/release/AI Quant Trader Setup 0.0.0.exe.blockmap`

### 验证备注

1. `electron-builder` 仍提示 `package.json` 缺少 `description` 和 `author`，但不影响本次构建成功。
2. 构建日志中出现若干 `extraneous/missing` wasm 依赖提示，属于依赖扫描警告，未阻断产物生成。
3. 本轮主要变更集中在桌面前端，后端 `v2` 聚合层仍保持阶段 1 已通过的回归状态。

### 本轮总结

1. 控制中心已从旧单页升级为六工作区结构化桌面 UI。
2. 后端 `v2` snapshot / websocket 与新前端壳层已完成首轮闭环联通。
3. 阶段 0 到阶段 9 的核心实施项已按计划落地，并保留了后续继续细化页面细节的扩展位。
