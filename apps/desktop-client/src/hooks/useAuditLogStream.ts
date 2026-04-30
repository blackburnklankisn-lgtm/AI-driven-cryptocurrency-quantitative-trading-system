import { useEffect, useRef, useState } from 'react';
import { createWsChannel } from '../services/ws';

export function useAuditLogStream() {
  const [logs, setLogs] = useState<string[]>([]);
  const [connected, setConnected] = useState(false);
  const batchRef = useRef<string[]>([]);

  useEffect(() => {
    const flushTimer = window.setInterval(() => {
      if (batchRef.current.length === 0) return;
      const batch = batchRef.current.splice(0);
      setLogs((prev) => [...prev, ...batch].slice(-300));
    }, 200);

    const dispose = createWsChannel<string>({
      path: '/api/v1/ws/logs',
      parseMessage: (raw) => raw,
      onMessage: (line) => {
        batchRef.current.push(line);
      },
      onOpen: () => setConnected(true),
      onClose: () => setConnected(false),
      onError: () => setConnected(false),
    });

    return () => {
      window.clearInterval(flushTimer);
      dispose();
    };
  }, []);

  return { logs, connected };
}
