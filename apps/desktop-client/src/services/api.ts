import type {
  AlphaBrainSnapshot,
  DashboardSnapshot,
  DataFusionSnapshot,
  EvolutionReport,
  EvolutionSnapshot,
  ExecutionSnapshot,
  OverviewAlert,
  OverviewSnapshot,
  RiskEvent,
  RiskMatrixSnapshot,
} from '../types/dashboard';
import { fetchWithFallback } from './backendEndpoint';

type JsonRecord = Record<string, unknown>;

interface EvolutionReportsEnvelope {
  reports?: EvolutionReport[];
}

export interface ControlActionPayload {
  action: string;
  family_key?: string;
  candidate_id?: string;
  rollback_to_candidate_id?: string;
}

interface RiskEventsEnvelope {
  generated_at?: string;
  items?: Array<Record<string, unknown>>;
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as JsonRecord) : {};
}

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function asBoolean(value: unknown, fallback = false): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

export function normalizeDashboardSnapshot(raw: DashboardSnapshot): DashboardSnapshot {
  const source = asRecord(raw);
  const overview = asRecord(source.overview);
  const overviewPositions = asRecord(overview.positions_summary);
  const overviewFeedHealth = asRecord(overview.feed_health);
  const overviewLatestRejection = asRecord(overview.latest_order_rejection);
  const normalizedAlerts: OverviewAlert[] = asArray<unknown>(overview.alerts).map((item, index) => {
    if (typeof item === 'string') {
      return {
        code: `legacy_${index}`,
        severity: 'warning' as const,
        source: 'overview',
        message: item,
        occurred_at: asString(overview.generated_at),
        details: {},
      };
    }
    const alert = asRecord(item);
    const severityRaw = asString(alert.severity, 'warning');
    const severity: OverviewAlert['severity'] =
      severityRaw === 'critical' ? 'critical' : severityRaw === 'info' ? 'info' : 'warning';
    return {
      code: asString(alert.code, `alert_${index}`),
      severity,
      source: asString(alert.source, 'overview'),
      message: asString(alert.message),
      occurred_at: asString(alert.occurred_at, asString(overview.generated_at)),
      details: asRecord(alert.details),
    };
  });
  const alphaBrain = asRecord(source.alpha_brain);
  const regimeProbs = asRecord(alphaBrain.regime_probs);
  const orchestrator = asRecord(alphaBrain.orchestrator);
  const continuousLearner = asRecord(alphaBrain.continuous_learner);
  const evolution = asRecord(source.evolution);
  const riskMatrix = asRecord(source.risk_matrix);
  const dataFusion = asRecord(source.data_fusion);
  const execution = asRecord(source.execution);
  const executionPositions = asRecord(execution.positions);

  return {
    generated_at: asString(source.generated_at),
    overview: {
      generated_at: asString(overview.generated_at),
      status: asString(overview.status, 'unknown'),
      mode: asString(overview.mode, 'unknown'),
      exchange: asString(overview.exchange, 'unknown'),
      equity: asNumber(overview.equity),
      daily_pnl: asNumber(overview.daily_pnl),
      peak_equity: asNumber(overview.peak_equity),
      drawdown_pct: asNumber(overview.drawdown_pct),
      positions_summary: {
        count: asNumber(overviewPositions.count),
        total_notional: asNumber(overviewPositions.total_notional),
        items: asArray(overviewPositions.items),
      },
      dominant_regime: asString(overview.dominant_regime, 'unknown'),
      regime_confidence: asNumber(overview.regime_confidence),
      is_regime_stable: asBoolean(overview.is_regime_stable),
      risk_level: asString(overview.risk_level, 'unknown'),
      feed_health: {
        health: asString(overviewFeedHealth.health, 'unknown'),
        exchange: asString(overviewFeedHealth.exchange, 'unknown'),
        reconnect_count: asNumber(overviewFeedHealth.reconnect_count),
      },
      strategy_weight_summary: asRecord(overview.strategy_weight_summary) as Record<string, number>,
      alerts: normalizedAlerts,
      latest_order_rejection: Object.keys(overviewLatestRejection).length
        ? {
            timestamp: asString(overviewLatestRejection.timestamp),
            stage: asString(overviewLatestRejection.stage),
            reason: asString(overviewLatestRejection.reason),
            strategy_id: asString(overviewLatestRejection.strategy_id),
            symbol: asString(overviewLatestRejection.symbol),
            side: asString(overviewLatestRejection.side),
            quantity: asString(overviewLatestRejection.quantity),
          }
        : null,
      message: asString(overview.message),
    },
    alpha_brain: {
      generated_at: asString(alphaBrain.generated_at),
      dominant_regime: asString(alphaBrain.dominant_regime, 'unknown'),
      confidence: asNumber(alphaBrain.confidence),
      regime_probs: {
        bull: asNumber(regimeProbs.bull),
        bear: asNumber(regimeProbs.bear),
        sideways: asNumber(regimeProbs.sideways),
        high_vol: asNumber(regimeProbs.high_vol),
      },
      is_regime_stable: asBoolean(alphaBrain.is_regime_stable),
      orchestrator: {
        decision_chain: asString(orchestrator.decision_chain, 'unknown'),
        gating_action: asString(orchestrator.gating_action, 'unknown'),
        weights: asRecord(orchestrator.weights) as Record<string, number>,
          weight_basis: asString(orchestrator.weight_basis, 'regime_affinity'),
        block_reasons: asArray<string>(orchestrator.block_reasons),
        selected_results: asArray(orchestrator.selected_results),
      },
      continuous_learner: {
        count: asNumber(continuousLearner.count),
        active_version: asString(continuousLearner.active_version) || null,
        model_type: asString(continuousLearner.model_type) || null,
        model_path: asString(continuousLearner.model_path) || null,
        threshold_source: asString(continuousLearner.threshold_source) || null,
        thresholds: asRecord(continuousLearner.thresholds) as Record<string, number>,
        last_retrain_at: asString(continuousLearner.last_retrain_at) || null,
        items: asArray(continuousLearner.items),
      },
      ai_analysis: asString(alphaBrain.ai_analysis, 'N/A'),
    },
    evolution: {
      generated_at: asString(evolution.generated_at),
      candidate_counts_by_status: asRecord(evolution.candidate_counts_by_status) as Record<string, number>,
      active_candidates: asArray(evolution.active_candidates),
      candidates: asArray(evolution.candidates),
      latest_promotions: asArray(evolution.latest_promotions),
      latest_retirements: asArray(evolution.latest_retirements),
      latest_rollbacks: asArray(evolution.latest_rollbacks),
      ab_experiments: {
        summary: asRecord(asRecord(evolution.ab_experiments).summary),
        active: asArray(asRecord(evolution.ab_experiments).active),
        completed: asArray(asRecord(evolution.ab_experiments).completed),
      },
      weekly_params_optimizer: {
        cron: asString(asRecord(evolution.weekly_params_optimizer).cron),
        is_running: asBoolean(asRecord(evolution.weekly_params_optimizer).is_running),
        target_count: asNumber(asRecord(evolution.weekly_params_optimizer).target_count),
        targets: asArray(asRecord(evolution.weekly_params_optimizer).targets),
        runs: asArray(asRecord(evolution.weekly_params_optimizer).runs),
        state: asRecord(asRecord(evolution.weekly_params_optimizer).state),
      },
      last_report_meta: evolution.last_report_meta ?? null,
      status: asString(evolution.status),
      message: asString(evolution.message),
    },
    risk_matrix: {
      generated_at: asString(riskMatrix.generated_at),
      circuit_broken: asBoolean(riskMatrix.circuit_broken),
      circuit_reason: asString(riskMatrix.circuit_reason),
      circuit_cooldown_remaining_sec: asNumber(riskMatrix.circuit_cooldown_remaining_sec),
      daily_pnl: asNumber(riskMatrix.daily_pnl),
      consecutive_losses: asNumber(riskMatrix.consecutive_losses),
      peak_equity: asNumber(riskMatrix.peak_equity),
      budget_remaining_pct: typeof riskMatrix.budget_remaining_pct === 'number' ? riskMatrix.budget_remaining_pct : null,
      kill_switch: asRecord(riskMatrix.kill_switch),
      cooldown: asRecord(riskMatrix.cooldown),
      dca_plan: asRecord(riskMatrix.dca_plan),
      exit_plan: asRecord(riskMatrix.exit_plan),
      position_sizing_mode: asString(riskMatrix.position_sizing_mode, 'unknown'),
      risk_state: asRecord(riskMatrix.risk_state),
      status: asString(riskMatrix.status),
    },
    data_fusion: {
      generated_at: asString(dataFusion.generated_at),
      price_feed_health: asString(dataFusion.price_feed_health, 'unknown'),
      subscription_manager: asRecord(dataFusion.subscription_manager),
      orderbook_health: asRecord(dataFusion.orderbook_health),
      trade_feed_health: asRecord(dataFusion.trade_feed_health),
      onchain_health: asRecord(dataFusion.onchain_health),
      sentiment_health: asRecord(dataFusion.sentiment_health),
      freshness_summary: asRecord(dataFusion.freshness_summary),
      stale_fields: asArray<string>(dataFusion.stale_fields),
      latest_prices: asRecord(dataFusion.latest_prices) as Record<string, { price: number; updated_at: string; age_sec: number }>,
      status: asString(dataFusion.status),
    },
    execution: {
      generated_at: asString(execution.generated_at),
      open_orders: asArray(execution.open_orders),
      recent_fills: asArray(execution.recent_fills),
      paper_summary: asRecord(execution.paper_summary),
      positions: {
        count: asNumber(executionPositions.count),
        total_notional: asNumber(executionPositions.total_notional),
        items: asArray(executionPositions.items),
      },
      control_actions: asArray(execution.control_actions),
      status: asString(execution.status),
    },
  };
}

