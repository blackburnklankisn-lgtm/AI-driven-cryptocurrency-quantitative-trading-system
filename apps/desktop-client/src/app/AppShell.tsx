import { useMemo, useState } from 'react';
import { Activity, BrainCircuit, DatabaseZap, History, Shield, TerminalSquare } from 'lucide-react';
import { useDashboardSnapshot } from '../hooks/useDashboardSnapshot';
import { useDashboardSocket } from '../hooks/useDashboardSocket';
import { postControlAction } from '../services/api';
import type { DashboardSnapshot, WorkspaceKey } from '../types/dashboard';
import { zh } from '../utils/i18n';
import { OverviewPage } from '../pages/OverviewPage';
import { AlphaBrainPage } from '../pages/AlphaBrainPage';
import { EvolutionPage } from '../pages/EvolutionPage';
import { RiskMatrixPage } from '../pages/RiskMatrixPage';
import { DataFusionPage } from '../pages/DataFusionPage';
import { ExecutionAuditPage } from '../pages/ExecutionAuditPage';
import { formatBeijingTime } from '../utils/i18n';

const navigation: Array<{ key: WorkspaceKey; label: string; icon: typeof Activity }> = [
  { key: 'overview', label: '总览', icon: Activity },
  { key: 'alpha-brain', label: '阿尔法大脑', icon: BrainCircuit },
  { key: 'evolution', label: '进化', icon: History },
  { key: 'risk-matrix', label: '风险矩阵', icon: Shield },
  { key: 'data-fusion', label: '数据融合', icon: DatabaseZap },
  { key: 'execution-audit', label: '执行与审计', icon: TerminalSquare },
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

  const actionLabelMap: Record<string, string> = {
    reset_circuit: '重置熔断',
    trigger_circuit_test: '触发熔断测试',
    stop: '停止交易引擎',
    rollback_evolution: '回滚进化',
  };

  async function handleControl(action: string) {
    const result = await postControlAction(action);
    setActionFeedback(`${actionLabelMap[action] ?? action}: ${result.message}`);
    window.setTimeout(() => setActionFeedback(''), 4000);
  }

  function renderWorkspace() {
    if (!snapshot) {
      return <div className="dcc-empty">正在等待仪表盘快照...</div>;
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
        return <div className="dcc-empty">未知工作区</div>;
    }
  }

  return (
    <div className="dcc-shell">
      <aside className="dcc-sidebar">
        <div className="dcc-brand">
          <div className="dcc-brand__kicker">AI 量化交易</div>
          <h1>控制中心</h1>
          <p>阿尔法大脑 · 进化 · 风险矩阵 · 数据融合</p>
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
            <div className="dcc-topbar__kicker">工作区</div>
            <h2>{activeLabel}</h2>
          </div>
          <div className="dcc-health-strip">
            <span className={`dcc-pill ${connected ? 'is-good' : 'is-risk'}`}>{connected ? '仪表盘 WS 已连接' : '仪表盘 WS 已断开'}</span>
            <span className="dcc-pill">模式 {zh(overview?.mode, '未知')}</span>
            <span className="dcc-pill">交易所 {zh(overview?.exchange, '未知')}</span>
            <span className="dcc-pill">市场状态 {zh(overview?.dominant_regime, '未知')}</span>
            <span className={`dcc-pill ${overview?.risk_level === 'critical' ? 'is-risk' : 'is-info'}`}>风险 {zh(overview?.risk_level, '未知')}</span>
            <span className={`dcc-pill ${overview?.feed_health?.health === 'healthy' ? 'is-good' : overview?.feed_health?.health === 'degraded' ? 'is-risk' : 'is-info'}`}>
              数据源 {zh(overview?.feed_health?.health, '未知')}
            </span>
            {(snapshot?.overview.alerts?.length ?? 0) > 0 && (
              <span
                className="dcc-alert-badge"
                title={snapshot!.overview.alerts!.map((item) => item.message).join('\n')}
              >
                ⚠ {snapshot!.overview.alerts!.length} 条告警
              </span>
            )}
          </div>
        </header>

        <div className="dcc-content-wrap">
          <section className="dcc-content">
            {loading && !snapshot ? <div className="dcc-empty">正在加载仪表盘快照...</div> : null}
            {error ? <div className="dcc-error">快照加载失败：{error}</div> : null}
            {renderWorkspace()}
          </section>

          <aside className="dcc-context-panel">
            <section className="dcc-context-card">
              <div className="dcc-context-card__kicker">操作控制</div>
              <div className="dcc-control-stack">
                <button type="button" onClick={() => handleControl('reset_circuit')}>重置熔断</button>
                <button type="button" onClick={() => handleControl('trigger_circuit_test')}>触发熔断测试</button>
                <button type="button" onClick={() => handleControl('stop')}>停止交易引擎</button>
              </div>
              {actionFeedback ? <p className="dcc-feedback">{actionFeedback}</p> : null}
            </section>

            <section className="dcc-context-card">
              <div className="dcc-context-card__kicker">AI 洞察面板</div>
              <p className="dcc-paragraph">{snapshot?.alpha_brain.ai_analysis ?? 'AI 解读状态同步中...'}</p>
            </section>

            {(snapshot?.overview.alerts?.length ?? 0) > 0 && (
              <section className="dcc-context-card">
                <div className="dcc-context-card__kicker">当前告警</div>
                <ul className="dcc-list" style={{ paddingLeft: 0, listStyle: 'none' }}>
                  {snapshot!.overview.alerts!.map((alert, index) => (
                    <li key={index} style={{ padding: '6px 0', borderBottom: '1px solid rgba(90,118,153,0.12)', color: 'var(--dcc-risk)', fontSize: '13px' }}>
                      ⚠ [{alert.severity}] {zh(alert.message, alert.message)}
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section className="dcc-context-card">
              <div className="dcc-context-card__kicker">最新快照</div>
              <dl className="dcc-definition-list">
                <div><dt>生成时间</dt><dd title={snapshot?.generated_at ?? ''}>{formatBeijingTime(snapshot?.generated_at)}</dd></div>
                <div><dt>状态</dt><dd>{zh(overview?.status, '未知')}</dd></div>
                <div><dt>权益</dt><dd>{overview?.equity?.toFixed(2) ?? '0.00'}</dd></div>
                <div><dt>重连次数</dt><dd>{overview?.feed_health?.reconnect_count ?? 0}</dd></div>
              </dl>
            </section>
          </aside>
        </div>
      </main>
    </div>
  );
}