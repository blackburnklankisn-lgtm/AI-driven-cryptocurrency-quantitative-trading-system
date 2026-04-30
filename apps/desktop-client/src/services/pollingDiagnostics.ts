export interface PollingChannelDiagnostics {
  key: string;
  label: string;
  path: string;
  status: string;
  refresh_interval_ms: number | null;
  request_count: number;
  success_count: number;
  failure_count: number;
  last_started_at: string | null;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_completed_at: string | null;
  last_error: string | null;
  last_duration_ms: number | null;
  next_due_at: string | null;
}

interface PollingChannelMeta {
  label: string;
  path: string;
  refreshIntervalMs: number;
}

const pollingDiagnostics = new Map<string, PollingChannelDiagnostics>();
const pollingListeners = new Set<() => void>();

function nowIso(): string {
  return new Date().toISOString();
}

function emitPollingDiagnostics(): void {
  pollingListeners.forEach((listener) => listener());
}

function defaultDiagnostics(key: string, meta: PollingChannelMeta): PollingChannelDiagnostics {
  return {
    key,
    label: meta.label,
    path: meta.path,
    status: 'idle',
    refresh_interval_ms: meta.refreshIntervalMs,
    request_count: 0,
    success_count: 0,
    failure_count: 0,
    last_started_at: null,
    last_success_at: null,
    last_failure_at: null,
    last_completed_at: null,
    last_error: null,
    last_duration_ms: null,
    next_due_at: null,
  };
}

function updatePollingDiagnostics(
  key: string,
  meta: PollingChannelMeta,
  updater: (current: PollingChannelDiagnostics) => PollingChannelDiagnostics,
): void {
  const current = pollingDiagnostics.get(key) ?? defaultDiagnostics(key, meta);
  pollingDiagnostics.set(
    key,
    updater({
      ...current,
      label: meta.label,
      path: meta.path,
      refresh_interval_ms: meta.refreshIntervalMs,
    }),
  );
  emitPollingDiagnostics();
}

export function getPollingDiagnosticsSnapshot(): Record<string, PollingChannelDiagnostics> {
  return Object.fromEntries(
    Array.from(pollingDiagnostics.entries()).map(([key, value]) => [key, { ...value }]),
  );
}

export function subscribePollingDiagnostics(listener: () => void): () => void {
  pollingListeners.add(listener);
  return () => {
    pollingListeners.delete(listener);
  };
}

export function recordPollingStart(key: string, meta: PollingChannelMeta): number {
  const startedAt = performance.now();
  updatePollingDiagnostics(key, meta, (current) => ({
    ...current,
    status: 'running',
    request_count: current.request_count + 1,
    last_started_at: nowIso(),
    next_due_at: new Date(Date.now() + meta.refreshIntervalMs).toISOString(),
  }));
  return startedAt;
}

export function recordPollingSuccess(key: string, meta: PollingChannelMeta, startedAt: number): void {
  const completedAt = nowIso();
  updatePollingDiagnostics(key, meta, (current) => ({
    ...current,
    status: 'healthy',
    success_count: current.success_count + 1,
    last_success_at: completedAt,
    last_completed_at: completedAt,
    last_error: null,
    last_duration_ms: Math.round((performance.now() - startedAt) * 100) / 100,
    next_due_at: new Date(Date.now() + meta.refreshIntervalMs).toISOString(),
  }));
}

export function recordPollingFailure(
  key: string,
  meta: PollingChannelMeta,
  startedAt: number,
  error: unknown,
): void {
  const completedAt = nowIso();
  const message = error instanceof Error ? error.message : String(error);
  updatePollingDiagnostics(key, meta, (current) => ({
    ...current,
    status: 'error',
    failure_count: current.failure_count + 1,
    last_failure_at: completedAt,
    last_completed_at: completedAt,
    last_error: message,
    last_duration_ms: Math.round((performance.now() - startedAt) * 100) / 100,
    next_due_at: new Date(Date.now() + meta.refreshIntervalMs).toISOString(),
  }));
}

export function recordPollingStopped(key: string, meta: PollingChannelMeta): void {
  updatePollingDiagnostics(key, meta, (current) => ({
    ...current,
    status: 'stopped',
    next_due_at: null,
  }));
}