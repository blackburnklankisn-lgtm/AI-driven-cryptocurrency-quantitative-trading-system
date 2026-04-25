import { useEffect, useState } from 'react';
import { dashboardApi } from '../services/api';
import type { DashboardSnapshot } from '../types/dashboard';

export function useDashboardSnapshot() {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const data = await dashboardApi.getSnapshot();
        if (!cancelled) {
          setSnapshot(data);
        }
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : 'Unknown error';
          console.error('[desktop-client][hook] snapshot load failed', message);
          setError(message);
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  return { snapshot, setSnapshot, loading, error };
}