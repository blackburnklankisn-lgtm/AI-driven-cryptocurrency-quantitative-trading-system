import { useEffect, useState } from 'react';
import { createWsChannel } from '../services/ws';
import type { ExecutionSnapshot } from '../types/dashboard';

export function useExecutionStream() {
  const [liveData, setLiveData] = useState<ExecutionSnapshot | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const dispose = createWsChannel<ExecutionSnapshot>({
      path: '/api/v2/ws/execution',
      onMessage: (data) => {
        console.debug('[desktop-client][execution-stream] update', data.generated_at);
        setLiveData(data);
      },
      onOpen: () => setConnected(true),
      onClose: () => setConnected(false),
      onError: () => setConnected(false),
    });
    return dispose;
  }, []);

  return { liveData, connected };
}
