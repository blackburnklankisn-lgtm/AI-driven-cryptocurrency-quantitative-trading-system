import { useEffect, useState, useRef, useCallback } from 'react';
import { Activity, AlertTriangle, Square, RefreshCcw, Terminal, Wifi, WifiOff } from 'lucide-react';

const API_HOST = window.location.protocol === 'file:'
  ? '127.0.0.1:8000' 
  : window.location.host;

interface SystemStatus {
  status: string;
  mode: string;
  exchange: string;
  equity: number;
  circuit_broken: boolean;
  circuit_reason: string;
  positions: Record<string, number>;
  risk_state?: {
    daily_pnl: number;
    consecutive_losses: number;
    peak_equity: number;
  };
}

function App() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [wsConnected, setWsConnected] = useState(false);
  const [logsWsConnected, setLogsWsConnected] = useState(false);
  const [controlFeedback, setControlFeedback] = useState<{ msg: string; ok: boolean } | null>(null);

  const logEndRef = useRef<HTMLDivElement>(null);
  const logWsRef = useRef<WebSocket | null>(null);
  const statusWsRef = useRef<WebSocket | null>(null);
  // Batch buffer for log lines — flush every 200ms to reduce re-renders
  const logBatchRef = useRef<string[]>([]);

  // ── 日志批处理：每 200ms 统一 flush 到 state ──────────────
  const queueLog = useCallback((line: string) => {
    logBatchRef.current.push(line);
  }, []);

  useEffect(() => {
    const timer = setInterval(() => {
      if (logBatchRef.current.length === 0) return;
      const batch = logBatchRef.current.splice(0);
      setLogs(prev => [...prev, ...batch].slice(-200));
    }, 200);
    return () => clearInterval(timer);
  }, []);

  // ── 自动滚动（requestAnimationFrame 节流） ────────────────
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    });
    return () => cancelAnimationFrame(frame);
  }, [logs]);

  // ── WebSocket: 订阅 ws/status 替代 REST 轮询 ─────────────
  useEffect(() => {
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    const connectStatus = () => {
      const ws = new WebSocket(`ws://${API_HOST}/api/v1/ws/status`);
      statusWsRef.current = ws;

      ws.onopen = () => setWsConnected(true);

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.status !== undefined) setStatus(data);
        } catch (_) { }
      };

      ws.onclose = () => {
        setWsConnected(false);
        retryTimer = setTimeout(connectStatus, 3000);
      };

      ws.onerror = () => ws.close();
    };

    const pingInterval = setInterval(() => {
      if (statusWsRef.current?.readyState === WebSocket.OPEN) {
        statusWsRef.current.send('ping');
      }
    }, 10000);

    connectStatus();

    return () => {
      clearInterval(pingInterval);
      if (retryTimer) clearTimeout(retryTimer);
      statusWsRef.current?.close();
    };
  }, []);

  // ── WebSocket: 日志流（带自动重连） ──────────────────────
  useEffect(() => {
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    const connectLogs = () => {
      const ws = new WebSocket(`ws://${API_HOST}/api/v1/ws/logs`);
      logWsRef.current = ws;

      ws.onopen = () => setLogsWsConnected(true);

      ws.onmessage = (event) => {
        if (event.data !== 'pong') queueLog(event.data);
      };

      ws.onclose = () => {
        setLogsWsConnected(false);
        retryTimer = setTimeout(connectLogs, 3000);
      };

      ws.onerror = () => ws.close();
    };


    const pingInterval = setInterval(() => {
      if (logWsRef.current?.readyState === WebSocket.OPEN) {
        logWsRef.current.send('ping');
      }
    }, 10000);

    connectLogs();

    return () => {
      clearInterval(pingInterval);
      if (retryTimer) clearTimeout(retryTimer);
      logWsRef.current?.close();
    };
  }, [queueLog]);

  // ── 控制操作（带反馈提示） ────────────────────────────────
  const handleAction = useCallback(async (action: string) => {
    try {
      const res = await fetch(`http://${API_HOST}/api/v1/control`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      });
      const data = await res.json();
      setControlFeedback({ msg: data.message, ok: data.result === 'ok' });
      setTimeout(() => setControlFeedback(null), 3000);
    } catch (err) {
      setControlFeedback({ msg: 'Network error', ok: false });
      setTimeout(() => setControlFeedback(null), 3000);
    }
  }, []);

  return (
    <div className="min-h-screen flex flex-col p-6 space-y-6">

      {/* Header */}
      <header className="flex justify-between items-center bg-surface p-4 rounded-xl border border-border">
        <div>
          <h1 className="text-2xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-blue-400 to-indigo-500">
            AI Quant Trader
          </h1>
          <p className="text-slate-400 text-sm mt-1 flex items-center gap-2">
            {wsConnected
              ? <Wifi size={14} className="text-success" />
              : <WifiOff size={14} className="text-danger" />}
            {status ? `Connected · ${status.exchange} [${status.mode}]` : 'Connecting...'}
          </p>
        </div>

        <div className="flex items-center gap-4">
          {/* 操作反馈提示 */}
          {controlFeedback && (
            <span className={`text-xs px-3 py-1 rounded-full font-mono ${controlFeedback.ok ? 'bg-success/20 text-success' : 'bg-danger/20 text-danger'
              }`}>
              {controlFeedback.msg}
            </span>
          )}

          <button
            onClick={() => handleAction('reset_circuit')}
            className="flex items-center gap-2 px-4 py-2 bg-slate-800 hover:bg-slate-700 rounded-lg text-slate-200 transition-colors border border-border"
          >
            <RefreshCcw size={16} /> Reset Circuit
          </button>

          {status?.circuit_broken ? (
            <div className="flex flex-col items-end gap-1">
              <div className="flex items-center gap-2 px-4 py-2 bg-danger/20 text-danger rounded-lg outline outline-1 outline-danger">
                <AlertTriangle size={16} /> CIRCUIT BROKEN
              </div>
              {status.circuit_reason && (
                <span className="text-xs text-danger/70 font-mono max-w-xs truncate">
                  {status.circuit_reason}
                </span>
              )}
            </div>
          ) : (
            <button
              onClick={() => handleAction('stop')}
              className="flex items-center gap-2 px-4 py-2 bg-danger hover:bg-red-600 rounded-lg text-white shadow-lg shadow-danger/20 transition-all font-medium"
            >
              <Square size={16} fill="currentColor" /> Stop System
            </button>
          )}
        </div>
      </header>

      {/* Main Grid */}
      <div className="grid grid-cols-3 gap-6 flex-1">

        {/* Left Column: Stats */}
        <div className="col-span-1 space-y-6">
          {/* Equity Card */}
          <div className="bg-surface p-6 rounded-xl border border-border flex flex-col justify-center relative overflow-hidden">
            <div className="absolute top-0 right-0 p-4 opacity-10">
              <Activity size={80} />
            </div>
            <h3 className="text-slate-400 text-sm font-medium uppercase tracking-wider mb-2">Total Equity</h3>
            <div className="text-4xl font-bold text-white tracking-tight">
              ${status ? status.equity.toLocaleString('en-US', { minimumFractionDigits: 2 }) : '0.00'}
            </div>
            <div className="mt-4 text-xs text-primary font-mono bg-primary/10 px-2 py-1 rounded inline-flex w-max">
              Via WebSocket Push
            </div>
          </div>

          {/* Risk State Card */}
          {status?.risk_state && (
            <div className="bg-surface p-4 rounded-xl border border-border">
              <h3 className="text-slate-400 text-xs font-medium uppercase tracking-wider mb-3 border-b border-border pb-2">Risk State</h3>
              <div className="space-y-2 text-xs font-mono">
                <div className="flex justify-between">
                  <span className="text-slate-500">Daily PnL</span>
                  <span className={status.risk_state.daily_pnl >= 0 ? 'text-success' : 'text-danger'}>
                    {status.risk_state.daily_pnl >= 0 ? '+' : ''}{status.risk_state.daily_pnl.toFixed(2)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">Consec. Losses</span>
                  <span className={status.risk_state.consecutive_losses > 3 ? 'text-danger' : 'text-slate-300'}>
                    {status.risk_state.consecutive_losses}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">Peak Equity</span>
                  <span className="text-slate-300">${status.risk_state.peak_equity.toFixed(2)}</span>
                </div>
              </div>
            </div>
          )}

          {/* Positions Card */}
          <div className="bg-surface p-6 rounded-xl border border-border flex-1">
            <h3 className="text-slate-400 text-sm font-medium uppercase tracking-wider mb-4 border-b border-border pb-2">Active Positions</h3>
            {status && Object.keys(status.positions).length > 0 ? (
              <div className="space-y-3">
                {Object.entries(status.positions).map(([sym, qty]) => (
                  <div key={sym} className="flex justify-between items-center py-2 px-3 bg-slate-800/50 rounded">
                    <span className="font-semibold text-slate-200">{sym}</span>
                    <span className="font-mono text-primary">{qty}</span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-slate-500 text-sm text-center py-8">No Active Positions</div>
            )}
          </div>
        </div>

        {/* Right Column: Terminal */}
        <div className="col-span-2 bg-[#0a0f1c] rounded-xl border border-border font-mono text-xs p-4 flex flex-col relative overflow-hidden">
          <div className="flex items-center gap-2 mb-4 text-slate-500 border-b border-slate-800 pb-2">
            <Terminal size={14} />
            <span>Live Audit Stream</span>
            <span className="ml-auto text-slate-600">{logs.length} lines</span>
          </div>
            <div className="flex-1 overflow-auto p-4 font-mono text-sm space-y-1 scrollbar-thin scrollbar-thumb-gray-700">
              {logs.length === 0 && (
                <div className="flex flex-col items-center justify-center h-full text-gray-500 space-y-2">
                  <RefreshCcw className={`w-6 h-6 ${!logsWsConnected ? 'animate-spin' : ''}`} />
                  <p className="italic">
                    {!logsWsConnected 
                      ? "Establishing secure connection to audit stream..." 
                      : "Audit stream connected. Waiting for engine activity..."}
                  </p>
                </div>
              )}
              {logs.map((log, i) => (
                <div key={i} className="border-l-2 border-blue-500/30 pl-2 py-0.5 hover:bg-white/5 transition-colors">
                  <span className="text-blue-400 opacity-70">[{new Date().toLocaleTimeString()}]</span>{" "}
                  <span className={log.includes('ERROR') ? 'text-red-400' : log.includes('WARNING') ? 'text-yellow-400' : 'text-gray-300'}>
                    {log}
                  </span>
                </div>
              ))}
              <div ref={logEndRef} />
            </div>

        </div>

      </div>
    </div>
  );
}

export default App;
