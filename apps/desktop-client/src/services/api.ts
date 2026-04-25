import type {
  AlphaBrainSnapshot,
  DashboardSnapshot,
  DataFusionSnapshot,
  EvolutionSnapshot,
  ExecutionSnapshot,
  OverviewSnapshot,
  RiskMatrixSnapshot,
} from '../types/dashboard';

const API_BASE = 'http://localhost:8000';

async function fetchJson<T>(path: string): Promise<T> {
  const url = `${API_BASE}${path}`;
  console.debug('[desktop-client][api] request', url);
  const response = await fetch(url);
  if (!response.ok) {
    const text = await response.text();
    console.error('[desktop-client][api] request failed', url, response.status, text);
    throw new Error(`Request failed: ${response.status} ${path}`);
  }
  const data = (await response.json()) as T;
  console.debug('[desktop-client][api] response ok', path, data);
  return data;
}

export const dashboardApi = {
  getSnapshot: () => fetchJson<DashboardSnapshot>('/api/v2/dashboard/snapshot'),
  getOverview: () => fetchJson<OverviewSnapshot>('/api/v2/dashboard/overview'),
  getAlphaBrain: () => fetchJson<AlphaBrainSnapshot>('/api/v2/dashboard/alpha-brain'),
  getEvolution: () => fetchJson<EvolutionSnapshot>('/api/v2/dashboard/evolution'),
  getRiskMatrix: () => fetchJson<RiskMatrixSnapshot>('/api/v2/dashboard/risk-matrix'),
  getDataFusion: () => fetchJson<DataFusionSnapshot>('/api/v2/dashboard/data-fusion'),
  getExecution: () => fetchJson<ExecutionSnapshot>('/api/v2/dashboard/execution'),
};

export async function postControlAction(action: string): Promise<{ result: string; message: string }> {
  const url = `${API_BASE}/api/v1/control`;
  console.info('[desktop-client][api] control action', action);
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action }),
  });
  const data = (await response.json()) as { result: string; message: string };
  console.info('[desktop-client][api] control result', action, data);
  return data;
}