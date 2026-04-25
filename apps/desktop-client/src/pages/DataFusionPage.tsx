import { SectionPanel } from '../components/layout/SectionPanel';
import type { DataFusionSnapshot } from '../types/dashboard';

interface DataFusionPageProps {
  snapshot: DataFusionSnapshot;
}

export function DataFusionPage({ snapshot }: DataFusionPageProps) {
  return (
    <div className="dcc-page-grid">
      <SectionPanel title="Feed Health" kicker="Data Fusion workspace">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">Freshness</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot.freshness_summary, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">Subscription Manager</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot.subscription_manager, null, 2)}</pre>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="Source Health Matrix" kicker="Omni-data visibility">
        <div className="dcc-four-col">
          <div><h3 className="dcc-subtitle">OrderBook</h3><pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot.orderbook_health, null, 2)}</pre></div>
          <div><h3 className="dcc-subtitle">Trade Feed</h3><pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot.trade_feed_health, null, 2)}</pre></div>
          <div><h3 className="dcc-subtitle">On-chain</h3><pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot.onchain_health, null, 2)}</pre></div>
          <div><h3 className="dcc-subtitle">Sentiment</h3><pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot.sentiment_health, null, 2)}</pre></div>
        </div>
      </SectionPanel>

      <SectionPanel title="Stale Fields" kicker="Freshness exceptions">
        <ul className="dcc-list">
          {snapshot.stale_fields.length ? snapshot.stale_fields.map((field) => <li key={field}>{field}</li>) : <li>No stale fields</li>}
        </ul>
      </SectionPanel>
    </div>
  );
}