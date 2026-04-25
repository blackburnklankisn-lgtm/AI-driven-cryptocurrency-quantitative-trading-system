import { useDataHealthStream } from '../hooks/useDataHealthStream';
import { zh } from '../utils/i18n';
import { SectionPanel } from '../components/layout/SectionPanel';
import type { DataFusionSnapshot } from '../types/dashboard';

interface DataFusionPageProps {
  snapshot: DataFusionSnapshot;
}

function HealthBadge({ value }: { value: unknown }) {
  const s = String(value ?? 'unknown').toLowerCase();
  const label = zh(value, '未知');
  if (s.includes('fresh') || s.includes('healthy') || s.includes('connected') || s.includes('ok')) {
    return <span className="dcc-badge dcc-badge--fresh">{label}</span>;
  }
  if (s.includes('stale') || s.includes('disconnected') || s.includes('error') || s.includes('fail')) {
    return <span className="dcc-badge dcc-badge--stale">{label}</span>;
  }
  return <span className="dcc-badge dcc-badge--partial">{label}</span>;
}

export function DataFusionPage({ snapshot }: DataFusionPageProps) {
  const { liveData, connected } = useDataHealthStream();
  const data = liveData ?? snapshot;

  return (
    <div className="dcc-page-grid">
      <SectionPanel title="数据源健康总览" kicker={connected ? 'data-health ws 实时连接' : '数据融合工作区'}>
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">价格数据源</h3>
            <div style={{ marginBottom: '12px' }}><HealthBadge value={data.price_feed_health} /></div>
            <h3 className="dcc-subtitle">新鲜度汇总</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(data.freshness_summary, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">订阅管理器</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(data.subscription_manager, null, 2)}</pre>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="数据源健康矩阵" kicker="全域数据可视化">
        <div className="dcc-four-col">
          <div>
            <h3 className="dcc-subtitle">OrderBook</h3>
            <HealthBadge value={(data.orderbook_health as Record<string, unknown>)?.status ?? data.orderbook_health} />
            <pre className="dcc-pre dcc-pre--compact" style={{ marginTop: '10px' }}>{JSON.stringify(data.orderbook_health, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">成交数据源</h3>
            <HealthBadge value={(data.trade_feed_health as Record<string, unknown>)?.status ?? data.trade_feed_health} />
            <pre className="dcc-pre dcc-pre--compact" style={{ marginTop: '10px' }}>{JSON.stringify(data.trade_feed_health, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">链上数据</h3>
            <HealthBadge value={(data.onchain_health as Record<string, unknown>)?.status ?? data.onchain_health} />
            <pre className="dcc-pre dcc-pre--compact" style={{ marginTop: '10px' }}>{JSON.stringify(data.onchain_health, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">情绪数据</h3>
            <HealthBadge value={(data.sentiment_health as Record<string, unknown>)?.status ?? data.sentiment_health} />
            <pre className="dcc-pre dcc-pre--compact" style={{ marginTop: '10px' }}>{JSON.stringify(data.sentiment_health, null, 2)}</pre>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="过期字段" kicker="新鲜度异常">
        {data.stale_fields.length ? (
          <ul className="dcc-list">
            {data.stale_fields.map((field) => (
              <li key={field}><span className="dcc-badge dcc-badge--stale">{field}</span></li>
            ))}
          </ul>
        ) : <p className="dcc-paragraph">无过期字段，所有数据源均新鲜。</p>}
      </SectionPanel>

      <SectionPanel title="最新价格" kicker="实时行情">
        {Object.keys(data.latest_prices ?? {}).length > 0 ? (
          <table className="dcc-table">
            <thead>
              <tr><th>标的</th><th>最新价</th></tr>
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
        ) : <p className="dcc-paragraph">暂无实时价格数据。</p>}
      </SectionPanel>
    </div>
  );
}