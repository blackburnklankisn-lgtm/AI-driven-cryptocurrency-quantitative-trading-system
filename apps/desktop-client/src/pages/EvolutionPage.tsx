import { useEffect, useMemo, useState } from 'react';
import { postControlAction, getEvolutionReports } from '../services/api';
import { SectionPanel } from '../components/layout/SectionPanel';
import type { EvolutionReport, EvolutionSnapshot } from '../types/dashboard';
import { zh } from '../utils/i18n';

interface EvolutionPageProps {
  snapshot: EvolutionSnapshot;
}

export function EvolutionPage({ snapshot }: EvolutionPageProps) {
  const asRecord = (value: unknown): Record<string, unknown> =>
    value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};

  const asString = (value: unknown, fallback = ''): string =>
    typeof value === 'string' ? value : fallback;

  const counts = Object.entries(snapshot.candidate_counts_by_status ?? {});
  const activeExperiments = snapshot.ab_experiments.active ?? [];
  const completedExperiments = snapshot.ab_experiments.completed ?? [];
  const weeklyState = snapshot.weekly_params_optimizer.state ?? {};
  const weeklyTargets = snapshot.weekly_params_optimizer.targets ?? [];
  const weeklyRuns = snapshot.weekly_params_optimizer.runs ?? [];
  const allCandidates = snapshot.candidates ?? [];
  const [reports, setReports] = useState<EvolutionReport[]>([]);
  const [rollbackFeedback, setRollbackFeedback] = useState('');
  const [optimizerFeedback, setOptimizerFeedback] = useState('');
  const [selectedFamilyKey, setSelectedFamilyKey] = useState('');
  const [selectedCurrentCandidateId, setSelectedCurrentCandidateId] = useState('');
  const [selectedRollbackToCandidateId, setSelectedRollbackToCandidateId] = useState('');

  const formatRollbackFeedback = (result: { result: string; message: string; error_code?: string }): string => {
    if (result.result === 'ok') {
      return `回滚成功：${result.message}`;
    }

    const code = asString(result.error_code, 'ROLLBACK_FAILED');
    const categoryMap: Record<string, string> = {
      CANDIDATE_NOT_FOUND: '候选不存在',
      ROLLBACK_TARGET_NOT_FOUND: '候选不存在',
      FAMILY_MISMATCH: 'Family 不匹配',
      ROLLBACK_TARGET_ACTIVE: '目标已是 Active',
      CURRENT_NOT_ACTIVE: '当前候选非 Active',
      FAMILY_NO_ACTIVE_CANDIDATE: 'Family 无 Active 候选',
      NO_ACTIVE_CANDIDATE: '无 Active 候选',
      NO_ROLLBACK_TARGET: '无可回滚目标',
      INVALID_ROLLBACK_TARGET: '回滚目标非法',
      ROLLBACK_UNAVAILABLE: '回滚能力不可用',
      ROLLBACK_FAILED: '回滚执行失败',
    };
    const category = categoryMap[code] || '未知回滚错误';
    return `回滚失败【${category}】${result.message}（错误码: ${code}）`;
  };

  const activeCandidateRecords = useMemo(
    () => (snapshot.active_candidates ?? []).map(asRecord),
    [snapshot.active_candidates],
  );

  const familyOptions = useMemo(() => {
    const familySet = new Set<string>();
    activeCandidateRecords.forEach((candidate) => {
      const family = asString(candidate.family_key) || asString(candidate.owner);
      if (family) {
        familySet.add(family);
      }
    });
    return Array.from(familySet).sort();
  }, [activeCandidateRecords]);

  const currentCandidateOptions = useMemo(() => {
    return activeCandidateRecords.filter((candidate) => {
      if (!selectedFamilyKey) {
        return true;
      }
      const family = asString(candidate.family_key) || asString(candidate.owner);
      return family === selectedFamilyKey;
    });
  }, [activeCandidateRecords, selectedFamilyKey]);

  const rollbackToOptions = useMemo(() => {
    const selectedCurrent = currentCandidateOptions.find(
      (candidate) => asString(candidate.candidate_id) === selectedCurrentCandidateId,
    );
    const currentFamily =
      asString(selectedCurrent?.family_key) ||
      asString(selectedCurrent?.owner) ||
      selectedFamilyKey;

    return allCandidates
      .map(asRecord)
      .filter((candidate) => {
        const candidateId = asString(candidate.candidate_id);
        if (!candidateId || candidateId === selectedCurrentCandidateId) {
          return false;
        }
        if (asString(candidate.status).toLowerCase() === 'active') {
          return false;
        }
        if (!currentFamily) {
          return true;
        }
        const family = asString(candidate.family_key) || asString(candidate.owner);
        return family === currentFamily;
      });
  }, [allCandidates, currentCandidateOptions, selectedCurrentCandidateId, selectedFamilyKey]);

  const weeklyRunDetails = useMemo(() => {
    return weeklyRuns.flatMap((run, runIndex) => {
      const runRecord = asRecord(run);
      const optimizedSymbols = Array.isArray(runRecord.optimized_symbols)
        ? (runRecord.optimized_symbols as Array<Record<string, unknown>>)
        : [];
      if (!optimizedSymbols.length) {
        return [
          {
            key: `${String(runRecord.slot_id ?? `run-${runIndex}`)}-empty`,
            slotId: asString(runRecord.slot_id, `run-${runIndex}`),
            status: asString(runRecord.status, 'unknown'),
            strategyId: '-',
            symbol: '-',
            rows: 0,
            candidateId: '-',
            modelPath: '-',
            thresholdPath: '-',
          },
        ];
      }

      return optimizedSymbols.map((item, itemIndex) => {
        const detail = asRecord(item);
        return {
          key: `${String(runRecord.slot_id ?? `run-${runIndex}`)}-${itemIndex}`,
          slotId: asString(runRecord.slot_id, `run-${runIndex}`),
          status: asString(runRecord.status, 'unknown'),
          strategyId: asString(detail.strategy_id, '-'),
          symbol: asString(detail.symbol, '-'),
          rows: Number(detail.rows ?? 0),
          candidateId: asString(detail.candidate_id, '-'),
          modelPath: asString(detail.model_path, '-'),
          thresholdPath: asString(detail.threshold_path, '-'),
        };
      });
    });
  }, [weeklyRuns]);

  useEffect(() => {
    getEvolutionReports(200)
      .then(setReports)
      .catch((err: unknown) => console.error('[EvolutionPage] reports fetch failed', err));
  }, []);

  useEffect(() => {
    if (!selectedFamilyKey && familyOptions.length > 0) {
      setSelectedFamilyKey(familyOptions[0]);
    }
  }, [familyOptions, selectedFamilyKey]);

  useEffect(() => {
    if (!selectedCurrentCandidateId && currentCandidateOptions.length > 0) {
      setSelectedCurrentCandidateId(asString(currentCandidateOptions[0].candidate_id));
      return;
    }
    if (
      selectedCurrentCandidateId &&
      !currentCandidateOptions.some((candidate) => asString(candidate.candidate_id) === selectedCurrentCandidateId)
    ) {
      setSelectedCurrentCandidateId(currentCandidateOptions.length > 0 ? asString(currentCandidateOptions[0].candidate_id) : '');
    }
  }, [currentCandidateOptions, selectedCurrentCandidateId]);

  useEffect(() => {
    if (rollbackToOptions.length === 0) {
      setSelectedRollbackToCandidateId('');
      return;
    }
    if (
      !selectedRollbackToCandidateId ||
      !rollbackToOptions.some((candidate) => asString(candidate.candidate_id) === selectedRollbackToCandidateId)
    ) {
      setSelectedRollbackToCandidateId(asString(rollbackToOptions[0].candidate_id));
    }
  }, [rollbackToOptions, selectedRollbackToCandidateId]);

  async function handleRollback() {
    const result = await postControlAction({
      action: 'rollback_evolution',
      family_key: selectedFamilyKey || undefined,
      candidate_id: selectedCurrentCandidateId || undefined,
      rollback_to_candidate_id: selectedRollbackToCandidateId || undefined,
    });
    setRollbackFeedback(formatRollbackFeedback(result));
    window.setTimeout(() => setRollbackFeedback(''), 4000);
    getEvolutionReports(200)
      .then(setReports)
      .catch((err: unknown) => console.error('[EvolutionPage] reports refresh failed', err));
  }

  async function handleWeeklyOptimizerTrigger() {
    const result = await postControlAction('trigger_weekly_optimizer');
    setOptimizerFeedback(`触发结果：${result.message}`);
    window.setTimeout(() => setOptimizerFeedback(''), 5000);
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
        <div className="dcc-two-col" style={{ marginBottom: 12 }}>
          <div>
            <label className="dcc-subtitle" htmlFor="rollback-family-select">Family</label>
            <select
              id="rollback-family-select"
              className="dcc-select"
              value={selectedFamilyKey}
              onChange={(event) => setSelectedFamilyKey(event.target.value)}
            >
              {familyOptions.length ? familyOptions.map((family) => (
                <option key={family} value={family}>{family}</option>
              )) : <option value="">暂无可选 Family</option>}
            </select>
          </div>
          <div>
            <label className="dcc-subtitle" htmlFor="rollback-current-select">当前 active 候选</label>
            <select
              id="rollback-current-select"
              className="dcc-select"
              value={selectedCurrentCandidateId}
              onChange={(event) => setSelectedCurrentCandidateId(event.target.value)}
            >
              {currentCandidateOptions.length ? currentCandidateOptions.map((candidate) => (
                <option key={asString(candidate.candidate_id)} value={asString(candidate.candidate_id)}>
                  {asString(candidate.candidate_id)} ({asString(candidate.strategy_id, 'unknown')})
                </option>
              )) : <option value="">暂无 active 候选</option>}
            </select>
          </div>
        </div>
        <div style={{ marginBottom: 12 }}>
          <label className="dcc-subtitle" htmlFor="rollback-target-select">回滚到候选</label>
          <select
            id="rollback-target-select"
            className="dcc-select"
            value={selectedRollbackToCandidateId}
            onChange={(event) => setSelectedRollbackToCandidateId(event.target.value)}
          >
            {rollbackToOptions.length ? rollbackToOptions.map((candidate) => (
              <option key={asString(candidate.candidate_id)} value={asString(candidate.candidate_id)}>
                {asString(candidate.candidate_id)} [{asString(candidate.status, 'unknown')}]
              </option>
            )) : <option value="">自动选择最近可回滚候选</option>}
          </select>
        </div>
        {rollbackFeedback
          ? <p className="dcc-feedback">{rollbackFeedback}</p>
          : <p className="dcc-paragraph">可按 family 和 candidate 精确指定回滚路径。若不选目标候选，将由后端自动选择同 family 最近可回滚版本。</p>}
      </SectionPanel>

      <SectionPanel title="进化报告" kicker="历史运行">
        {reports.length > 0 ? (
          <table className="dcc-table">
            <thead>
              <tr><th>报告 ID</th><th>评估数</th><th>结果摘要</th><th>完成时间</th></tr>
            </thead>
            <tbody>
              {reports.map((r, index) => (
                <tr key={r.report_id ?? `report-${index}`}>
                  <td>{r.report_id ?? '暂无'}</td>
                  <td>{r.total_candidates ?? 0}</td>
                  <td>
                    晋升 {r.promoted?.length ?? 0} / 降级 {r.demoted?.length ?? 0} / 退役 {r.retired?.length ?? 0} / 回滚 {r.rollbacks?.length ?? 0}
                  </td>
                  <td>{r.period_end ?? '暂无'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="dcc-paragraph">暂无进化报告。</p>}
      </SectionPanel>

      <SectionPanel title="A/B 实验" kicker="活跃实验与增益">
        {activeExperiments.length > 0 || completedExperiments.length > 0 ? (
          <table className="dcc-table">
            <thead>
              <tr><th>实验</th><th>对照组</th><th>实验组</th><th>增益</th><th>状态</th></tr>
            </thead>
            <tbody>
              {[...activeExperiments, ...completedExperiments].map((exp, index) => {
                const e = exp as Record<string, unknown>;
                const lift = Number(e.lift ?? (Number(e.test_pnl ?? 0) - Number(e.control_pnl ?? 0)));
                const status = String(e.status ?? (index < activeExperiments.length ? 'active' : 'completed'));
                return (
                  <tr key={String(e.experiment_id ?? `ab-${index}`)}>
                    <td>{String(e.experiment_id ?? `ab-${index}`)}</td>
                    <td>{String(e.control_id ?? '暂无')}</td>
                    <td>{String(e.test_id ?? '暂无')}</td>
                    <td style={{ color: lift >= 0 ? 'var(--dcc-bull)' : 'var(--dcc-risk)' }}>
                      {lift >= 0 ? '+' : ''}{lift.toFixed(4)}
                    </td>
                    <td>
                      <span className={`dcc-badge ${status.includes('active') ? 'dcc-badge--fresh' : 'dcc-badge--partial'}`}>
                        {zh(status, status === 'completed' ? '已完成' : '进行中')}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : <p className="dcc-paragraph">当前无活跃 A/B 实验。候选项进入影子阶段后将由自进化引擎自动创建实验。</p>}
      </SectionPanel>

      <SectionPanel
        title="每周参数优化器"
        kicker="优化编排"
        actions={
          <button type="button" className="dcc-action-btn" onClick={handleWeeklyOptimizerTrigger}>
            立即触发
          </button>
        }
      >
        {optimizerFeedback ? <p className="dcc-feedback">{optimizerFeedback}</p> : null}
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">当前状态</h3>
            <dl className="dcc-definition-list">
              <div><dt>Cron</dt><dd>{snapshot.weekly_params_optimizer.cron || '未配置'}</dd></div>
              <div><dt>运行中</dt><dd>{snapshot.weekly_params_optimizer.is_running ? '是' : '否'}</dd></div>
              <div><dt>目标数</dt><dd>{snapshot.weekly_params_optimizer.target_count ?? 0}</dd></div>
              <div><dt>状态</dt><dd>{String(weeklyState.status ?? 'idle')}</dd></div>
              <div><dt>最近尝试</dt><dd>{String(weeklyState.last_attempted_at ?? '暂无')}</dd></div>
              <div><dt>最近完成</dt><dd>{String(weeklyState.last_finished_at ?? '暂无')}</dd></div>
            </dl>
          </div>
          <div>
            <h3 className="dcc-subtitle">优化目标</h3>
            {weeklyTargets.length ? (
              <table className="dcc-table">
                <thead>
                  <tr><th>类型</th><th>策略</th><th>标的</th><th>族群</th></tr>
                </thead>
                <tbody>
                  {weeklyTargets.map((target, index) => {
                    const item = asRecord(target);
                    return (
                      <tr key={`target-${index}`}>
                        <td>{asString(item.target_kind, 'unknown')}</td>
                        <td>{asString(item.strategy_id, '-')}</td>
                        <td>{asString(item.symbol, '-')}</td>
                        <td>{asString(item.family_key, '-')}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            ) : <p className="dcc-paragraph">暂无优化目标。</p>}
          </div>
        </div>

        <h3 className="dcc-subtitle" style={{ marginTop: 12 }}>运行明细</h3>
        {weeklyRuns.length ? (
          <table className="dcc-table">
            <thead>
              <tr><th>Slot</th><th>状态</th><th>目标数</th><th>优化数</th><th>错误数</th><th>完成时间</th></tr>
            </thead>
            <tbody>
              {weeklyRuns.map((run, index) => {
                const item = asRecord(run);
                const errors = asRecord(item.errors);
                const optimizedCount = Array.isArray(item.optimized_symbols) ? item.optimized_symbols.length : 0;
                return (
                  <tr key={`weekly-run-${index}`}>
                    <td>{asString(item.slot_id, `run-${index}`)}</td>
                    <td>{asString(item.status, 'unknown')}</td>
                    <td>{Number(item.targets_count ?? 0)}</td>
                    <td>{optimizedCount}</td>
                    <td>{Object.keys(errors).length}</td>
                    <td>{asString(item.finished_at, '-')}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : <p className="dcc-paragraph">暂无运行记录。</p>}

        <h3 className="dcc-subtitle" style={{ marginTop: 12 }}>运行产物明细</h3>
        {weeklyRunDetails.length ? (
          <table className="dcc-table">
            <thead>
              <tr><th>Slot</th><th>状态</th><th>策略</th><th>标的</th><th>训练行数</th><th>候选ID</th><th>模型路径</th><th>阈值路径</th></tr>
            </thead>
            <tbody>
              {weeklyRunDetails.map((row) => (
                <tr key={row.key}>
                  <td>{row.slotId}</td>
                  <td>{row.status}</td>
                  <td>{row.strategyId}</td>
                  <td>{row.symbol}</td>
                  <td>{row.rows}</td>
                  <td>{row.candidateId}</td>
                  <td>{row.modelPath}</td>
                  <td>{row.thresholdPath}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="dcc-paragraph">暂无产物明细。</p>}

        <h3 className="dcc-subtitle" style={{ marginTop: 12 }}>运行错误明细</h3>
        {weeklyRuns.some((run) => Object.keys(asRecord(asRecord(run).errors)).length > 0) ? (
          <table className="dcc-table">
            <thead>
              <tr><th>Slot</th><th>错误键</th><th>错误内容</th></tr>
            </thead>
            <tbody>
              {weeklyRuns.flatMap((run, index) => {
                const runRecord = asRecord(run);
                const errors = asRecord(runRecord.errors);
                return Object.entries(errors).map(([key, value], errIndex) => (
                  <tr key={`weekly-error-${index}-${errIndex}`}>
                    <td>{asString(runRecord.slot_id, `run-${index}`)}</td>
                    <td>{key}</td>
                    <td>{asString(value, String(value))}</td>
                  </tr>
                ));
              })}
            </tbody>
          </table>
        ) : <p className="dcc-paragraph">暂无运行错误。</p>}
      </SectionPanel>
    </div>
  );
}