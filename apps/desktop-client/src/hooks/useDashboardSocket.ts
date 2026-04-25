import { useEffect, useRef, useState } from 'react';
import { normalizeDashboardSnapshot } from '../services/api';
import { createWsChannel } from '../services/ws';
import type { DashboardSnapshot } from '../types/dashboard';

export function useDashboardSocket(onSnapshot: (snapshot: DashboardSnapshot) => void) {
  const [connected, setConnected] = useState(false);
  const onSnapshotRef = useRef(onSnapshot);

  useEffect(() => {
    onSnapshotRef.current = onSnapshot;
  }, [onSnapshot]);

  useEffect(() => {
    const dispose = createWsChannel<DashboardSnapshot>({
      path: '/api/v2/ws/dashboard',
      onMessage: (data) => {
        onSnapshotRef.current(normalizeDashboardSnapshot(data));
      },
      onOpen: () => setConnected(true),
      onClose: () => setConnected(false),
      onError: () => setConnected(false),
    });

    return dispose;
  }, []);

  return connected;
}
