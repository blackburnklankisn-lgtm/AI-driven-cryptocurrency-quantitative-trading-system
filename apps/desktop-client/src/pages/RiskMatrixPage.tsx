import { useEffect, useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { getRiskEvents } from '../services/api';
import { useRiskStream } from '../hooks/useRiskStream';
import { MetricCard } from '../components/cards/MetricCard';
import { SectionPanel } from '../components/layout/SectionPanel';
import type { RiskEvent, RiskMatrixSnapshot } from '../types/dashboard';

interface RiskMatrixPageProps {
  snapshot: RiskMatrixSnapshot;
}

function BudgetBar({ pct }: { pct: number | null }) {
  const value = pct == null ? 1 : pct;
  const isLow = value < 0.2;
  return (
    <div className="dcc-progress-wrap">
      <div className="dcc-progress-bar">
        <div
          className={`dcc-progress-bar__fill ${isLow ? 'is-risk' : 'is-good'}`}
          style={{ width: `${(value * 100).toFixed(0)}%` }}
        />
      </div>
      <span className="dcc-progress-label">{pct == null ? 'N/A' : `${(pct * 100).toFixed(1)}% remaining`}</span>
    </div>
  );
}

export function RiskMatrixPage({ snapshot }: RiskMatrixPageProps) {
  const { liveData } = useRiskStream();
  const data = liveData ?? snapshot;
  const [riskEvents, setRiskEvents] = useState<RiskEvent[]>([]);

  useEffect(() => {
    getRiskEvents()
      .then(setRiskEvents)
      .catch((err: unknown) => console.error('[RiskMatrix] risk events fetch failed', err));
  }, []);

  return (
    <div className="dcc-page-grid">
      <div className="dcc-metric-grid">
        <MetricCard
          label="Circuit Breaker"
          value={data.circuit_broken ? 'TRIGGERED' : 'HEALTHY'}
          accent={data.circuit_broken ? 'risk' : 'bull'}
          subtitle={data.circuit_reason || 'No active circuit condition'}
          icon={<AlertTriangle size={18} />}
        />
        <MetricCard
          label="Cooldown Remaining"
          value={`${data.circuit_cooldown_remaining_sec}s`}
          accent="neutral"
          subtitle={`Consecutive losses ${data.consecutive_losses}`}
        />
        <MetricCard
          label="Budget Remaining"
          value={data.budget_remaining_pct == null ? 'N/A' : `${(data.budget_remaining_pct * 100).toFixed(1)}%`}
          accent="info"
          subtitle={`Position sizing ${data.position_sizing_mode}`}
        />
      </div>

      <SectionPanel title="Budget Usage" kicker="Risk Matrix workspace">
        <BudgetBar pct={data.budget_remaining_pct} />
      </SectionPanel>

      <SectionPanel title="Advanced Risk Components" kicker="Kill Switch / Cooldown / DCA / Exit Plan">
        <div className="dcc-four-col">
          <div>
            <h3 className="dcc-subtitle">Kill Switch</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(data.kill_switch, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">Cooldown</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(data.cooldown, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">DCA Plan</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(data.dca_plan, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">Exit Plan</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(data.exit_plan, null, 2)}</pre>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="Risk State" kicker="Full risk context">
        <pre className="dcc-pre">{JSON.stringify(data.risk_state, null, 2)}</pre>
      </SectionPanel>

      <SectionPanel title="Risk Event Timeline" kicker="Historical risk events">
        {riskEvents.length > 0 ? (
          <div className="dcc-timeline">
            {riskEvents.map((event, index) => (
              <div key={event.event_id ?? `evt-${index}`} className="dcc-timeline__item">
                <span className="dcc-timeline__dot" />
                <div className="dcc-timeline__body">
                  <div className="dcc-timeline__header">
                    <span className="dcc-badge dcc-badge--partial">{event.event_type ?? 'event'}</span>
                    <span className="dcc-timeline__time">{event.timestamp ?? 'unknown time'}</span>
                  </div>
                  <p className="dcc-timeline__reason">{event.reason ?? 'No reason provided'}</p>
                </div>
              </div>
            ))}
          </div>
        ) : <p className="dcc-paragraph">No risk events recorded yet.</p>}
      </SectionPanel>
    </div>
  );
}