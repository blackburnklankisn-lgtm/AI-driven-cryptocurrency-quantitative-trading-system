import { useEffect, useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { getRiskEvents } from '../services/api';
import { zh } from '../utils/i18n';
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
      <span className="dcc-progress-label">{pct == null ? '暂无' : `剩余 ${(pct * 100).toFixed(1)}%`}</span>
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
          label="熔断器"
          value={data.circuit_broken ? '已触发' : '健康'}
          accent={data.circuit_broken ? 'risk' : 'bull'}
          subtitle={data.circuit_reason ? zh(data.circuit_reason) : '无活动熔断条件'}
          icon={<AlertTriangle size={18} />}
        />
        <MetricCard
          label="冷却剩余"
          value={`${data.circuit_cooldown_remaining_sec}s`}
          accent="neutral"
          subtitle={`连续亏损 ${data.consecutive_losses}`}
        />
        <MetricCard
          label="预算剩余"
          value={data.budget_remaining_pct == null ? '暂无' : `${(data.budget_remaining_pct * 100).toFixed(1)}%`}
          accent="info"
          subtitle={`仓位模式 ${zh(data.position_sizing_mode, '未知')}`}
        />
      </div>

      <SectionPanel title="预算使用" kicker="风险矩阵工作区">
        <BudgetBar pct={data.budget_remaining_pct} />
      </SectionPanel>

      <SectionPanel title="高级风控组件" kicker="熔断开关 / 冷却 / DCA / 退出计划">
        <div className="dcc-four-col">
          <div>
            <h3 className="dcc-subtitle">熔断开关</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(data.kill_switch, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">冷却</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(data.cooldown, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">DCA 计划</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(data.dca_plan, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">退出计划</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(data.exit_plan, null, 2)}</pre>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="风险状态" kicker="完整风控上下文">
        <pre className="dcc-pre">{JSON.stringify(data.risk_state, null, 2)}</pre>
      </SectionPanel>

      <SectionPanel title="风险事件时间线" kicker="历史风险事件">
        {riskEvents.length > 0 ? (
          <div className="dcc-timeline">
            {riskEvents.map((event, index) => (
              <div key={event.event_id ?? `evt-${index}`} className="dcc-timeline__item">
                <span className="dcc-timeline__dot" />
                <div className="dcc-timeline__body">
                  <div className="dcc-timeline__header">
                    <span className="dcc-badge dcc-badge--partial">{zh(event.event_type, '事件')}</span>
                    <span className="dcc-timeline__time">{event.timestamp ?? '未知时间'}</span>
                  </div>
                  <p className="dcc-timeline__reason">{event.reason ? zh(event.reason) : '未提供原因'}</p>
                </div>
              </div>
            ))}
          </div>
        ) : <p className="dcc-paragraph">暂无风险事件记录。</p>}
      </SectionPanel>
    </div>
  );
}