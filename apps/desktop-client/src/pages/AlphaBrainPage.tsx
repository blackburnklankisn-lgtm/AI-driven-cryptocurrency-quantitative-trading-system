import { BrainCircuit, ShieldCheck } from 'lucide-react';
import { MetricCard } from '../components/cards/MetricCard';
import { SectionPanel } from '../components/layout/SectionPanel';
import type { AlphaBrainSnapshot } from '../types/dashboard';
import { zh } from '../utils/i18n';

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
          label="主导市场状态"
          value={zh(snapshot.dominant_regime, '未知')}
          accent={snapshot.dominant_regime === 'bear' ? 'bear' : 'bull'}
          subtitle={`置信度 ${(snapshot.confidence * 100).toFixed(1)}%`}
          icon={<BrainCircuit size={18} />}
        />
        <MetricCard
          label="门控动作"
          value={zh(snapshot.orchestrator.gating_action, '未知')}
          accent={snapshot.orchestrator.gating_action.includes('block') ? 'risk' : 'info'}
          subtitle={`状态稳定：${snapshot.is_regime_stable ? '是' : '否'}`}
          icon={<ShieldCheck size={18} />}
        />
      </div>

      <SectionPanel title="市场状态概率分布" kicker="阿尔法大脑工作区">
        <div className="dcc-prob-grid">
          <div className="dcc-prob-item"><span>牛市</span><strong>{(probs.bull * 100).toFixed(1)}%</strong></div>
          <div className="dcc-prob-item"><span>熊市</span><strong>{(probs.bear * 100).toFixed(1)}%</strong></div>
          <div className="dcc-prob-item"><span>震荡</span><strong>{(probs.sideways * 100).toFixed(1)}%</strong></div>
          <div className="dcc-prob-item"><span>高波动</span><strong>{(probs.high_vol * 100).toFixed(1)}%</strong></div>
        </div>
      </SectionPanel>

      <SectionPanel title="编排器" kicker="决策链">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">权重</h3>
            <ul className="dcc-list">
              {weights.length ? weights.map(([key, value]) => <li key={key}>{zh(key)}: {(value * 100).toFixed(1)}%</li>) : <li>暂无权重数据</li>}
            </ul>
          </div>
          <div>
            <h3 className="dcc-subtitle">阻断原因</h3>
            <ul className="dcc-list">
              {snapshot.orchestrator.block_reasons.length ? snapshot.orchestrator.block_reasons.map((reason) => <li key={reason}>{zh(reason)}</li>) : <li>无阻断原因</li>}
            </ul>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="持续学习器" kicker="自适应机器学习">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">当前模型</h3>
            <dl className="dcc-definition-list">
              <div><dt>版本</dt><dd>{snapshot.continuous_learner.active_version ?? '无'}</dd></div>
              <div><dt>最近重训</dt><dd>{snapshot.continuous_learner.last_retrain_at ?? '从未'}</dd></div>
              <div><dt>学习器数量</dt><dd>{snapshot.continuous_learner.count}</dd></div>
            </dl>
            <h3 className="dcc-subtitle" style={{ marginTop: '18px' }}>阈值</h3>
            <ul className="dcc-list">
              {Object.entries(snapshot.continuous_learner.thresholds).length
                ? Object.entries(snapshot.continuous_learner.thresholds).map(([key, value]) => (
                    <li key={key}>{key}: {typeof value === 'number' ? value.toFixed(4) : String(value)}</li>
                  ))
                : <li>暂无阈值数据</li>}
            </ul>
          </div>
          <div>
            <h3 className="dcc-subtitle">AI 分析</h3>
            <p className="dcc-paragraph">{snapshot.ai_analysis}</p>

            <h3 className="dcc-subtitle" style={{ marginTop: '18px' }}>版本历史</h3>
            <ul className="dcc-list">
              {snapshot.continuous_learner.items.length > 0 ? snapshot.continuous_learner.items.map((item, index) => (
                <li key={`${item.id ?? 'learner'}-${index}`}>
                  <strong>{item.id ?? `学习器-${index + 1}`}</strong>: {item.versions?.length ? item.versions.join(' -> ') : (item.active_version ?? '无版本历史')}
                </li>
              )) : <li>暂无学习器版本历史</li>}
            </ul>
          </div>
        </div>
      </SectionPanel>

      {snapshot.orchestrator.selected_results.length > 0 && (
        <SectionPanel title="已选策略结果" kicker="编排器输出">
          <table className="dcc-table">
            <thead>
              <tr><th>策略</th><th>标的</th><th>动作</th><th>置信度</th></tr>
            </thead>
            <tbody>
              {snapshot.orchestrator.selected_results.map((item, index) => (
                <tr key={`${item.strategy_id}-${index}`}>
                  <td>{item.strategy_id}</td>
                  <td>{item.symbol}</td>
                  <td>{zh(item.action)}</td>
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