import { BrainCircuit, ShieldCheck } from 'lucide-react';
import { MetricCard } from '../components/cards/MetricCard';
import { SectionPanel } from '../components/layout/SectionPanel';
import type { AlphaBrainSnapshot } from '../types/dashboard';

interface AlphaBrainPageProps {
  snapshot: AlphaBrainSnapshot;
}

export function AlphaBrainPage({ snapshot }: AlphaBrainPageProps) {
  const probs = snapshot.regime_probs;
  const weights = Object.entries(snapshot.orchestrator.weights ?? {});

  return (
    <div className="dcc-page-grid">
      <div className="dcc-metric-grid">
        <MetricCard
          label="Dominant Regime"
          value={snapshot.dominant_regime}
          accent={snapshot.dominant_regime === 'bear' ? 'bear' : 'bull'}
          subtitle={`Confidence ${(snapshot.confidence * 100).toFixed(1)}%`}
          icon={<BrainCircuit size={18} />}
        />
        <MetricCard
          label="Gating Action"
          value={snapshot.orchestrator.gating_action}
          accent={snapshot.orchestrator.gating_action.includes('block') ? 'risk' : 'info'}
          subtitle={`Stable regime: ${snapshot.is_regime_stable ? 'Yes' : 'No'}`}
          icon={<ShieldCheck size={18} />}
        />
      </div>

      <SectionPanel title="Regime Probability Distribution" kicker="Alpha Brain workspace">
        <div className="dcc-prob-grid">
          <div className="dcc-prob-item"><span>Bull</span><strong>{(probs.bull * 100).toFixed(1)}%</strong></div>
          <div className="dcc-prob-item"><span>Bear</span><strong>{(probs.bear * 100).toFixed(1)}%</strong></div>
          <div className="dcc-prob-item"><span>Sideways</span><strong>{(probs.sideways * 100).toFixed(1)}%</strong></div>
          <div className="dcc-prob-item"><span>High Vol</span><strong>{(probs.high_vol * 100).toFixed(1)}%</strong></div>
        </div>
      </SectionPanel>

      <SectionPanel title="Orchestrator" kicker="Decision chain">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">Weights</h3>
            <ul className="dcc-list">
              {weights.length ? weights.map(([key, value]) => <li key={key}>{key}: {(value * 100).toFixed(1)}%</li>) : <li>No weights available</li>}
            </ul>
          </div>
          <div>
            <h3 className="dcc-subtitle">Block Reasons</h3>
            <ul className="dcc-list">
              {snapshot.orchestrator.block_reasons.length ? snapshot.orchestrator.block_reasons.map((reason) => <li key={reason}>{reason}</li>) : <li>No block reasons</li>}
            </ul>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="Continuous Learner" kicker="Adaptive ML">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">Active Model</h3>
            <dl className="dcc-definition-list">
              <div><dt>Version</dt><dd>{snapshot.continuous_learner.active_version ?? 'none'}</dd></div>
              <div><dt>Last Retrain</dt><dd>{snapshot.continuous_learner.last_retrain_at ?? 'never'}</dd></div>
              <div><dt>Learners</dt><dd>{snapshot.continuous_learner.count}</dd></div>
            </dl>
            <h3 className="dcc-subtitle" style={{ marginTop: '18px' }}>Thresholds</h3>
            <ul className="dcc-list">
              {Object.entries(snapshot.continuous_learner.thresholds).length
                ? Object.entries(snapshot.continuous_learner.thresholds).map(([key, value]) => (
                    <li key={key}>{key}: {typeof value === 'number' ? value.toFixed(4) : String(value)}</li>
                  ))
                : <li>No threshold data available</li>}
            </ul>
          </div>
          <div>
            <h3 className="dcc-subtitle">AI Analysis</h3>
            <p className="dcc-paragraph">{snapshot.ai_analysis}</p>

            <h3 className="dcc-subtitle" style={{ marginTop: '18px' }}>Version History</h3>
            <ul className="dcc-list">
              {snapshot.continuous_learner.items.length > 0 ? snapshot.continuous_learner.items.map((item, index) => (
                <li key={`${item.id ?? 'learner'}-${index}`}>
                  <strong>{item.id ?? `learner-${index + 1}`}</strong>: {item.versions?.length ? item.versions.join(' -> ') : (item.active_version ?? 'no version history')}
                </li>
              )) : <li>No learner version history available</li>}
            </ul>
          </div>
        </div>
      </SectionPanel>

      {snapshot.orchestrator.selected_results.length > 0 && (
        <SectionPanel title="Selected Strategy Results" kicker="Orchestrator output">
          <table className="dcc-table">
            <thead>
              <tr><th>Strategy</th><th>Symbol</th><th>Action</th><th>Confidence</th></tr>
            </thead>
            <tbody>
              {snapshot.orchestrator.selected_results.map((item, index) => (
                <tr key={`${item.strategy_id}-${index}`}>
                  <td>{item.strategy_id}</td>
                  <td>{item.symbol}</td>
                  <td>{item.action}</td>
                  <td>{(item.confidence * 100).toFixed(1)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </SectionPanel>
      )}
    </div>
  );
}