export const HTTP_BASE_CANDIDATES = ['http://localhost:8000', 'http://127.0.0.1:8000'] as const;

let preferredBaseIndex = 0;

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

export async function fetchWithFallback(path: string, init?: RequestInit): Promise<Response> {
  let lastNetworkError: unknown = null;
  let lastHttpError: Error | null = null;

  for (const index of buildAttemptOrder()) {
    const base = HTTP_BASE_CANDIDATES[index];
    const url = `${base}${path}`;
    try {
      const response = await fetch(url, init);
      if (response.ok) {
        preferredBaseIndex = index;
        return response;
      }
      const body = await response.text();
      lastHttpError = new Error(`Request failed: ${response.status} ${path} @ ${base} (${body.slice(0, 200)})`);
    } catch (error) {
      lastNetworkError = error;
    }
  }

  if (lastHttpError) {
    throw lastHttpError;
  }
  throw new Error(`Request failed for ${path}: ${String(lastNetworkError)}`);
}

export function getPreferredWsBase(): string {
  return wsBaseFromHttp(HTTP_BASE_CANDIDATES[preferredBaseIndex]);
}

export function rotatePreferredBase(): void {
  preferredBaseIndex = (preferredBaseIndex + 1) % HTTP_BASE_CANDIDATES.length;
}
