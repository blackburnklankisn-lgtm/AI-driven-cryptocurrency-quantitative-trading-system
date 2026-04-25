import { SectionPanel } from '../components/layout/SectionPanel';
import type { ExecutionSnapshot } from '../types/dashboard';

interface ExecutionAuditPageProps {
  snapshot: ExecutionSnapshot;
}

export function ExecutionAuditPage({ snapshot }: ExecutionAuditPageProps) {
  return (
    <div className="dcc-page-grid">
      <SectionPanel title="Paper Execution Summary" kicker="Execution & Audit workspace">
        <pre className="dcc-pre">{JSON.stringify(snapshot.paper_summary, null, 2)}</pre>
      </SectionPanel>

      <SectionPanel title="Orders & Fills" kicker="Execution chain">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">Open Orders</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot.open_orders, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">Recent Fills</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot.recent_fills, null, 2)}</pre>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="Controls & Position Exposure" kicker="Operator actions">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">Control Actions</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot.control_actions, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">Positions</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot.positions, null, 2)}</pre>
          </div>
        </div>
      </SectionPanel>
    </div>
  );
}