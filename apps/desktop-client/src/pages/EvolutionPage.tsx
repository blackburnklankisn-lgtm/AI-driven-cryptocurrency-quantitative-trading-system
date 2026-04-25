import { SectionPanel } from '../components/layout/SectionPanel';
import type { EvolutionSnapshot } from '../types/dashboard';

interface EvolutionPageProps {
  snapshot: EvolutionSnapshot;
}

export function EvolutionPage({ snapshot }: EvolutionPageProps) {
  const counts = Object.entries(snapshot.candidate_counts_by_status ?? {});

  return (
    <div className="dcc-page-grid">
      <SectionPanel title="Candidate Lifecycle" kicker="Evolution workspace">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">Counts by Status</h3>
            <ul className="dcc-list">
              {counts.length ? counts.map(([key, value]) => <li key={key}>{key}: {value}</li>) : <li>No candidates found</li>}
            </ul>
          </div>
          <div>
            <h3 className="dcc-subtitle">Active Candidates</h3>
            <ul className="dcc-list">
              {snapshot.active_candidates.length ? snapshot.active_candidates.map((item) => <li key={String(item.candidate_id)}>{String(item.candidate_id)} · {String(item.owner ?? 'unknown')}</li>) : <li>No active candidates</li>}
            </ul>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="Promotion / Retirement Timeline" kicker="Self-evolution history">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">Latest Promotions</h3>
            <ul className="dcc-list">
              {snapshot.latest_promotions.length ? snapshot.latest_promotions.map((item, index) => <li key={`promo-${index}`}>{JSON.stringify(item)}</li>) : <li>No promotion records</li>}
            </ul>
          </div>
          <div>
            <h3 className="dcc-subtitle">Latest Retirements</h3>
            <ul className="dcc-list">
              {snapshot.latest_retirements.length ? snapshot.latest_retirements.map((item, index) => <li key={`ret-${index}`}>{JSON.stringify(item)}</li>) : <li>No retirement records</li>}
            </ul>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="Weekly Params Optimizer" kicker="Optimization orchestration">
        <pre className="dcc-pre">{JSON.stringify(snapshot.weekly_params_optimizer, null, 2)}</pre>
      </SectionPanel>
    </div>
  );
}