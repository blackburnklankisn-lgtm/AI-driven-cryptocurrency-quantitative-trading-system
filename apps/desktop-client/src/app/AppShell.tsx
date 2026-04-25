import { useMemo, useState } from 'react';
import { Activity, BrainCircuit, DatabaseZap, History, Shield, TerminalSquare } from 'lucide-react';
import { useDashboardSnapshot } from '../hooks/useDashboardSnapshot';
import { useDashboardSocket } from '../hooks/useDashboardSocket';
import { postControlAction } from '../services/api';
import type { DashboardSnapshot, WorkspaceKey } from '../types/dashboard';
import { OverviewPage } from '../pages/OverviewPage';
import { AlphaBrainPage } from '../pages/AlphaBrainPage';
import { EvolutionPage } from '../pages/EvolutionPage';
import { RiskMatrixPage } from '../pages/RiskMatrixPage';
import { DataFusionPage } from '../pages/DataFusionPage';
import { ExecutionAuditPage } from '../pages/ExecutionAuditPage';

const navigation: Array<{ key: WorkspaceKey; label: string; icon: typeof Activity }> = [
  { key: 'overview', label: 'Overview', icon: Activity },
  { key: 'alpha-brain', label: 'Alpha Brain', icon: BrainCircuit },
  { key: 'evolution', label: 'Evolution', icon: History },
  { key: 'risk-matrix', label: 'Risk Matrix', icon: Shield },
  { key: 'data-fusion', label: 'Data Fusion', icon: DatabaseZap },
  { key: 'execution-audit', label: 'Execution & Audit', icon: TerminalSquare },
];

