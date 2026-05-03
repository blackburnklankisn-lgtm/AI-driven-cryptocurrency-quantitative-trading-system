import { getPreferredWsEndpointBase, rotatePreferredEndpointBase } from './backendEndpoint';

export interface WsChannelDiagnostics {
  path: string;
  status: string;
  subscription_count: number;
  open_count: number;
  message_count: number;
  reconnect_count: number;
  last_url: string | null;
  last_connect_at: string | null;
  last_message_at: string | null;
  last_close_at: string | null;
  last_error_at: string | null;
  last_error: string | null;
  last_error_kind: string | null;
  last_close_code: number | null;
  last_close_reason: string | null;
  last_close_was_clean: boolean | null;
  status_detail: string | null;
}

const wsDiagnostics = new Map<string, WsChannelDiagnostics>();
const wsListeners = new Set<() => void>();

function nowIso(): string {
  return new Date().toISOString();
}

function emitWsDiagnostics(): void {
  wsListeners.forEach((listener) => listener());
}

function updateWsDiagnostics(
  path: string,
  updater: (current: WsChannelDiagnostics) => WsChannelDiagnostics,
): void {
  const current = wsDiagnostics.get(path) ?? {
    path,
    status: 'idle',
    subscription_count: 0,
    open_count: 0,
    message_count: 0,
    reconnect_count: 0,
    last_url: null,
    last_connect_at: null,
    last_message_at: null,
    last_close_at: null,
    last_error_at: null,
    last_error: null,
    last_error_kind: null,
    last_close_code: null,
    last_close_reason: null,
    last_close_was_clean: null,
    status_detail: null,
  };
  wsDiagnostics.set(path, updater(current));
  emitWsDiagnostics();
}

export function getWsDiagnosticsSnapshot(): Record<string, WsChannelDiagnostics> {
  return Object.fromEntries(
    Array.from(wsDiagnostics.entries()).map(([key, value]) => [key, { ...value }]),
  );
}

export function subscribeWsDiagnostics(listener: () => void): () => void {
  wsListeners.add(listener);
  return () => {
    wsListeners.delete(listener);
  };
}

export interface WsChannelOptions<T> {
  path: string;
  onMessage: (data: T) => void;
  onOpen?: () => void;
  onClose?: () => void;
  onError?: (error: Event) => void;
  parseMessage?: (raw: string) => T;
}

export function createWsChannel<T>(options: WsChannelOptions<T>): () => void {
  let socket: WebSocket | null = null;
  let retryTimer: number | null = null;
  let closedByClient = false;

  const updateErrorState = (kind: string, message: string, detail?: string) => {
    updateWsDiagnostics(options.path, (current) => ({
      ...current,
      status: 'error',
      last_error_at: nowIso(),
      last_error: message,
      last_error_kind: kind,
      status_detail: detail ?? message,
    }));
  };

  updateWsDiagnostics(options.path, (current) => ({
    ...current,
    subscription_count: current.subscription_count + 1,
  }));

  const connect = () => {
    const url = `${getPreferredWsEndpointBase()}${options.path}`;
    console.info('[desktop-client][ws] connecting', url);
    updateWsDiagnostics(options.path, (current) => ({
      ...current,
      status: 'connecting',
      last_url: url,
      last_connect_at: nowIso(),
      status_detail: `connecting ${url}`,
    }));
    socket = new WebSocket(url);
    socket.onopen = () => {
      console.info('[desktop-client][ws] connected', url);
      updateWsDiagnostics(options.path, (current) => ({
        ...current,
        status: 'open',
        open_count: current.open_count + 1,
        last_url: url,
        last_connect_at: nowIso(),
        status_detail: null,
      }));
      options.onOpen?.();
    };
    socket.onmessage = (event) => {
      const raw = typeof event.data === 'string' ? event.data : String(event.data);
      if (raw === 'pong') {
        return;
      }

      let parsed: T;
      try {
        parsed = options.parseMessage ? options.parseMessage(raw) : (JSON.parse(raw) as T);
      } catch (error) {
        console.error('[desktop-client][ws] parse error', url, raw, error);
        updateErrorState(
          'parse',
          error instanceof Error ? error.message : 'message parse error',
          `parse failed for ${url}`,
        );
        return;
      }

      try {
        console.debug('[desktop-client][ws] message', url, parsed);
        options.onMessage(parsed);
        updateWsDiagnostics(options.path, (current) => ({
          ...current,
          status: 'open',
          message_count: current.message_count + 1,
          last_message_at: nowIso(),
          status_detail: null,
        }));
      } catch (error) {
        console.error('[desktop-client][ws] handler error', url, parsed, error);
        updateErrorState(
          'handler',
          error instanceof Error ? error.message : 'message handler error',
          `handler failed for ${url}`,
        );
      }
    };
    socket.onerror = (error) => {
      console.error('[desktop-client][ws] error', url, error);
      updateErrorState('socket', 'websocket error', `socket error for ${url}`);
      options.onError?.(error);
    };
    socket.onclose = (event) => {
      console.warn('[desktop-client][ws] closed', url, event.code, event.reason, event.wasClean);
      updateWsDiagnostics(options.path, (current) => ({
        ...current,
        status: closedByClient ? 'inactive' : 'retrying',
        reconnect_count: closedByClient ? current.reconnect_count : current.reconnect_count + 1,
        last_close_at: nowIso(),
        last_close_code: event.code,
        last_close_reason: event.reason || null,
        last_close_was_clean: event.wasClean,
        status_detail: closedByClient
          ? 'client unsubscribed'
          : `socket closed code=${event.code}${event.reason ? ` reason=${event.reason}` : ''}`,
      }));
      options.onClose?.();
      if (!closedByClient) {
        rotatePreferredEndpointBase();
        retryTimer = window.setTimeout(connect, 3000);
      }
    };
  };

  connect();

  const pingTimer = window.setInterval(() => {
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send('ping');
    }
  }, 5000);

  return () => {
    closedByClient = true;
    window.clearInterval(pingTimer);
    if (retryTimer !== null) {
      window.clearTimeout(retryTimer);
    }
    socket?.close();
    updateWsDiagnostics(options.path, (current) => ({
      ...current,
      status: 'inactive',
      subscription_count: Math.max(0, current.subscription_count - 1),
      last_close_at: nowIso(),
      status_detail: 'client unsubscribed',
    }));
  };
}