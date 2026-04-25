import { AlertTriangle } from 'lucide-react';
import { MetricCard } from '../components/cards/MetricCard';
import { SectionPanel } from '../components/layout/SectionPanel';
import type { RiskMatrixSnapshot } from '../types/dashboard';

interface RiskMatrixPageProps {
  snapshot: RiskMatrixSnapshot;
}

export function RiskMatrixPage({ snapshot }: RiskMatrixPageProps) {
  return (
    <div className="dcc-page-grid">
      <div className="dcc-metric-grid">
        <MetricCard
          label="Circuit Breaker"
          value={snapshot.circuit_broken ? 'TRIGGERED' : 'HEALTHY'}
          accent={snapshot.circuit_broken ? 'risk' : 'bull'}
          subtitle={snapshot.circuit_reason || 'No active circuit condition'}
          icon={<AlertTriangle size={18} />}
        />
        <MetricCard
          label="Cooldown Remaining"
          value={`${snapshot.circuit_cooldown_remaining_sec}s`}
          accent="neutral"
          subtitle={`Consecutive losses ${snapshot.consecutive_losses}`}
        />
        <MetricCard
          label="Budget Remaining"
          value={snapshot.budget_remaining_pct == null ? 'N/A' : `${(snapshot.budget_remaining_pct * 100).toFixed(1)}%`}
          accent="info"
          subtitle={`Position sizing ${snapshot.position_sizing_mode}`}
        />
      </div>

      <SectionPanel title="Risk State" kicker="Risk Matrix workspace">
        <pre className="dcc-pre">{JSON.stringify(snapshot.risk_state, null, 2)}</pre>
      </SectionPanel>

      <SectionPanel title="Advanced Risk Components" kicker="Kill Switch / Cooldown / DCA / Exit Plan">
        <div className="dcc-four-col">
          <div>
            <h3 className="dcc-subtitle">Kill Switch</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot.kill_switch, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">Cooldown</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot.cooldown, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">DCA Plan</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot.dca_plan, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">Exit Plan</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot.exit_plan, null, 2)}</pre>
          </div>
        </div>
      </SectionPanel>
    </div>
  );
}