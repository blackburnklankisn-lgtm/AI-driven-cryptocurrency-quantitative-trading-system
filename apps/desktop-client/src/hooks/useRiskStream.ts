import { useEffect, useState } from 'react';
import { createWsChannel } from '../services/ws';
import type { RiskMatrixSnapshot } from '../types/dashboard';

export function useRiskStream() {
  const [liveData, setLiveData] = useState<RiskMatrixSnapshot | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const dispose = createWsChannel<RiskMatrixSnapshot>({
      path: '/api/v2/ws/risk',
      onMessage: (data) => {
        console.debug('[desktop-client][risk-stream] update', data.generated_at);
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