async function fetchJson<T>(path: string): Promise<T> {
  console.debug('[desktop-client][api] request', path);
  const response = await fetchWithFallback(path);
  if (!response.ok) {
    const text = await response.text();
    console.error('[desktop-client][api] request failed', path, response.status, text);
    throw new Error(`Request failed: ${response.status} ${path}`);
  }
  const data = (await response.json()) as T;
  console.debug('[desktop-client][api] response ok', path, data);
  return data;
}

export const dashboardApi = {
  getSnapshot: async () => normalizeDashboardSnapshot(await fetchJson<DashboardSnapshot>('/api/v2/dashboard/snapshot')),
  getOverview: () => fetchJson<OverviewSnapshot>('/api/v2/dashboard/overview'),
  getAlphaBrain: () => fetchJson<AlphaBrainSnapshot>('/api/v2/dashboard/alpha-brain'),
  getEvolution: () => fetchJson<EvolutionSnapshot>('/api/v2/dashboard/evolution'),
  getRiskMatrix: () => fetchJson<RiskMatrixSnapshot>('/api/v2/dashboard/risk-matrix'),
  getDataFusion: () => fetchJson<DataFusionSnapshot>('/api/v2/dashboard/data-fusion'),
  getExecution: () => fetchJson<ExecutionSnapshot>('/api/v2/dashboard/execution'),
};

