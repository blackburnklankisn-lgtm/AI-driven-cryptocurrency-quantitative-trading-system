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
            <h3 className="dcc-subtitle">Learner Inventory</h3>
            <ul className="dcc-list">
              {snapshot.continuous_learner.items.length ? snapshot.continuous_learner.items.map((item, index) => (
                <li key={`${index}-${String(item.id ?? 'learner')}`}>{String(item.id ?? 'unknown')} · Versions {Array.isArray(item.versions) ? item.versions.length : 0}</li>
              )) : <li>No learner attached</li>}
            </ul>
          </div>
          <div>
            <h3 className="dcc-subtitle">AI Analysis</h3>
            <p className="dcc-paragraph">{snapshot.ai_analysis}</p>
          </div>
        </div>
      </SectionPanel>
    </div>
  );
}