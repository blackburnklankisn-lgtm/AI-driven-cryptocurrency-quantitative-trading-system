import { useEffect, useState } from 'react';
import { postControlAction, getEvolutionReports } from '../services/api';
import { SectionPanel } from '../components/layout/SectionPanel';
import type { EvolutionReport, EvolutionSnapshot } from '../types/dashboard';
import { zh } from '../utils/i18n';

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
    setRollbackFeedback(`回滚结果：${result.message}`);
    window.setTimeout(() => setRollbackFeedback(''), 4000);
  }

  return (
    <div className="dcc-page-grid">
      <SectionPanel title="候选生命周期" kicker="进化工作区">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">状态统计</h3>
            <ul className="dcc-list">
              {counts.length ? counts.map(([key, value]) => <li key={key}>{zh(key)}: {value}</li>) : <li>未找到候选项</li>}
            </ul>
          </div>
          <div>
            <h3 className="dcc-subtitle">活跃候选项</h3>
            <ul className="dcc-list">
              {snapshot.active_candidates.length ? snapshot.active_candidates.map((item) => <li key={String(item.candidate_id)}>{String(item.candidate_id)} · {zh(item.owner, '未知')}</li>) : <li>暂无活跃候选项</li>}
            </ul>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="晋升 / 退役 / 回滚时间线" kicker="自进化历史">
        <div className="dcc-three-col">
          <div>
            <h3 className="dcc-subtitle">最近晋升</h3>
            <ul className="dcc-list">
              {snapshot.latest_promotions.length ? snapshot.latest_promotions.map((item, index) => (
                <li key={`promo-${index}`}>
                  <span className="dcc-badge dcc-badge--fresh">已晋升</span>{' '}
                  {String(item.candidate_id ?? JSON.stringify(item))}
                </li>
              )) : <li>暂无晋升记录</li>}
            </ul>
          </div>
          <div>
            <h3 className="dcc-subtitle">最近退役</h3>
            <ul className="dcc-list">
              {snapshot.latest_retirements.length ? snapshot.latest_retirements.map((item, index) => (
                <li key={`ret-${index}`}>
                  <span className="dcc-badge dcc-badge--stale">已退役</span>{' '}
                  {String(item.candidate_id ?? JSON.stringify(item))}
                </li>
              )) : <li>暂无退役记录</li>}
            </ul>
          </div>
          <div>
            <h3 className="dcc-subtitle">最近回滚</h3>
            <ul className="dcc-list">
              {snapshot.latest_rollbacks.length ? snapshot.latest_rollbacks.map((item, index) => (
                <li key={`rb-${index}`}>
                  <span className="dcc-badge dcc-badge--partial">已回滚</span>{' '}
                  {String(item.candidate_id ?? JSON.stringify(item))}
                </li>
              )) : <li>暂无回滚记录</li>}
            </ul>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel
        title="手动回滚"
        kicker="人工操作"
        actions={
          <button type="button" className="dcc-action-btn" onClick={handleRollback}>
            回滚进化
          </button>
        }
      >
        {rollbackFeedback
          ? <p className="dcc-feedback">{rollbackFeedback}</p>
          : <p className="dcc-paragraph">手动回滚最近一次进化步骤。适用于新晋升策略表现不佳时。</p>}
      </SectionPanel>

      <SectionPanel title="进化报告" kicker="历史运行">
        {reports.length > 0 ? (
          <table className="dcc-table">
            <thead>
              <tr><th>报告 ID</th><th>候选项</th><th>结果</th><th>创建时间</th></tr>
            </thead>
            <tbody>
              {reports.map((r, index) => (
                <tr key={r.report_id ?? `report-${index}`}>
                  <td>{r.report_id ?? '暂无'}</td>
                  <td>{r.candidate_id ?? '暂无'}</td>
                  <td>{r.result ?? '暂无'}</td>
                  <td>{r.created_at ?? '暂无'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="dcc-paragraph">暂无进化报告。</p>}
      </SectionPanel>

      <SectionPanel title="A/B 实验" kicker="活跃实验与增益">
        {Object.keys(snapshot.ab_experiments ?? {}).length > 0 ? (
          <table className="dcc-table">
            <thead>
              <tr><th>实验</th><th>对照组</th><th>实验组</th><th>增益</th><th>状态</th></tr>
            </thead>
            <tbody>
              {Object.entries(snapshot.ab_experiments).map(([expId, exp]) => {
                const e = exp as Record<string, unknown>;
                const lift = Number(e.lift ?? 0);
                return (
                  <tr key={expId}>
                    <td>{expId}</td>
                    <td>{String(e.control ?? '暂无')}</td>
                    <td>{String(e.treatment ?? '暂无')}</td>
                    <td style={{ color: lift >= 0 ? 'var(--dcc-bull)' : 'var(--dcc-risk)' }}>
                      {lift >= 0 ? '+' : ''}{lift.toFixed(2)}%
                    </td>
                    <td>
                      <span className={`dcc-badge ${String(e.status ?? '').includes('active') ? 'dcc-badge--fresh' : 'dcc-badge--partial'}`}>
                        {zh(e.status, '未知')}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : <p className="dcc-paragraph">当前无活跃 A/B 实验。候选项进入影子阶段后将由自进化引擎自动创建实验。</p>}
      </SectionPanel>

      <SectionPanel title="每周参数优化器" kicker="优化编排">
        <pre className="dcc-pre">{JSON.stringify(snapshot.weekly_params_optimizer, null, 2)}</pre>
      </SectionPanel>
    </div>
  );
}