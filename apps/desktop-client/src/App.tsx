import { useEffect, useState, useRef } from 'react';
import { Activity, AlertTriangle, Square, RefreshCcw, Terminal } from 'lucide-react';

interface SystemStatus {
  status: string;
  mode: string;
  exchange: string;
  equity: number;
  circuit_broken: boolean;
  positions: Record<string, number>;
}

function App() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const logEndRef = useRef<HTMLDivElement>(null);

  // Poll exactly as per Python Backend FastAPI setup
  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const res = await fetch('http://127.0.0.1:8000/api/v1/status');
        if (res.ok) {
          const data = await res.json();
          setStatus(data);
        }
      } catch (err) {
        console.error("Backend offline", err);
      }
    };
    
    fetchStatus();
    const interval = setInterval(fetchStatus, 3000);
    return () => clearInterval(interval);
  }, []);

  // Map WebSocket for logs
  useEffect(() => {
    const ws = new WebSocket('ws://127.0.0.1:8000/api/v1/ws/logs');
    ws.onmessage = (event) => {
      if (event.data !== 'pong') {
        setLogs(prev => [...prev.slice(-100), event.data]);
      }
    };
    const pingInterval = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send('ping');
    }, 10000);
    return () => {
      clearInterval(pingInterval);
      ws.close();
    };
  }, []);

  // Update scroll on new log
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);

  // Handle controls
  const handleAction = async (action: string) => {
    try {
      await fetch('http://127.0.0.1:8000/api/v1/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action })
      });
    } catch(err) {
      console.error(err);
    }
  }

  return (
    <div className="min-h-screen flex flex-col p-6 space-y-6">
      
      {/* Header */}
      <header className="flex justify-between items-center bg-surface p-4 rounded-xl border border-border">
        <div>
          <h1 className="text-2xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-blue-400 to-indigo-500">
            AI Quant Trader
          </h1>
          <p className="text-slate-400 text-sm mt-1 flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full ${status ? 'bg-success' : 'bg-danger'} animate-pulse`}></span>
            {status ? `Connected to ${status.exchange} [${status.mode}]` : 'Disconnected'}
          </p>
        </div>

        <div className="flex gap-4">
          <button 
            onClick={() => handleAction('reset_circuit')}
            className="flex items-center gap-2 px-4 py-2 bg-slate-800 hover:bg-slate-700 rounded-lg text-slate-200 transition-colors border border-border"
          >
            <RefreshCcw size={16} /> Reset Circuit
          </button>
          {status?.circuit_broken ? (
            <div className="flex items-center gap-2 px-4 py-2 bg-danger/20 text-danger rounded-lg outline outline-1 outline-danger">
              <AlertTriangle size={16} /> CIRCUIT BROKEN
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
          <div className="bg-surface p-6 rounded-xl border border-border flex flex-col justify-center relative overflow-hidden">
            <div className="absolute top-0 right-0 p-4 opacity-10">
              <Activity size={80} />
            </div>
            <h3 className="text-slate-400 text-sm font-medium uppercase tracking-wider mb-2">Total Equity</h3>
            <div className="text-4xl font-bold text-white tracking-tight">
              ${status ? status.equity.toLocaleString('en-US', {minimumFractionDigits: 2}) : '0.00'}
            </div>
            <div className="mt-4 text-xs text-primary font-mono bg-primary/10 px-2 py-1 rounded inline-flex w-max">
              Updated Live
            </div>
          </div>

          <div className="bg-surface p-6 rounded-xl border border-border flex-1">
            <h3 className="text-slate-400 text-sm font-medium uppercase tracking-wider mb-4 border-b border-border pb-2">Active Positions</h3>
            {status && Object.keys(status.positions).length > 0 ? (
              <div className="space-y-3">
                {Object.entries(status.positions).map(([sym, qty]) => (
                  <div key={sym} className="flex justify-between items-center py-2 px-3 bg-slate-800/50 rounded pointer-events-none">
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
            <Terminal size={14} /> <span>Live Audit Stream</span>
          </div>
          <div className="flex-1 overflow-y-auto pr-2 space-y-1 text-slate-300">
            {logs.length === 0 ? (
              <div className="opacity-50">Waiting for backend connection...</div>
            ) : (
              logs.map((log, i) => (
                <div key={i} className="break-all whitespace-pre-wrap leading-relaxed py-0.5 border-b border-slate-800/50 last:border-0 hover:bg-slate-800/20 transition-colors">
                  {log}
                </div>
              ))
            )}
            <div ref={logEndRef} />
          </div>
        </div>

      </div>
    </div>
  );
}

export default App;