export function AppShell() {
  const { snapshot, setSnapshot, loading, error } = useDashboardSnapshot();
  const [workspace, setWorkspace] = useState<WorkspaceKey>('overview');
  const [actionFeedback, setActionFeedback] = useState('');
  const connected = useDashboardSocket((data: DashboardSnapshot) => {
    console.info('[desktop-client][shell] dashboard snapshot pushed', data.generated_at);
    setSnapshot(data);
  });

  const activeLabel = useMemo(() => navigation.find((item) => item.key === workspace)?.label ?? workspace, [workspace]);
  const overview = snapshot?.overview;

  async function handleControl(action: string) {
    const result = await postControlAction(action);
    setActionFeedback(`${action}: ${result.message}`);
    window.setTimeout(() => setActionFeedback(''), 4000);
  }

  function renderWorkspace() {
    if (!snapshot) {
      return <div className="dcc-empty">Waiting for dashboard snapshot...</div>;
    }
    switch (workspace) {
      case 'overview':
        return <OverviewPage snapshot={snapshot.overview} />;
      case 'alpha-brain':
        return <AlphaBrainPage snapshot={snapshot.alpha_brain} />;
      case 'evolution':
        return <EvolutionPage snapshot={snapshot.evolution} />;
      case 'risk-matrix':
        return <RiskMatrixPage snapshot={snapshot.risk_matrix} />;
      case 'data-fusion':
        return <DataFusionPage snapshot={snapshot.data_fusion} />;
      case 'execution-audit':
        return <ExecutionAuditPage snapshot={snapshot.execution} />;
      default:
        return <div className="dcc-empty">Unknown workspace</div>;
    }
  }

  return (
    <div className="dcc-shell">
      <aside className="dcc-sidebar">
        <div className="dcc-brand">
          <div className="dcc-brand__kicker">AI Quant Trader</div>
          <h1>Control Center</h1>
          <p>Alpha Brain · Evolution · Risk Matrix · Data Fusion</p>
        </div>
        <nav className="dcc-nav">
          {navigation.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              type="button"
              className={`dcc-nav__item ${workspace === key ? 'is-active' : ''}`}
              onClick={() => {
                console.info('[desktop-client][shell] workspace switch', key);
                setWorkspace(key);
              }}
            >
              <Icon size={16} />
              <span>{label}</span>
            </button>
          ))}
        </nav>
      </aside>

      <main className="dcc-main">
        <header className="dcc-topbar">
          <div>
            <div className="dcc-topbar__kicker">Workspace</div>
            <h2>{activeLabel}</h2>
          </div>
          <div className="dcc-health-strip">
            <span className={`dcc-pill ${connected ? 'is-good' : 'is-risk'}`}>{connected ? 'dashboard ws connected' : 'dashboard ws disconnected'}</span>
            <span className="dcc-pill">mode {overview?.mode ?? 'unknown'}</span>
            <span className="dcc-pill">exchange {overview?.exchange ?? 'unknown'}</span>
            <span className="dcc-pill">regime {overview?.dominant_regime ?? 'unknown'}</span>
            <span className={`dcc-pill ${overview?.risk_level === 'critical' ? 'is-risk' : 'is-info'}`}>risk {overview?.risk_level ?? 'unknown'}</span>
            <span className={`dcc-pill ${overview?.feed_health?.health === 'healthy' ? 'is-good' : overview?.feed_health?.health === 'degraded' ? 'is-risk' : 'is-info'}`}>
              feed {overview?.feed_health?.health ?? 'unknown'}
            </span>
            {(snapshot?.overview.alerts?.length ?? 0) > 0 && (
              <span
                className="dcc-alert-badge"
                title={snapshot!.overview.alerts!.join('\n')}
              >
                ⚠ {snapshot!.overview.alerts!.length} alert{snapshot!.overview.alerts!.length > 1 ? 's' : ''}
              </span>
            )}
          </div>
        </header>

        <div className="dcc-content-wrap">
          <section className="dcc-content">
            {loading && !snapshot ? <div className="dcc-empty">Loading dashboard snapshot...</div> : null}
            {error ? <div className="dcc-error">Snapshot load error: {error}</div> : null}
            {renderWorkspace()}
          </section>

          <aside className="dcc-context-panel">
            <section className="dcc-context-card">
              <div className="dcc-context-card__kicker">Operator Controls</div>
              <div className="dcc-control-stack">
                <button type="button" onClick={() => handleControl('reset_circuit')}>Reset Circuit</button>
                <button type="button" onClick={() => handleControl('trigger_circuit_test')}>Trigger Circuit Test</button>
                <button type="button" onClick={() => handleControl('stop')}>Stop Trader</button>
              </div>
              {actionFeedback ? <p className="dcc-feedback">{actionFeedback}</p> : null}
            </section>

            <section className="dcc-context-card">
              <div className="dcc-context-card__kicker">AI Insight Drawer</div>
              <p className="dcc-paragraph">{snapshot?.alpha_brain.ai_analysis ?? 'Waiting for AI analysis...'}</p>
            </section>

            {(snapshot?.overview.alerts?.length ?? 0) > 0 && (
              <section className="dcc-context-card">
                <div className="dcc-context-card__kicker">Active Alerts</div>
                <ul className="dcc-list" style={{ paddingLeft: 0, listStyle: 'none' }}>
                  {snapshot!.overview.alerts!.map((alert, index) => (
                    <li key={index} style={{ padding: '6px 0', borderBottom: '1px solid rgba(90,118,153,0.12)', color: 'var(--dcc-risk)', fontSize: '13px' }}>
                      ⚠ {alert}
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section className="dcc-context-card">
              <div className="dcc-context-card__kicker">Latest Snapshot</div>
              <dl className="dcc-definition-list">
                <div><dt>Generated</dt><dd>{snapshot?.generated_at ?? 'N/A'}</dd></div>
                <div><dt>Status</dt><dd>{overview?.status ?? 'unknown'}</dd></div>
                <div><dt>Equity</dt><dd>{overview?.equity?.toFixed(2) ?? '0.00'}</dd></div>
                <div><dt>Reconnects</dt><dd>{overview?.feed_health?.reconnect_count ?? 0}</dd></div>
              </dl>
            </section>
          </aside>
        </div>
      </main>
    </div>
  );
}