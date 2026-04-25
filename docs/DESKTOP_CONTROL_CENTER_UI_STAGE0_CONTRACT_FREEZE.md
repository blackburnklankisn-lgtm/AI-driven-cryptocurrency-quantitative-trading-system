# Desktop Control Center UI — 阶段 0 契约冻结

更新时间：2026-04-25

## 目标

冻结桌面控制台前后端契约，避免阶段 1~9 过程中接口字段频繁变化导致返工。

## 冻结结论

### 顶层页面

1. Overview
2. Alpha Brain
3. Evolution
4. Risk Matrix
5. Data Fusion
6. Execution & Audit

### v2 REST 契约

1. `/api/v2/dashboard/overview`
2. `/api/v2/dashboard/alpha-brain`
3. `/api/v2/dashboard/evolution`
4. `/api/v2/dashboard/risk-matrix`
5. `/api/v2/dashboard/data-fusion`
6. `/api/v2/dashboard/execution`
7. `/api/v2/dashboard/snapshot`
8. `/api/v2/evolution/reports`
9. `/api/v2/evolution/decisions`
10. `/api/v2/evolution/retirements`
11. `/api/v2/risk/events`
12. `/api/v2/execution/fills`
13. `/api/v2/execution/orders`
14. `/api/v2/data/freshness`

### v2 WS 契约

1. `/api/v2/ws/dashboard`
2. `/api/v2/ws/risk`
3. `/api/v2/ws/evolution`
4. `/api/v2/ws/data-health`
5. `/api/v2/ws/execution`

## 命名规则

1. 顶层对象统一使用 snake_case，保持与后端一致
2. 页面内部 TS 类型使用明确 domain 命名
3. `generated_at` 为所有快照标准字段
4. 旧版 v1 接口继续保留兼容

## 冻结字段范围

### Overview

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
11. `is_regime_stable`
12. `risk_level`
13. `feed_health`
14. `strategy_weight_summary`
15. `alerts`

### Alpha Brain

1. `dominant_regime`
2. `confidence`
3. `regime_probs`
4. `is_regime_stable`
5. `orchestrator`
6. `continuous_learner`
7. `ai_analysis`

### Evolution

1. `candidate_counts_by_status`
2. `active_candidates`
3. `candidates`
4. `latest_promotions`
5. `latest_retirements`
6. `latest_rollbacks`
7. `ab_experiments`
8. `weekly_params_optimizer`
9. `last_report_meta`

### Risk Matrix

1. `circuit_broken`
2. `circuit_reason`
3. `circuit_cooldown_remaining_sec`
4. `daily_pnl`
5. `consecutive_losses`
6. `peak_equity`
7. `budget_remaining_pct`
8. `kill_switch`
9. `cooldown`
10. `dca_plan`
11. `exit_plan`
12. `position_sizing_mode`
13. `risk_state`

### Data Fusion

1. `price_feed_health`
2. `subscription_manager`
3. `orderbook_health`
4. `trade_feed_health`
5. `onchain_health`
6. `sentiment_health`
7. `freshness_summary`
8. `stale_fields`
9. `latest_prices`

### Execution

1. `open_orders`
2. `recent_fills`
3. `paper_summary`
4. `positions`
5. `control_actions`

## 阶段 0 完成标准

1. 页面与领域划分冻结
2. v2 REST/WS 契约冻结
3. 字段命名冻结
4. 阶段 1 后端实现以此为准