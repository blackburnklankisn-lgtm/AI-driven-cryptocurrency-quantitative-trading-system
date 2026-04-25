export type WorkspaceKey =
  | 'overview'
  | 'alpha-brain'
  | 'evolution'
  | 'risk-matrix'
  | 'data-fusion'
  | 'execution-audit';

export interface FeedHealthSummary {
  health: string;
  exchange: string;
  reconnect_count: number;
}

export interface PositionSummaryItem {
  symbol: string;
  quantity: number;
  last_price: number;
  notional: number;
  entry_price?: number | null;
  unrealized_pnl?: number | null;
}

export interface PositionsSummary {
  count: number;
  total_notional: number;
  items: PositionSummaryItem[];
}

export interface OverviewAlert {
  code: string;
  severity: 'info' | 'warning' | 'critical';
  source: string;
  message: string;
  occurred_at: string;
  details?: Record<string, unknown>;
}

export interface OrderRejectionSummary {
  timestamp: string;
  stage: string;
  reason: string;
  strategy_id: string;
  symbol: string;
  side: string;
  quantity: string;
}

export interface OverviewSnapshot {
  generated_at: string;
  status: string;
  mode?: string;
  exchange?: string;
  equity?: number;
  daily_pnl?: number;
  peak_equity?: number;
  drawdown_pct?: number;
  positions_summary?: PositionsSummary;
  dominant_regime?: string;
  regime_confidence?: number;
  is_regime_stable?: boolean;
  risk_level?: string;
  feed_health?: FeedHealthSummary;
  strategy_weight_summary?: Record<string, number>;
  alerts?: OverviewAlert[];
  latest_order_rejection?: OrderRejectionSummary | null;
  message?: string;
}

export interface AlphaBrainSelectedResult {
  strategy_id: string;
  symbol: string;
  action: string;
  confidence: number;
}

export interface ContinuousLearnerItem {
  id?: string;
  active_version?: string | null;
  thresholds?: Record<string, number>;
  versions?: string[];
  error?: string;
}

export interface AlphaBrainSnapshot {
  generated_at: string;
  dominant_regime: string;
  confidence: number;
  regime_probs: {
    bull: number;
    bear: number;
    sideways: number;
    high_vol: number;
  };
  is_regime_stable: boolean;
  orchestrator: {
    gating_action: string;
    weights: Record<string, number>;
    block_reasons: string[];
    selected_results: AlphaBrainSelectedResult[];
  };
  continuous_learner: {
    count: number;
    active_version: string | null;
    thresholds: Record<string, number>;
    last_retrain_at: string | null;
    items: ContinuousLearnerItem[];
  };
  ai_analysis: string;
}

export interface EvolutionSnapshot {
  generated_at: string;
  candidate_counts_by_status: Record<string, number>;
  active_candidates: Array<Record<string, unknown>>;
  candidates: Array<Record<string, unknown>>;
  latest_promotions: Array<Record<string, unknown>>;
  latest_retirements: Array<Record<string, unknown>>;
  latest_rollbacks: Array<Record<string, unknown>>;
  ab_experiments: Record<string, unknown>;
  weekly_params_optimizer: Record<string, unknown>;
  last_report_meta: unknown;
  status?: string;
  message?: string;
}

export interface RiskMatrixSnapshot {
  generated_at: string;
  circuit_broken: boolean;
  circuit_reason: string;
  circuit_cooldown_remaining_sec: number;
  daily_pnl: number;
  consecutive_losses: number;
  peak_equity: number;
  budget_remaining_pct: number | null;
  kill_switch: Record<string, unknown>;
  cooldown: Record<string, unknown>;
  dca_plan: Record<string, unknown>;
  exit_plan: Record<string, unknown>;
  position_sizing_mode: string;
  risk_state: Record<string, unknown>;
  status?: string;
}

export interface DataFusionSnapshot {
  generated_at: string;
  price_feed_health: string;
  subscription_manager: Record<string, unknown>;
  orderbook_health: Record<string, unknown>;
  trade_feed_health: Record<string, unknown>;
  onchain_health: Record<string, unknown>;
  sentiment_health: Record<string, unknown>;
  freshness_summary: Record<string, unknown>;
  stale_fields: string[];
  latest_prices: Record<string, number>;
  status?: string;
}

export interface ExecutionSnapshot {
  generated_at: string;
  open_orders: Array<Record<string, unknown>>;
  recent_fills: Array<Record<string, unknown>>;
  paper_summary: Record<string, unknown>;
  positions: PositionsSummary;
  control_actions: Array<Record<string, unknown>>;
  status?: string;
}

export interface DashboardSnapshot {
  generated_at: string;
  overview: OverviewSnapshot;
  alpha_brain: AlphaBrainSnapshot;
  evolution: EvolutionSnapshot;
  risk_matrix: RiskMatrixSnapshot;
  data_fusion: DataFusionSnapshot;
  execution: ExecutionSnapshot;
}

export interface RiskEvent {
  event_id?: string;
  timestamp?: string;
  event_type?: string;
  reason?: string;
  details?: Record<string, unknown>;
}

export interface EvolutionReport {
  report_id?: string;
  created_at?: string;
  candidate_id?: string;
  result?: string;
  summary?: Record<string, unknown>;
}