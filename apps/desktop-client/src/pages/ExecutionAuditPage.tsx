import { useRef, useEffect } from 'react';
import { useExecutionStream } from '../hooks/useExecutionStream';
import { useAuditLogStream } from '../hooks/useAuditLogStream';
import { SectionPanel } from '../components/layout/SectionPanel';
import type { ExecutionSnapshot } from '../types/dashboard';

interface ExecutionAuditPageProps {
  snapshot: ExecutionSnapshot;
}

export function ExecutionAuditPage({ snapshot }: ExecutionAuditPageProps) {
  const { liveData, connected: execConnected } = useExecutionStream();
  const { logs, connected: logsConnected } = useAuditLogStream();
  const data = liveData ?? snapshot;
  const logEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    });
    return () => cancelAnimationFrame(frame);
  }, [logs]);

  return (
    <div className="dcc-page-grid">
      <SectionPanel title="Paper Execution Summary" kicker={execConnected ? 'execution ws live' : 'Execution & Audit workspace'}>
        <pre className="dcc-pre">{JSON.stringify(data.paper_summary, null, 2)}</pre>
      </SectionPanel>

      <SectionPanel title="Orders & Fills" kicker="Execution chain">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">Open Orders ({data.open_orders.length})</h3>
            {data.open_orders.length > 0 ? (
              <table className="dcc-table">
                <thead><tr><th>ID</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th></tr></thead>
                <tbody>
                  {data.open_orders.map((order, index) => (
                    <tr key={String(order.order_id ?? `order-${index}`)}>
                      <td>{String(order.order_id ?? 'N/A')}</td>
                      <td>{String(order.symbol ?? 'N/A')}</td>
                      <td>{String(order.side ?? 'N/A')}</td>
                      <td>{String(order.amount ?? 'N/A')}</td>
                      <td>{String(order.price ?? 'market')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <p className="dcc-paragraph">No open orders</p>}
          </div>
          <div>
            <h3 className="dcc-subtitle">Recent Fills ({data.recent_fills.length})</h3>
            {data.recent_fills.length > 0 ? (
              <table className="dcc-table">
                <thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th></tr></thead>
                <tbody>
                  {data.recent_fills.map((fill, index) => (
                    <tr key={`fill-${index}`}>
                      <td>{String(fill.symbol ?? 'N/A')}</td>
                      <td>{String(fill.side ?? 'N/A')}</td>
                      <td>{String(fill.amount ?? 'N/A')}</td>
                      <td>{String(fill.price ?? 'N/A')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <p className="dcc-paragraph">No recent fills</p>}
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="Position Exposure" kicker="Current holdings">
        <table className="dcc-table">
          <thead><tr><th>Symbol</th><th>Qty</th><th>Entry Price</th><th>Last Price</th><th>Unrealized PnL</th><th>Notional</th></tr></thead>
          <tbody>
            {data.positions.items.length > 0 ? data.positions.items.map((pos) => (
              <tr key={pos.symbol}>
                <td>{pos.symbol}</td>
                <td>{pos.quantity}</td>
                <td>{pos.entry_price == null ? 'N/A' : pos.entry_price.toFixed(2)}</td>
                <td>{pos.last_price.toFixed(2)}</td>
                <td className={pos.unrealized_pnl != null && pos.unrealized_pnl < 0 ? 'dcc-log-line--error' : ''}>
                  {pos.unrealized_pnl == null ? 'N/A' : pos.unrealized_pnl.toFixed(2)}
                </td>
                <td>{pos.notional.toFixed(2)}</td>
              </tr>
            )) : <tr><td colSpan={6}>No active positions</td></tr>}
          </tbody>
        </table>
      </SectionPanel>

      <SectionPanel title="Control Actions" kicker="Operator actions exposed by backend">
        <table className="dcc-table">
          <thead><tr><th>Action</th><th>Enabled</th></tr></thead>
          <tbody>
            {data.control_actions.length > 0 ? data.control_actions.map((action, index) => (
              <tr key={`${String(action.action ?? 'action')}-${index}`}>
                <td>{String(action.action ?? 'unknown')}</td>
                <td>
                  <span className={`dcc-badge ${action.enabled ? 'dcc-badge--fresh' : 'dcc-badge--stale'}`}>
                    {action.enabled ? 'enabled' : 'disabled'}
                  </span>
                </td>
              </tr>
            )) : <tr><td colSpan={2}>No control actions available</td></tr>}
          </tbody>
        </table>
      </SectionPanel>

      <SectionPanel title="Audit Log Stream" kicker={logsConnected ? 'ws/logs connected' : 'ws/logs reconnecting...'}>
        <div className="dcc-log-stream">
          {logs.length === 0 && <span className="dcc-log-line dcc-log-line--muted">Waiting for log events...</span>}
          {logs.map((line, index) => (
            <div
              key={index}
              className={`dcc-log-line ${line.includes('ERROR') ? 'dcc-log-line--error' : line.includes('WARNING') ? 'dcc-log-line--warning' : ''}`}
            >
              {line}
            </div>
          ))}
          <div ref={logEndRef} />
        </div>
      </SectionPanel>
    </div>
  );
}