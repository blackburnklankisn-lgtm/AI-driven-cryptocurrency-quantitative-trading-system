import { getPreferredWsBase, rotatePreferredBase } from './backendEndpoint';

export interface WsChannelOptions<T> {
  path: string;
  onMessage: (data: T) => void;
  onOpen?: () => void;
  onClose?: () => void;
  onError?: (error: Event) => void;
}

export function createWsChannel<T>(options: WsChannelOptions<T>): () => void {
  let socket: WebSocket | null = null;
  let retryTimer: number | null = null;
  let closedByClient = false;

  const connect = () => {
    const url = `${getPreferredWsBase()}${options.path}`;
    console.info('[desktop-client][ws] connecting', url);
    socket = new WebSocket(url);
    socket.onopen = () => {
      console.info('[desktop-client][ws] connected', url);
      options.onOpen?.();
    };
    socket.onmessage = (event) => {
      if (event.data === 'pong') {
        return;
      }
      try {
        const parsed = JSON.parse(event.data) as T;
        console.debug('[desktop-client][ws] message', url, parsed);
        options.onMessage(parsed);
      } catch (error) {
        console.error('[desktop-client][ws] parse error', url, event.data, error);
      }
    };
    socket.onerror = (error) => {
      console.error('[desktop-client][ws] error', url, error);
      options.onError?.(error);
    };
    socket.onclose = () => {
      console.warn('[desktop-client][ws] closed', url);
      options.onClose?.();
      if (!closedByClient) {
        rotatePreferredBase();
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
  };
}