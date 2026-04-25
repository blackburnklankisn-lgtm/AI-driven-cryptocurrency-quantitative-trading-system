import { Activity, ShieldAlert, TrendingUp } from 'lucide-react';
import { MetricCard } from '../components/cards/MetricCard';
import { SectionPanel } from '../components/layout/SectionPanel';
import type { OverviewSnapshot } from '../types/dashboard';

interface OverviewPageProps {
  snapshot: OverviewSnapshot;
}

export function OverviewPage({ snapshot }: OverviewPageProps) {
  const positions = snapshot.positions_summary?.items ?? [];
  const weights = Object.entries(snapshot.strategy_weight_summary ?? {});

  return (
    <div className="dcc-page-grid">
      <div className="dcc-metric-grid">
        <MetricCard
          label="Total Equity"
          value={`$${(snapshot.equity ?? 0).toLocaleString('en-US', { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`}
          accent="info"
          subtitle={`Mode ${snapshot.mode ?? 'unknown'} · Exchange ${snapshot.exchange ?? 'unknown'}`}
          icon={<TrendingUp size={18} />}
        />
        <MetricCard
          label="Dominant Regime"
          value={`${snapshot.dominant_regime ?? 'unknown'}`}
          accent={snapshot.dominant_regime === 'bear' ? 'bear' : 'bull'}
          subtitle={`Confidence ${((snapshot.regime_confidence ?? 0) * 100).toFixed(1)}% · Stable ${snapshot.is_regime_stable ? 'Yes' : 'No'}`}
          icon={<Activity size={18} />}
        />
        <MetricCard
          label="Risk Level"
          value={`${snapshot.risk_level ?? 'unknown'}`}
          accent={snapshot.risk_level === 'critical' ? 'risk' : 'neutral'}
          subtitle={`Drawdown ${((snapshot.drawdown_pct ?? 0) * 100).toFixed(2)}% · Daily PnL ${(snapshot.daily_pnl ?? 0).toFixed(2)}`}
          icon={<ShieldAlert size={18} />}
        />
      </div>

      <SectionPanel title="Global Situation" kicker="Overview workspace">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">Alerts</h3>
            <ul className="dcc-list">
              {(snapshot.alerts?.length ? snapshot.alerts : ['No active alerts']).map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
          <div>
            <h3 className="dcc-subtitle">Feed Health</h3>
            <dl className="dcc-definition-list">
              <div><dt>Status</dt><dd>{snapshot.feed_health?.health ?? 'unknown'}</dd></div>
              <div><dt>Exchange</dt><dd>{snapshot.feed_health?.exchange ?? 'unknown'}</dd></div>
              <div><dt>Reconnects</dt><dd>{snapshot.feed_health?.reconnect_count ?? 0}</dd></div>
            </dl>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="Exposure & Positioning" kicker="Portfolio view">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">Positions</h3>
            <table className="dcc-table">
              <thead>
                <tr><th>Symbol</th><th>Qty</th><th>Last</th><th>Notional</th></tr>
              </thead>
              <tbody>
                {positions.length > 0 ? positions.map((item) => (
                  <tr key={item.symbol}>
                    <td>{item.symbol}</td>
                    <td>{item.quantity}</td>
                    <td>{item.last_price.toFixed(2)}</td>
                    <td>{item.notional.toFixed(2)}</td>
                  </tr>
                )) : (
                  <tr><td colSpan={4}>No active positions</td></tr>
                )}
              </tbody>
            </table>
          </div>
          <div>
            <h3 className="dcc-subtitle">Strategy Weight Summary</h3>
            <ul className="dcc-list">
              {weights.length > 0 ? weights.map(([key, value]) => (
                <li key={key}>{key}: {(value * 100).toFixed(1)}%</li>
              )) : <li>No orchestration weights available</li>}
            </ul>
          </div>
        </div>
      </SectionPanel>
    </div>
  );
}