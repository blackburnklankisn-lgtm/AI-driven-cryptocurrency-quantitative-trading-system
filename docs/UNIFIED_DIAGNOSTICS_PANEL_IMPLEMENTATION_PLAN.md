# Unified Diagnostics Panel Implementation Plan

## Goal

Build a dedicated diagnostics workspace for AI Quant Trader that unifies backend runtime health, six existing business workspaces, transport status, realtime freshness, degradation reasons, and execution/runtime diagnostics into one operator-facing panel.

This panel is not limited to the current HTX/WebSocket issue. It is intended to become the standard troubleshooting surface for packaged deployments, live runtime debugging, and user issue triage.

## Review Summary

The current system already contains substantial diagnostic capability, but it is fragmented.

- The frontend exposes six business workspaces: overview, alpha-brain, evolution, risk-matrix, data-fusion, execution-audit.
- The backend exposes six business snapshots in `apps/api/server.py`, but no unified diagnostics model.
- Many runtime modules already implement `diagnostics()` or `health_snapshot()` methods, but most are not exposed to the frontend.
- Frontend transport state is only partially visible. Each page knows whether its own WebSocket is connected, but there is no unified transport dashboard covering all WS/HTTP channels.

## Current Diagnostic Sources

### Already Exposed

- Overview snapshot: trader status, feed health, alerts, latest order rejection.
- Alpha brain snapshot: regime state, orchestrator output, continuous learner versions, AI analysis.
- Evolution snapshot: candidates, promotions, retirements, rollbacks, A/B experiments, weekly optimizer state.
- Risk matrix snapshot: circuit breaker, cooldown, kill switch payload, DCA/exit plan config.
- Data fusion snapshot: subscription manager, orderbook/trade/onchain/sentiment health, freshness summary, price ages.
- Execution snapshot: orders, fills, positions, control actions.

### Existing Internal Diagnostics Not Fully Exposed

- Realtime feed internals: websocket client diagnostics, per-symbol depth/trade cache internals.
- Risk internals: budget checker snapshot, richer adaptive risk state, state store diagnostics.
- Alpha/ML internals: strategy registry health, regime detector health, DataKitchen diagnostics, feature pipeline diagnostics, meta learner diagnostics.
- Evolution internals: engine diagnostics, registry/state store/scheduler diagnostics.
- Phase 3 internals: market-making diagnostics, RL diagnostics, realtime runtime wiring diagnostics.

### Frontend Signals Available But Not Unified

- WebSocket connected/disconnected state per hook.
- REST request failure state in polling hooks.
- Endpoint rotation logic in backend endpoint service.
- Audit log stream connectivity.

## Target Outcome

Create a new dedicated frontend workspace, `diagnostics`, backed by a new backend diagnostics snapshot and dedicated diagnostics WebSocket channel.

The diagnostics panel will unify:

1. System status
2. Transport status
3. Six workspace health summaries
4. Data source and freshness diagnostics
5. Alpha/ML diagnostics
6. Risk diagnostics
7. Execution diagnostics
8. Evolution diagnostics
9. Phase 3 diagnostics
10. Recent errors and degradation reasons

## Unified Diagnostics Data Model

Introduce a new `DiagnosticsSnapshot` model with these top-level sections:

- `generated_at`
- `status`
- `system`
- `transport`
- `workspace_health`
- `alpha_brain_diag`
- `risk_diag`
- `data_sources`
- `execution_diag`
- `evolution_diag`
- `phase3_diag`
- `alerts`
- `recent_errors`

This model should remain independent from the existing `DashboardSnapshot` to avoid turning business snapshots into an overloaded troubleshooting payload.

## Backend Implementation Plan

### Phase 1: Unified Diagnostics Snapshot

- Add `_build_diagnostics_snapshot()` in `apps/api/server.py`.
- Reuse the existing six snapshot builders as baseline sections.
- Add a dedicated REST endpoint:
  - `GET /api/v2/diagnostics`
- Add a dedicated WebSocket endpoint:
  - `WS /api/v2/ws/diagnostics`
- Extend the 3-second broadcast worker to push diagnostics snapshots.

### Phase 2: Backend Telemetry Layer

