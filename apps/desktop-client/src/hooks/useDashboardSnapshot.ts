import { useEffect, useState } from 'react';
import { dashboardApi } from '../services/api';
import type { DashboardSnapshot } from '../types/dashboard';

const DEFAULT_REFRESH_INTERVAL_MS = 3000;

export function useDashboardSnapshot(refreshIntervalMs = DEFAULT_REFRESH_INTERVAL_MS) {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load(showLoading = false) {
      if (showLoading) {
        setLoading(true);
      }
      try {
        const data = await dashboardApi.getSnapshot();
        if (!cancelled) {
          setSnapshot(data);
          setError(null);
        }
      } catch (err) {
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
    };
  }, [refreshIntervalMs]);

  return { snapshot, setSnapshot, loading, error };
}