export async function postControlAction(
  actionOrPayload: string | ControlActionPayload,
): Promise<{ result: string; message: string; error_code?: string }> {
  const payload: ControlActionPayload =
    typeof actionOrPayload === 'string' ? { action: actionOrPayload } : actionOrPayload;
  console.info('[desktop-client][api] control action', payload);
  const response = await fetchWithFallback('/api/v1/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = (await response.json()) as { result: string; message: string; error_code?: string };
  console.info('[desktop-client][api] control result', payload.action, data);
  return data;
}

export function getRiskEvents(): Promise<RiskEvent[]> {
  return fetchJson<RiskEventsEnvelope>('/api/v2/risk/events').then((payload) => {
    const items = asArray<Record<string, unknown>>(asRecord(payload).items);
    return items.map((item, index) => {
      const event = asRecord(item);
      return {
        event_id: asString(event.event_id, `risk_evt_${index}`),
        timestamp: asString(event.timestamp, asString(event.generated_at)),
        event_type: asString(event.event_type, asString(event.type, 'risk_event')),
        reason: asString(event.reason, asString(event.message)),
        details: asRecord(event.details),
      };
    });
  });
}

export function getEvolutionReports(limit?: number): Promise<EvolutionReport[]> {
  const query = typeof limit === 'number' && Number.isFinite(limit) ? `?limit=${Math.max(1, Math.floor(limit))}` : '';
  return fetchJson<EvolutionReportsEnvelope>(`/api/v2/evolution/reports${query}`).then((data) => asArray<EvolutionReport>(data.reports));
}