Add backend-side telemetry for API push channels and worker loops:

- Per-channel active connection count
- Last successful broadcast time
- Broadcast count
- Last broadcast error
- Worker last tick time
- Worker last error
- Worker success count

These should be surfaced in the `transport` section.

### Phase 3: Expose High-Value Internal Diagnostics

Pull in existing runtime diagnostics from modules where available:

- `SubscriptionManager.diagnostics()`
- `ws_client.diagnostics()`
- `DepthCacheRegistry.diagnostics()`
- `TradeCacheRegistry.diagnostics()`
- `OnChainCollector.diagnostics()`
- `SentimentCollector.diagnostics()`
- `KillSwitch.health_snapshot()`
- `AdaptiveRiskMatrix.health_snapshot()`
- `CooldownManager.diagnostics()`
- `BudgetChecker.snapshot()`
- `StateStore.diagnostics()`
- `SelfEvolutionEngine.diagnostics()`
- `StrategyRegistry.health_snapshot()`
- `StrategyOrchestrator.health_snapshot()`
- `RegimeDetector.health_snapshot()` where available
- `DataKitchen.diagnostics()` where available
- `MMStrategy.diagnostics()`
- `PPOAgent.diagnostics()` where available

### Phase 4: Normalize Diagnostic Metadata

Every major diagnostics section should expose consistent fields where possible:

- `status`
- `detail`
- `last_success_at`
- `last_error`
- `age_sec`
- `degrade_reason`
- `expected_refresh_sec`

This is necessary to make the panel actionable for operators.

## Frontend Implementation Plan

### Phase 1: Add Diagnostics Workspace

- Extend workspace navigation with a new `diagnostics` entry.
- Add `DiagnosticsSnapshot` types in `apps/desktop-client/src/types/dashboard.ts`.
- Add diagnostics API accessors in `apps/desktop-client/src/services/api.ts`.
- Add a dedicated hook for diagnostics data.
- Add `DiagnosticsPage.tsx`.

### Phase 2: Frontend Transport Diagnostics

Instrument shared frontend transport layers:

- `services/ws.ts`
- `services/backendEndpoint.ts`
- polling hooks

Track:

- channel connected/disconnected state
- last message time
- reconnect count
- last error
- REST success/failure timestamps
- selected endpoint base

These will be shown in the diagnostics page as frontend-side transport health.

### Phase 3: Diagnostics UI Structure

Use a layered UI instead of raw JSON only:

- top summary metric cards
- transport health matrix
- workspace health matrix
- recent alerts / recent errors
- expandable diagnostic sections with structured JSON for deep inspection

The UI should optimize for fast issue isolation first, deep detail second.

## Proposed Diagnostics Sections

1. System Overview
2. Frontend/Backend Transport
3. Workspace Health Matrix
4. Data Source Freshness
5. Alpha Brain / ML Diagnostics
6. Risk Control Diagnostics
7. Execution Chain Diagnostics
8. Evolution Diagnostics
9. Phase 3 Realtime Diagnostics
10. Recent Errors and Degradation Reasons

## Validation Plan

After implementation, validate at least these scenarios:

1. Normal steady-state runtime: all major sections show healthy/partial/stale correctly.
2. Backend unavailable: transport diagnostics clearly show local API failure.
3. HTX realtime degraded but REST alive: diagnostics distinguish realtime WS degradation from REST K-line availability.
4. External source stale: diagnostics surface exact stale source and degrade reason.
5. Packaged installer environment: diagnostics remain usable in installed mode for field troubleshooting.

## Delivery Order

1. Save implementation plan document.
2. Implement backend diagnostics snapshot and diagnostics WS.
3. Implement frontend diagnostics workspace and baseline display.
4. Add frontend transport telemetry.
5. Expand internal module diagnostics exposure.
6. Improve layout and operator usability.

## Immediate Implementation Scope

Begin with Phase 1:

- backend unified diagnostics snapshot
- backend diagnostics websocket
- frontend diagnostics workspace skeleton
- baseline display of system, transport, workspace summaries, and nested diagnostics payloads

This gets a usable diagnostics panel into the product quickly, while keeping room for richer telemetry in subsequent slices.