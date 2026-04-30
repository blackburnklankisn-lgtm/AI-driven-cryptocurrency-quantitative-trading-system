import { useEffect, useState } from 'react';
import type { DiagnosticsSnapshot } from '../types/dashboard';
import { dashboardApi, normalizeDiagnosticsSnapshot } from '../services/api';
import {
  recordPollingFailure,
  recordPollingStart,
  recordPollingStopped,
  recordPollingSuccess,
} from '../services/pollingDiagnostics';
import { createWsChannel } from '../services/ws';

const DEFAULT_REFRESH_INTERVAL_MS = 15000;
const DIAGNOSTICS_POLLING_KEY = 'diagnostics-snapshot';

export function useDiagnosticsSnapshot(refreshIntervalMs = DEFAULT_REFRESH_INTERVAL_MS) {
  const [snapshot, setSnapshot] = useState<DiagnosticsSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const pollingMeta = {
      label: '统一诊断快照轮询',
      path: '/api/v2/diagnostics',
      refreshIntervalMs,
    };

    async function load(showLoading = false) {
      if (showLoading) {
        setLoading(true);
      }
      const startedAt = recordPollingStart(DIAGNOSTICS_POLLING_KEY, pollingMeta);
      try {
        const data = await dashboardApi.getDiagnostics();
        recordPollingSuccess(DIAGNOSTICS_POLLING_KEY, pollingMeta, startedAt);
        if (!cancelled) {
          setSnapshot(data);
          setError(null);
        }
      } catch (err) {
        recordPollingFailure(DIAGNOSTICS_POLLING_KEY, pollingMeta, startedAt, err);
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Unknown error');
        }
      } finally {
        if (!cancelled && showLoading) {
          setLoading(false);
        }
      }
    }

    void load(true);
    const intervalId = window.setInterval(() => {
      void load(false);
    }, refreshIntervalMs);

    const dispose = createWsChannel<DiagnosticsSnapshot>({
      path: '/api/v2/ws/diagnostics',
      onMessage: (data) => {
        if (!cancelled) {
          setSnapshot(normalizeDiagnosticsSnapshot(data));
          setError(null);
        }
      },
      onOpen: () => setConnected(true),
      onClose: () => setConnected(false),
      onError: () => setConnected(false),
    });

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
      dispose();
      recordPollingStopped(DIAGNOSTICS_POLLING_KEY, pollingMeta);
    };
  }, [refreshIntervalMs]);

  return { snapshot, loading, error, connected };
}