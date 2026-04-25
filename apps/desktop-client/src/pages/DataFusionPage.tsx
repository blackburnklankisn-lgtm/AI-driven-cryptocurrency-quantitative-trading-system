import { useDataHealthStream } from '../hooks/useDataHealthStream';
import { SectionPanel } from '../components/layout/SectionPanel';
import type { DataFusionSnapshot } from '../types/dashboard';

interface DataFusionPageProps {
  snapshot: DataFusionSnapshot;
}

function HealthBadge({ value }: { value: unknown }) {
  const s = String(value ?? 'unknown').toLowerCase();
  if (s.includes('fresh') || s.includes('healthy') || s.includes('connected') || s.includes('ok')) {
    return <span className="dcc-badge dcc-badge--fresh">{s}</span>;
  }
  if (s.includes('stale') || s.includes('disconnected') || s.includes('error') || s.includes('fail')) {
    return <span className="dcc-badge dcc-badge--stale">{s}</span>;
  }
  return <span className="dcc-badge dcc-badge--partial">{s}</span>;
}

export function DataFusionPage({ snapshot }: DataFusionPageProps) {
  const { liveData, connected } = useDataHealthStream();
  const data = liveData ?? snapshot;

  return (
    <div className="dcc-page-grid">
      <SectionPanel title="Feed Health Overview" kicker={connected ? 'data-health ws live' : 'Data Fusion workspace'}>
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">Price Feed</h3>
            <div style={{ marginBottom: '12px' }}><HealthBadge value={data.price_feed_health} /></div>
            <h3 className="dcc-subtitle">Freshness Summary</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(data.freshness_summary, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">Subscription Manager</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(data.subscription_manager, null, 2)}</pre>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="Source Health Matrix" kicker="Omni-data visibility">
        <div className="dcc-four-col">
          <div>
            <h3 className="dcc-subtitle">OrderBook</h3>
            <HealthBadge value={(data.orderbook_health as Record<string, unknown>)?.status ?? data.orderbook_health} />
            <pre className="dcc-pre dcc-pre--compact" style={{ marginTop: '10px' }}>{JSON.stringify(data.orderbook_health, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">Trade Feed</h3>
            <HealthBadge value={(data.trade_feed_health as Record<string, unknown>)?.status ?? data.trade_feed_health} />
            <pre className="dcc-pre dcc-pre--compact" style={{ marginTop: '10px' }}>{JSON.stringify(data.trade_feed_health, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">On-chain</h3>
            <HealthBadge value={(data.onchain_health as Record<string, unknown>)?.status ?? data.onchain_health} />
            <pre className="dcc-pre dcc-pre--compact" style={{ marginTop: '10px' }}>{JSON.stringify(data.onchain_health, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">Sentiment</h3>
            <HealthBadge value={(data.sentiment_health as Record<string, unknown>)?.status ?? data.sentiment_health} />
            <pre className="dcc-pre dcc-pre--compact" style={{ marginTop: '10px' }}>{JSON.stringify(data.sentiment_health, null, 2)}</pre>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="Stale Fields" kicker="Freshness exceptions">
        {data.stale_fields.length ? (
          <ul className="dcc-list">
            {data.stale_fields.map((field) => (
              <li key={field}><span className="dcc-badge dcc-badge--stale">{field}</span></li>
            ))}
          </ul>
        ) : <p className="dcc-paragraph">No stale fields — all data sources fresh.</p>}
      </SectionPanel>

      <SectionPanel title="Latest Prices" kicker="Realtime marks">
        {Object.keys(data.latest_prices ?? {}).length > 0 ? (
          <table className="dcc-table">
            <thead>
              <tr><th>Symbol</th><th>Last Price</th></tr>
            </thead>
            <tbody>
              {Object.entries(data.latest_prices).map(([symbol, price]) => (
                <tr key={symbol}>
                  <td>{symbol}</td>
                  <td>{price.toFixed(4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="dcc-paragraph">No realtime prices available yet.</p>}
      </SectionPanel>
    </div>
  );
}