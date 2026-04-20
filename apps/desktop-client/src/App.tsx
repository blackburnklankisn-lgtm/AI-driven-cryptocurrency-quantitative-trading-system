import { useEffect, useState, useRef, useCallback } from 'react';
import { AlertTriangle, Square, RefreshCcw, Terminal, Wifi, WifiOff, BarChart3, BrainCircuit } from 'lucide-react';
import { createChart, CandlestickSeries, type IChartApi, type ISeriesApi, type CandlestickData } from 'lightweight-charts';

// Use 'localhost' so both IPv4 and IPv6 loopback resolve correctly.
// '127.0.0.1' only works when the OS explicitly maps it to IPv4; on some
// Windows configs the Uvicorn dual-stack listener only answers on ::1.
const API_HOST = 'localhost:8000';
console.log('[App] API_HOST =', API_HOST, '| protocol =', window.location.protocol);

interface SystemStatus {
  status: string;
  mode: string;
  exchange: string;
  equity: number;
  circuit_broken: boolean;
  circuit_reason: string;
  positions: Record<string, number>;
  ai_analysis?: string;
  latest_prices?: Record<string, number>;
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
  const [controlFeedback, setControlFeedback] = useState<{ msg: string; ok: boolean } | null>(null);

  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);

  const logEndRef = useRef<HTMLDivElement>(null);
  const logWsRef = useRef<WebSocket | null>(null);
  const statusWsRef = useRef<WebSocket | null>(null);
  const logBatchRef = useRef<string[]>([]);

  // Diagnostic refs for detecting stale data
  const prevPricesRef = useRef<Record<string, number>>({});
  const statusMsgCountRef = useRef(0);
  const lastChartBarRef = useRef<{ time: number; close: number } | null>(null);

  // ── 1. Chart Initialization ───────────────────────────
  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      layout: { 
        background: { color: '#0f172a' }, 
        textColor: '#94a3b8' 
      },
      grid: { 
        vertLines: { color: '#1e293b' }, 
        horzLines: { color: '#1e293b' } 
      },
      width: chartContainerRef.current.clientWidth,
      height: 380, // Slightly taller for better visibility
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e', 
      downColor: '#ef4444', 
      borderVisible: false,
      wickUpColor: '#22c55e', 
      wickDownColor: '#ef4444',
    });

    chartRef.current = chart;
    seriesRef.current = series;

    const handleResize = () => {
      if (chartContainerRef.current) {
        chart.applyOptions({ width: chartContainerRef.current.clientWidth });
      }
    };
    window.addEventListener('resize', handleResize);

    // Initial and periodic fetch for history klines until data is present
    const fetchKlines = async (trigger: string = 'manual') => {
      const url = `http://${API_HOST}/api/v1/klines?symbol=BTC/USDT`;
      console.log(`[Chart] ▶ fetchKlines triggered by: ${trigger} at ${new Date().toLocaleTimeString()}`);
      try {
        const res = await fetch(url);
        console.log('[Chart] Klines HTTP status:', res.status);
        const data = await res.json();
        if (Array.isArray(data) && data.length > 0) {
          const lastBar = data[data.length - 1] as { time: number; close: number };
          const prev = lastChartBarRef.current;
          const barChanged = !prev || prev.time !== lastBar.time || prev.close !== lastBar.close;
          console.log(
            `[Chart] ${data.length} bars received. Last bar: time=${new Date(lastBar.time * 1000).toISOString()} close=${lastBar.close}`,
            barChanged ? '✅ Bar is NEW/CHANGED vs prev render' : '❌ Bar UNCHANGED from last fetch'
          );
          if (prev) {
            console.log(`[Chart] Previous last bar: time=${new Date(prev.time * 1000).toISOString()} close=${prev.close}`);
          }
          lastChartBarRef.current = { time: lastBar.time, close: lastBar.close };
          series.setData(data as CandlestickData[]);
          chart.timeScale().fitContent();
          return true;
        }
        console.warn('[Chart] Klines array empty or wrong format — retrying in 5 s');
      } catch (e) {
        console.error('[Chart] Klines fetch failed:', e);
      }
      return false;
    };

    // ① Quick-retry every 5 s until backend kline cache is ready (first load)
    fetchKlines('initial');
    let initialLoaded = false;
    const quickRetryInterval = setInterval(async () => {
      if (!initialLoaded) {
        console.log(`[Chart] ⏳ Quick-retry tick at ${new Date().toLocaleTimeString()} — waiting for kline data`);
        initialLoaded = await fetchKlines('quick-retry');
        if (initialLoaded) console.log('[Chart] ✅ Initial klines loaded — quick-retry interval will idle');
      }
    }, 5_000);

    // ② Permanent 60 s refresh — mirrors backend poll_interval_s so chart
    //    always shows newly completed bars without a page reload.
    console.log(`[Chart] ⏰ steadyRefreshInterval registered at ${new Date().toLocaleTimeString()} — fires every 60 s`);
    const steadyRefreshInterval = setInterval(async () => {
      console.log(`[Chart] ⏱ 60 s refresh tick at ${new Date().toLocaleTimeString()}`);
      await fetchKlines('steady-60s');
    }, 60_000);

    return () => {
      window.removeEventListener('resize', handleResize);
      clearInterval(quickRetryInterval);
      clearInterval(steadyRefreshInterval);
      chart.remove();
    };
  }, []);

  // ── 2. Logic & WebSockets ──────────────────────────────────
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

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    });
    return () => cancelAnimationFrame(frame);
  }, [logs]);

  useEffect(() => {
    let retryTimer: any = null;
    let retryCount = 0;
    let destroyed = false;
    const connectStatus = () => {
      if (destroyed) return;
      const url = `ws://${API_HOST}/api/v1/ws/status`;
      console.log(`[WS:status] Connecting (attempt #${retryCount + 1}):`, url);
      const ws = new WebSocket(url);
      statusWsRef.current = ws;
      ws.onopen = () => {
        if (destroyed) { ws.close(); return; }
        console.log('[WS:status] ✅ Connected');
        retryCount = 0;
        setWsConnected(true);
      };
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.status !== undefined) {
            setStatus(data);
            // ── Diagnostic: detect whether latest_prices actually changes between pushes ──
            statusMsgCountRef.current++;
            const prices: Record<string, number> = data.latest_prices ?? {};
            const prev = prevPricesRef.current;
            const changedSyms = Object.entries(prices).filter(([s, p]) => prev[s] !== p);
            if (changedSyms.length > 0) {
              console.log(
                `[WS:status] msg#${statusMsgCountRef.current} ✅ PRICE CHANGED:`,
                changedSyms.map(([s, p]) => `${s}: ${prev[s] ?? 'N/A'} → ${p}`).join(' | ')
              );
              prevPricesRef.current = { ...prices };
            } else {
              // Log every 5th unchanged message to avoid console spam
              if (statusMsgCountRef.current % 5 === 0) {
                console.log(
                  `[WS:status] msg#${statusMsgCountRef.current} — prices stable (ticker worker refreshes every ~5 s, ws/status pushes every 3 s):`,
                  prices
                );
              }
            }
            if (statusMsgCountRef.current === 1) {
              // First message — log full detail for verification
              console.log('[WS:status] 🏁 First status received:', {
                status: data.status, mode: data.mode, exchange: data.exchange,
                equity: data.equity, latest_prices: prices,
              });
              if (Object.keys(prices).length === 0) {
                console.warn('[WS:status] ⚠️ latest_prices is EMPTY on first push — backend may not have fetched tickers yet');
              }
            }
          }
        } catch (e) {
          console.warn('[WS:status] Failed to parse message:', event.data, e);
        }
      };
      ws.onclose = (e) => {
        if (destroyed) return;
        retryCount++;
        console.warn(`[WS:status] ❌ Closed (code=${e.code}, reason='${e.reason || 'none'}') — retry #${retryCount} in 3 s`);
        setWsConnected(false);
        retryTimer = setTimeout(connectStatus, 3000);
      };
      ws.onerror = (e) => {
        console.error('[WS:status] Error event:', e);
        ws.close();
      };
    };
    connectStatus();
    return () => {
      destroyed = true;
      if (retryTimer) clearTimeout(retryTimer);
      statusWsRef.current?.close();
    };
  }, []);

  useEffect(() => {
    let retryTimer: any = null;
    let retryCount = 0;
    let destroyed = false;
    const connectLogs = () => {
      if (destroyed) return;
      const url = `ws://${API_HOST}/api/v1/ws/logs`;
      console.log(`[WS:logs] Connecting (attempt #${retryCount + 1}):`, url);
      const ws = new WebSocket(url);
      logWsRef.current = ws;
      ws.onopen = () => {
        if (destroyed) { ws.close(); return; }
        console.log('[WS:logs] ✅ Connected');
        retryCount = 0;
      };
      ws.onmessage = (event) => {
        if (event.data !== 'pong') queueLog(event.data);
      };
      ws.onclose = (e) => {
        if (destroyed) return;
        retryCount++;
        console.warn(`[WS:logs] ❌ Closed (code=${e.code}) — retry #${retryCount} in 3 s`);
        retryTimer = setTimeout(connectLogs, 3000);
      };
      ws.onerror = (e) => {
        console.error('[WS:logs] Error event:', e);
        ws.close();
      };
    };
    connectLogs();
    return () => {
      destroyed = true;
      if (retryTimer) clearTimeout(retryTimer);
      logWsRef.current?.close();
    };
  }, [queueLog]);

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
    <div className="min-h-screen flex flex-col p-6 space-y-6 bg-[#020617] text-slate-200">
      {/* Header */}
      <header className="flex justify-between items-center bg-slate-900/50 p-4 rounded-xl border border-slate-800">
        <div>
          <h1 className="text-2xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-blue-400 to-indigo-500">
            AI Quant Trader <span className="text-xs font-mono opacity-50 ml-2">v1.2 PRO</span>
          </h1>
          <p className="text-slate-400 text-sm mt-1 flex items-center gap-2">
            {wsConnected ? <Wifi size={14} className="text-green-500" /> : <WifiOff size={14} className="text-red-500" />}
            {status ? `Connected · ${status.exchange} [${status.mode}]` : 'Connecting...'}
          </p>
        </div>

        <div className="flex items-center gap-4">
          {controlFeedback && (
            <span className={`text-xs px-3 py-1 rounded-full font-mono ${controlFeedback.ok ? 'bg-green-500/20 text-green-400' : 'bg-red-500/20 text-red-400'}`}>
              {controlFeedback.msg}
            </span>
          )}
          <button onClick={() => handleAction('reset_circuit')} className="flex items-center gap-2 px-4 py-2 bg-slate-800 hover:bg-slate-700 rounded-lg transition-colors border border-slate-700">
            <RefreshCcw size={16} /> Reset Circuit
          </button>

          {status?.circuit_broken ? (
            <div className="flex items-center gap-2 px-4 py-2 bg-red-500/20 text-red-400 rounded-lg border border-red-500/50">
              <AlertTriangle size={16} /> CIRCUIT BROKEN
            </div>
          ) : (
            <button onClick={() => handleAction('stop')} className="flex items-center gap-2 px-4 py-2 bg-red-600 hover:bg-red-500 rounded-lg text-white transition-all font-medium">
              <Square size={16} fill="currentColor" /> Stop
            </button>
          )}
        </div>
      </header>

      {/* Main Grid */}
      <div className="grid grid-cols-4 gap-6 flex-1">
        
        {/* Left Column: Stats & AI */}
        <div className="col-span-1 space-y-6">
          <div className="bg-slate-900/50 p-6 rounded-xl border border-slate-800 relative overflow-hidden">
            <h3 className="text-slate-500 text-xs font-bold uppercase tracking-widest mb-2">Total Equity</h3>
            <div className="text-4xl font-bold text-white tracking-tight">
              ${status ? status.equity.toLocaleString('en-US', { minimumFractionDigits: 2 }) : '0.00'}
            </div>
          </div>

          {/* Live Price Ticker — updated every 3 s via ws/status push */}
          <div className="bg-slate-900/50 p-4 rounded-xl border border-slate-800">
            <h3 className="text-slate-500 text-xs font-bold uppercase tracking-widest mb-3 border-b border-slate-800 pb-2">Live Prices</h3>
            <div className="space-y-2">
              {(['BTC/USDT', 'ETH/USDT', 'SOL/USDT'] as const).map(sym => {
                const price = status?.latest_prices?.[sym];
                const label = sym.replace('/USDT', '');
                return (
                  <div key={sym} className="flex justify-between items-center p-2 bg-slate-800/30 rounded-lg">
                    <span className="text-xs font-bold text-slate-400">{label}</span>
                    <span className="font-mono text-sm text-green-400">
                      {price != null
                        ? `$${price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
                        : <span className="text-slate-600 animate-pulse">---</span>}
                    </span>
                  </div>
                );
              })}
              <p className="text-slate-700 text-xs text-right mt-1">via ws/status · 3 s</p>
            </div>
          </div>

          <div className="bg-indigo-950/20 p-5 rounded-xl border border-indigo-500/30">
            <div className="flex items-center gap-2 text-indigo-400 mb-3">
              <BrainCircuit size={18} />
              <h3 className="text-xs font-bold uppercase tracking-widest">Gemini Market Analysis</h3>
            </div>
            <p className="text-sm text-indigo-200/80 leading-relaxed italic">
              "{status?.ai_analysis || 'AI is aggregating market depth data...'}"
            </p>
          </div>

          <div className="bg-slate-900/50 p-4 rounded-xl border border-slate-800">
            <h3 className="text-slate-500 text-xs font-bold uppercase tracking-widest mb-3 border-b border-slate-800 pb-2">Active Positions</h3>
            {status && Object.keys(status.positions).length > 0 ? (
              <div className="space-y-2">
                {Object.entries(status.positions).map(([sym, qty]) => (
                  <div key={sym} className="flex justify-between items-center p-3 bg-slate-800/30 rounded-lg">
                    <span className="font-bold text-slate-300">{sym}</span>
                    <span className="font-mono text-blue-400">{qty}</span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-slate-600 text-sm text-center py-4">No positions</div>
            )}
          </div>
        </div>

        {/* Center/Right Column: Chart & Terminal */}
        <div className="col-span-3 space-y-6 flex flex-col">
          <div className="bg-slate-900/50 rounded-xl border border-slate-800 overflow-hidden flex flex-col">
            <div className="flex items-center gap-2 p-3 bg-slate-800/30 border-b border-slate-800">
              <BarChart3 size={16} className="text-blue-400" />
              <span className="text-xs font-bold uppercase tracking-widest text-slate-400">BTC/USDT Real-time K-Line</span>
            </div>
            <div ref={chartContainerRef} className="w-full h-[380px]" />
          </div>

          <div className="flex-1 bg-black/40 rounded-xl border border-slate-800 p-4 font-mono text-xs flex flex-col overflow-hidden">
            <div className="flex items-center gap-2 mb-3 text-slate-500 border-b border-slate-800 pb-2">
              <Terminal size={14} />
              <span>Audit Log Stream</span>
              <span className="ml-auto opacity-50">{logs.length} lines</span>
            </div>
            <div className="flex-1 overflow-auto space-y-1">
              {logs.map((log, i) => (
                <div key={i} className="hover:bg-white/5 px-2 py-0.5 rounded transition-colors">
                  <span className={log.includes('ERROR') ? 'text-red-400' : log.includes('WARNING') ? 'text-yellow-400' : 'text-slate-300'}>
                    {log}
                  </span>
                </div>
              ))}
              <div ref={logEndRef} />
            </div>
          </div>
        </div>

      </div>
    </div>
  );
}

export default App;
