import { useEffect, useState } from 'react';
import { dashboardApi } from '../services/api';
import {
  recordPollingFailure,
  recordPollingStart,
  recordPollingStopped,
  recordPollingSuccess,
} from '../services/pollingDiagnostics';
import type { DashboardSnapshot } from '../types/dashboard';

const DEFAULT_REFRESH_INTERVAL_MS = 3000;
const DASHBOARD_POLLING_KEY = 'dashboard-snapshot';

export function useDashboardSnapshot(refreshIntervalMs = DEFAULT_REFRESH_INTERVAL_MS) {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const pollingMeta = {
      label: '仪表盘快照轮询',
      path: '/api/v2/dashboard/snapshot',
      refreshIntervalMs,
    };

    async function load(showLoading = false) {
      if (showLoading) {
        setLoading(true);
      }
      const startedAt = recordPollingStart(DASHBOARD_POLLING_KEY, pollingMeta);
      try {
        const data = await dashboardApi.getSnapshot();
        recordPollingSuccess(DASHBOARD_POLLING_KEY, pollingMeta, startedAt);
        if (!cancelled) {
          setSnapshot(data);
          setError(null);
        }
      } catch (err) {
        recordPollingFailure(DASHBOARD_POLLING_KEY, pollingMeta, startedAt, err);
        if (!cancelled) {
          const message = err instanceof Error ? err.message : 'Unknown error';
          console.error('[desktop-client][hook] snapshot load failed', message);
          setError(message);
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

    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
      recordPollingStopped(DASHBOARD_POLLING_KEY, pollingMeta);
    };
  }, [refreshIntervalMs]);

  return { snapshot, setSnapshot, loading, error };
}