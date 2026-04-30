import { useEffect, useState } from 'react';
import {
  getHttpDiagnosticsSnapshot,
  subscribeHttpDiagnostics,
  type HttpRouteDiagnostics,
} from '../services/backendEndpoint';
import {
  getPollingDiagnosticsSnapshot,
  subscribePollingDiagnostics,
  type PollingChannelDiagnostics,
} from '../services/pollingDiagnostics';
import {
  getWsDiagnosticsSnapshot,
  subscribeWsDiagnostics,
  type WsChannelDiagnostics,
} from '../services/ws';

export function useTransportDiagnostics() {
  const [wsDiagnostics, setWsDiagnostics] = useState<Record<string, WsChannelDiagnostics>>(
    () => getWsDiagnosticsSnapshot(),
  );
  const [httpDiagnostics, setHttpDiagnostics] = useState<Record<string, HttpRouteDiagnostics>>(
    () => getHttpDiagnosticsSnapshot(),
  );
  const [pollingDiagnostics, setPollingDiagnostics] = useState<Record<string, PollingChannelDiagnostics>>(
    () => getPollingDiagnosticsSnapshot(),
  );

  useEffect(() => {
    const unsubscribeWs = subscribeWsDiagnostics(() => {
      setWsDiagnostics(getWsDiagnosticsSnapshot());
    });
    const unsubscribeHttp = subscribeHttpDiagnostics(() => {
      setHttpDiagnostics(getHttpDiagnosticsSnapshot());
    });
    const unsubscribePolling = subscribePollingDiagnostics(() => {
      setPollingDiagnostics(getPollingDiagnosticsSnapshot());
    });
    return () => {
      unsubscribeWs();
      unsubscribeHttp();
      unsubscribePolling();
    };
  }, []);

  return { wsDiagnostics, httpDiagnostics, pollingDiagnostics };
}