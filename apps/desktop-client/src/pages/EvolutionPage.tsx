import { useEffect, useState } from 'react';
import { postControlAction, getEvolutionReports } from '../services/api';
import { SectionPanel } from '../components/layout/SectionPanel';
import type { EvolutionReport, EvolutionSnapshot } from '../types/dashboard';

interface EvolutionPageProps {
  snapshot: EvolutionSnapshot;
}

export function EvolutionPage({ snapshot }: EvolutionPageProps) {
  const counts = Object.entries(snapshot.candidate_counts_by_status ?? {});
  const [reports, setReports] = useState<EvolutionReport[]>([]);
  const [rollbackFeedback, setRollbackFeedback] = useState('');

  useEffect(() => {
    getEvolutionReports()
      .then(setReports)
      .catch((err: unknown) => console.error('[EvolutionPage] reports fetch failed', err));
  }, []);

  async function handleRollback() {
    const result = await postControlAction('rollback_evolution');
    setRollbackFeedback(`Rollback: ${result.message}`);
    window.setTimeout(() => setRollbackFeedback(''), 4000);
  }

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

      <SectionPanel title="Promotion / Retirement / Rollback Timeline" kicker="Self-evolution history">
        <div className="dcc-three-col">
          <div>
            <h3 className="dcc-subtitle">Latest Promotions</h3>
            <ul className="dcc-list">
              {snapshot.latest_promotions.length ? snapshot.latest_promotions.map((item, index) => (
                <li key={`promo-${index}`}>
                  <span className="dcc-badge dcc-badge--fresh">promoted</span>{' '}
                  {String(item.candidate_id ?? JSON.stringify(item))}
                </li>
              )) : <li>No promotion records</li>}
            </ul>
          </div>
          <div>
            <h3 className="dcc-subtitle">Latest Retirements</h3>
            <ul className="dcc-list">
              {snapshot.latest_retirements.length ? snapshot.latest_retirements.map((item, index) => (
                <li key={`ret-${index}`}>
                  <span className="dcc-badge dcc-badge--stale">retired</span>{' '}
                  {String(item.candidate_id ?? JSON.stringify(item))}
                </li>
              )) : <li>No retirement records</li>}
            </ul>
          </div>
          <div>
            <h3 className="dcc-subtitle">Latest Rollbacks</h3>
            <ul className="dcc-list">
              {snapshot.latest_rollbacks.length ? snapshot.latest_rollbacks.map((item, index) => (
                <li key={`rb-${index}`}>
                  <span className="dcc-badge dcc-badge--partial">rolled back</span>{' '}
                  {String(item.candidate_id ?? JSON.stringify(item))}
                </li>
              )) : <li>No rollback records</li>}
            </ul>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel
        title="Manual Rollback"
        kicker="Operator action"
        actions={
          <button type="button" className="dcc-action-btn" onClick={handleRollback}>
            Rollback Evolution
          </button>
        }
      >
        {rollbackFeedback
          ? <p className="dcc-feedback">{rollbackFeedback}</p>
          : <p className="dcc-paragraph">Trigger a manual rollback of the latest evolution step. Use when a recent promotion is underperforming.</p>}
      </SectionPanel>

      <SectionPanel title="Evolution Reports" kicker="Historical runs">
        {reports.length > 0 ? (
          <table className="dcc-table">
            <thead>
              <tr><th>Report ID</th><th>Candidate</th><th>Result</th><th>Created At</th></tr>
            </thead>
            <tbody>
              {reports.map((r, index) => (
                <tr key={r.report_id ?? `report-${index}`}>
                  <td>{r.report_id ?? 'N/A'}</td>
                  <td>{r.candidate_id ?? 'N/A'}</td>
                  <td>{r.result ?? 'N/A'}</td>
                  <td>{r.created_at ?? 'N/A'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="dcc-paragraph">No evolution reports available yet.</p>}
      </SectionPanel>

      <SectionPanel title="A/B Experiments" kicker="Active experiments &amp; lift">
        {Object.keys(snapshot.ab_experiments ?? {}).length > 0 ? (
          <table className="dcc-table">
            <thead>
              <tr><th>Experiment</th><th>Control</th><th>Treatment</th><th>Lift</th><th>Status</th></tr>
            </thead>
            <tbody>
              {Object.entries(snapshot.ab_experiments).map(([expId, exp]) => {
                const e = exp as Record<string, unknown>;
                const lift = Number(e.lift ?? 0);
                return (
                  <tr key={expId}>
                    <td>{expId}</td>
                    <td>{String(e.control ?? 'N/A')}</td>
                    <td>{String(e.treatment ?? 'N/A')}</td>
                    <td style={{ color: lift >= 0 ? 'var(--dcc-bull)' : 'var(--dcc-risk)' }}>
                      {lift >= 0 ? '+' : ''}{lift.toFixed(2)}%
                    </td>
                    <td>
                      <span className={`dcc-badge ${String(e.status ?? '').includes('active') ? 'dcc-badge--fresh' : 'dcc-badge--partial'}`}>
                        {String(e.status ?? 'unknown')}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : <p className="dcc-paragraph">No active A/B experiments. Experiments are created automatically by the Self-Evolution Engine when candidates reach shadow phase.</p>}
      </SectionPanel>

      <SectionPanel title="Weekly Params Optimizer" kicker="Optimization orchestration">
        <pre className="dcc-pre">{JSON.stringify(snapshot.weekly_params_optimizer, null, 2)}</pre>
      </SectionPanel>
    </div>
  );
}