import { useEffect, useState } from 'react';
import { createWsChannel } from '../services/ws';
import type { DataFusionSnapshot } from '../types/dashboard';

export function useDataHealthStream() {
  const [liveData, setLiveData] = useState<DataFusionSnapshot | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const dispose = createWsChannel<DataFusionSnapshot>({
      path: '/api/v2/ws/data-health',
      onMessage: (data) => {
        console.debug('[desktop-client][data-health-stream] update', data.generated_at);
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
