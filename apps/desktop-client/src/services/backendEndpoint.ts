export const HTTP_BASE_CANDIDATES = ['http://localhost:8000', 'http://127.0.0.1:8000'] as const;

let preferredBaseIndex = 0;

export interface HttpRouteDiagnostics {
  path: string;
  request_count: number;
  success_count: number;
  failure_count: number;
  last_requested_at: string | null;
  last_success_at: string | null;
  last_failure_at: string | null;
  last_error: string | null;
  last_base: string | null;
  preferred_base: string;
  last_latency_ms: number | null;
}

const httpDiagnostics = new Map<string, HttpRouteDiagnostics>();
const httpListeners = new Set<() => void>();

function nowIso(): string {
  return new Date().toISOString();
}

function emitHttpDiagnostics(): void {
  httpListeners.forEach((listener) => listener());
}

function updateHttpDiagnostics(
  path: string,
  updater: (current: HttpRouteDiagnostics) => HttpRouteDiagnostics,
): void {
  const current = httpDiagnostics.get(path) ?? {
    path,
    request_count: 0,
    success_count: 0,
    failure_count: 0,
    last_requested_at: null,
    last_success_at: null,
    last_failure_at: null,
    last_error: null,
    last_base: null,
    preferred_base: HTTP_BASE_CANDIDATES[preferredBaseIndex],
    last_latency_ms: null,
  };
  const next = updater(current);
  next.preferred_base = HTTP_BASE_CANDIDATES[preferredBaseIndex];
  httpDiagnostics.set(path, next);
  emitHttpDiagnostics();
}

export function getHttpDiagnosticsSnapshot(): Record<string, HttpRouteDiagnostics> {
  return Object.fromEntries(
    Array.from(httpDiagnostics.entries()).map(([key, value]) => [key, { ...value }]),
  );
}

export function subscribeHttpDiagnostics(listener: () => void): () => void {
  httpListeners.add(listener);
  return () => {
    httpListeners.delete(listener);
  };
}

function buildAttemptOrder(): number[] {
  if (HTTP_BASE_CANDIDATES.length <= 1) {
    return [0];
  }
  const order: number[] = [preferredBaseIndex];
  for (let i = 0; i < HTTP_BASE_CANDIDATES.length; i += 1) {
    if (i !== preferredBaseIndex) {
      order.push(i);
    }
  }
  return order;
}

export function wsBaseFromHttp(httpBase: string): string {
  return httpBase.replace(/^http/i, 'ws');
}

export async function fetchWithEndpointRetry(path: string, init?: RequestInit): Promise<Response> {
  let lastNetworkError: unknown = null;
  let lastHttpError: Error | null = null;
  let lastAttemptedBase: string | null = null;
  const startedAt = performance.now();

  updateHttpDiagnostics(path, (current) => ({
    ...current,
    request_count: current.request_count + 1,
    last_requested_at: nowIso(),
    last_error: null,
  }));

  for (const index of buildAttemptOrder()) {
    const base = HTTP_BASE_CANDIDATES[index];
    const url = `${base}${path}`;
    lastAttemptedBase = base;
    try {
      const response = await fetch(url, init);
      if (response.ok) {
        preferredBaseIndex = index;
        updateHttpDiagnostics(path, (current) => ({
          ...current,
          success_count: current.success_count + 1,
          last_success_at: nowIso(),
          last_base: base,
          preferred_base: HTTP_BASE_CANDIDATES[preferredBaseIndex],
          last_latency_ms: Math.round((performance.now() - startedAt) * 100) / 100,
          last_error: null,
        }));
        return response;
      }
      const body = await response.text();
      lastHttpError = new Error(`Request failed: ${response.status} ${path} @ ${base} (${body.slice(0, 200)})`);
    } catch (error) {
      lastNetworkError = error;
    }
  }

  if (lastHttpError) {
    updateHttpDiagnostics(path, (current) => ({
      ...current,
      failure_count: current.failure_count + 1,
      last_failure_at: nowIso(),
      last_error: lastHttpError?.message ?? 'Request failed',
      last_base: lastAttemptedBase,
      last_latency_ms: Math.round((performance.now() - startedAt) * 100) / 100,
    }));
    throw lastHttpError;
  }
  const networkError = new Error(`Request failed for ${path}: ${String(lastNetworkError)}`);
  updateHttpDiagnostics(path, (current) => ({
    ...current,
    failure_count: current.failure_count + 1,
    last_failure_at: nowIso(),
    last_error: networkError.message,
    last_base: lastAttemptedBase,
    last_latency_ms: Math.round((performance.now() - startedAt) * 100) / 100,
  }));
  throw networkError;
}

export function getPreferredWsEndpointBase(): string {
  return wsBaseFromHttp(HTTP_BASE_CANDIDATES[preferredBaseIndex]);
}

export function rotatePreferredEndpointBase(): void {
  preferredBaseIndex = (preferredBaseIndex + 1) % HTTP_BASE_CANDIDATES.length;
}
