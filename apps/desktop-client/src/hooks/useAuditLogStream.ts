import { useEffect, useRef, useState } from 'react';

export function useAuditLogStream() {
  const [logs, setLogs] = useState<string[]>([]);
  const [connected, setConnected] = useState(false);
  const batchRef = useRef<string[]>([]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retryTimer: number | null = null;
    let pingTimer: number | null = null;
    let closedByClient = false;

    const flushTimer = window.setInterval(() => {
      if (batchRef.current.length === 0) return;
      const batch = batchRef.current.splice(0);
      setLogs((prev) => [...prev, ...batch].slice(-300));
    }, 200);

    const connect = () => {
      console.info('[desktop-client][audit-log-stream] connecting');
      socket = new WebSocket('ws://localhost:8000/api/v1/ws/logs');
      socket.onopen = () => {
        console.info('[desktop-client][audit-log-stream] connected');
        setConnected(true);
        pingTimer = window.setInterval(() => {
          if (socket?.readyState === WebSocket.OPEN) socket.send('ping');
        }, 5000);
      };
      socket.onmessage = (event) => {
        const line = event.data as string;
        if (line !== 'pong') {
          batchRef.current.push(line);
        }
      };
      socket.onerror = () => {
        console.error('[desktop-client][audit-log-stream] error');
        setConnected(false);
      };
      socket.onclose = () => {
        setConnected(false);
        if (pingTimer !== null) {
          window.clearInterval(pingTimer);
          pingTimer = null;
        }
        if (!closedByClient) {
          retryTimer = window.setTimeout(connect, 3000);
        }
      };
    };

    connect();

    return () => {
      closedByClient = true;
      window.clearInterval(flushTimer);
      if (pingTimer !== null) window.clearInterval(pingTimer);
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      socket?.close();
    };
  }, []);

  return { logs, connected };
}
