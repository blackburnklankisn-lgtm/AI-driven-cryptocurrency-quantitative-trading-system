import { Activity, AlertTriangle, BrainCircuit, DatabaseZap, ShieldAlert, TerminalSquare } from 'lucide-react';
import { MetricCard } from '../components/cards/MetricCard';
import { SectionPanel } from '../components/layout/SectionPanel';
import { useDiagnosticsSnapshot } from '../hooks/useDiagnosticsSnapshot';
import { useTransportDiagnostics } from '../hooks/useTransportDiagnostics';
import { zh } from '../utils/i18n';

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function asString(value: unknown, fallback = '未知'): string {
  return typeof value === 'string' ? value : fallback;
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function StatusBadge({ value }: { value: unknown }) {
  const status = String(value ?? 'unknown').toLowerCase();
  const label = zh(value, '未知');
  if (status.includes('healthy') || status.includes('fresh') || status.includes('open') || status.includes('running')) {
    return <span className="dcc-badge dcc-badge--fresh">{label}</span>;
  }
  if (status.includes('critical') || status.includes('error') || status.includes('fail') || status.includes('stale') || status.includes('closed')) {
    return <span className="dcc-badge dcc-badge--stale">{label}</span>;
  }
  return <span className="dcc-badge dcc-badge--partial">{label}</span>;
}

export function DiagnosticsPage() {
  const { snapshot, loading, error, connected } = useDiagnosticsSnapshot();
  const { wsDiagnostics, httpDiagnostics, pollingDiagnostics } = useTransportDiagnostics();

  const transport = asRecord(snapshot?.transport);
  const system = asRecord(snapshot?.system);
  const workspaceHealth = Object.entries(asRecord(snapshot?.workspace_health));
  const backendChannels = Object.entries(asRecord(transport.channels));
  const frontendWsChannels = Object.entries(wsDiagnostics);
  const frontendHttpRoutes = Object.entries(httpDiagnostics);
  const frontendPollers = Object.entries(pollingDiagnostics);
  const alerts = asArray<Record<string, unknown>>(snapshot?.alerts);
  const recentErrors = asArray<Record<string, unknown>>(snapshot?.recent_errors);
  const uptimeSec = asNumber(system.uptime_sec);
  const activeBackendChannels = backendChannels.filter(([, value]) => asNumber(asRecord(value).active_connections) > 0).length;
  const openFrontendChannels = frontendWsChannels.filter(([, value]) => value.status === 'open').length;
  const staleWorkspaces = workspaceHealth.filter(([, value]) => {
    const status = asString(asRecord(value).status, 'unknown').toLowerCase();
    return status.includes('warning') || status.includes('critical') || status.includes('stale') || status.includes('degraded');
  }).length;

  return (
    <div className="dcc-page-grid">
      <div className="dcc-metric-grid">
        <MetricCard
          label="整体诊断状态"
          value={zh(snapshot?.status, '未知')}
          accent={snapshot?.status === 'critical' ? 'risk' : snapshot?.status === 'healthy' ? 'bull' : 'info'}
          subtitle={`诊断 WS ${connected ? '已连接' : '未连接'} · 生成时间 ${snapshot?.generated_at ?? '暂无'}`}
          icon={<Activity size={18} />}
        />
        <MetricCard
          label="活动告警"
          value={String(alerts.length)}
          accent={alerts.some((item) => asString(item.severity, '').toLowerCase() === 'critical') ? 'risk' : 'info'}
          subtitle={`最近错误 ${recentErrors.length} 条`}
          icon={<AlertTriangle size={18} />}
        />
        <MetricCard
          label="传输通道"
          value={`${openFrontendChannels}/${Math.max(frontendWsChannels.length, 1)}`}
          accent={openFrontendChannels > 0 ? 'bull' : 'risk'}
          subtitle={`后端活动通道 ${activeBackendChannels} 个`}
          icon={<DatabaseZap size={18} />}
        />
        <MetricCard
          label="工作区异常"
          value={String(staleWorkspaces)}
          accent={staleWorkspaces > 0 ? 'risk' : 'bull'}
          subtitle={`总计 ${workspaceHealth.length} 个工作区`}
          icon={<ShieldAlert size={18} />}
        />
        <MetricCard
          label="后端运行时长"
          value={`${Math.floor(uptimeSec)}s`}
          accent="neutral"
          subtitle={`API 版本 ${asString(system.api_version, '未知')}`}
          icon={<TerminalSquare size={18} />}
        />
      </div>

      {loading && !snapshot ? <div className="dcc-empty">正在加载统一诊断快照...</div> : null}
      {error ? <div className="dcc-error">诊断快照加载失败：{error}</div> : null}

      <SectionPanel title="前端传输诊断" kicker="共享 WS / HTTP 封装遥测">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">WebSocket 通道</h3>
            {frontendWsChannels.length ? (
              <table className="dcc-table">
                <thead>
                  <tr><th>路径</th><th>状态</th><th>消息数</th><th>重连数</th><th>最近消息</th></tr>
                </thead>
                <tbody>
                  {frontendWsChannels.map(([path, info]) => (
                    <tr key={path}>
                      <td>{path}</td>
                      <td><StatusBadge value={info.status} /></td>
                      <td>{info.message_count}</td>
                      <td>{info.reconnect_count}</td>
                      <td>{info.last_message_at ?? '暂无'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <p className="dcc-paragraph">当前会话尚未记录前端 WebSocket 遥测。</p>}
          </div>
          <div>
            <h3 className="dcc-subtitle">HTTP 路由</h3>
            {frontendHttpRoutes.length ? (
              <table className="dcc-table">
                <thead>
                  <tr><th>路径</th><th>成功/失败</th><th>最近 Base</th><th>最近耗时(ms)</th><th>最近错误</th></tr>
                </thead>
                <tbody>
                  {frontendHttpRoutes.map(([path, info]) => (
                    <tr key={path}>
                      <td>{path}</td>
                      <td>{info.success_count}/{info.failure_count}</td>
                      <td>{info.last_base ?? info.preferred_base}</td>
                      <td>{info.last_latency_ms ?? '暂无'}</td>
                      <td>{info.last_error ?? '无'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <p className="dcc-paragraph">当前会话尚未记录 HTTP 遥测。</p>}
          </div>
        </div>
        <div>
          <h3 className="dcc-subtitle">轮询 Hook</h3>
          {frontendPollers.length ? (
            <table className="dcc-table">
              <thead>
                <tr><th>轮询</th><th>状态</th><th>周期(ms)</th><th>成功/失败</th><th>最近完成</th><th>最近错误</th></tr>
              </thead>
              <tbody>
                {frontendPollers.map(([key, info]) => (
                  <tr key={key}>
                    <td>{info.label}</td>
                    <td><StatusBadge value={info.status} /></td>
                    <td>{info.refresh_interval_ms ?? '暂无'}</td>
                    <td>{info.success_count}/{info.failure_count}</td>
                    <td>{info.last_completed_at ?? '暂无'}</td>
                    <td>{info.last_error ?? '无'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p className="dcc-paragraph">当前会话尚未记录轮询诊断。</p>}
        </div>
      </SectionPanel>

      <SectionPanel title="工作区健康矩阵" kicker="六个业务工作区统一摘要">
        {workspaceHealth.length ? (
          <table className="dcc-table">
            <thead>
              <tr><th>工作区</th><th>状态</th><th>异常数</th><th>最近生成</th><th>摘要</th></tr>
            </thead>
            <tbody>
              {workspaceHealth.map(([key, value]) => {
                const info = asRecord(value);
                return (
                  <tr key={key}>
                    <td>{zh(key, key)}</td>
                    <td><StatusBadge value={info.status} /></td>
                    <td>{asNumber(info.alert_count)}</td>
                    <td>{asString(info.generated_at, '暂无')}</td>
                    <td>{asString(info.detail, '暂无')}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : <p className="dcc-paragraph">暂无工作区健康摘要。</p>}
      </SectionPanel>

      <SectionPanel title="后端通道诊断" kicker="API 推送与广播侧状态">
        {backendChannels.length ? (
          <table className="dcc-table">
            <thead>
              <tr><th>通道</th><th>路径</th><th>连接数</th><th>广播次数</th><th>最近广播</th><th>最近错误</th></tr>
            </thead>
            <tbody>
              {backendChannels.map(([key, value]) => {
                const info = asRecord(value);
                return (
                  <tr key={key}>
                    <td>{asString(info.label, key)}</td>
                    <td>{asString(info.path, '')}</td>
                    <td>{asNumber(info.active_connections)}</td>
                    <td>{asNumber(info.broadcast_count)}</td>
                    <td>{asString(info.last_broadcast_at, '暂无')}</td>
                    <td>{asString(info.last_error, '无')}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : <p className="dcc-paragraph">暂无后端通道诊断数据。</p>}
      </SectionPanel>

      <SectionPanel title="告警与近期错误" kicker="统一故障归因">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">当前告警</h3>
            <ul className="dcc-list">
              {alerts.length ? alerts.map((item, index) => (
                <li key={`${asString(item.code, 'alert')}-${index}`}>
                  [{asString(item.severity, 'info').toUpperCase()}] {asString(item.message, '未知告警')}
                </li>
              )) : <li>暂无当前告警</li>}
            </ul>
          </div>
          <div>
            <h3 className="dcc-subtitle">近期错误</h3>
            <ul className="dcc-list">
              {recentErrors.length ? recentErrors.map((item, index) => (
                <li key={`${asString(item.source, 'error')}-${index}`}>
                  [{asString(item.severity, 'warning').toUpperCase()}] {asString(item.source, 'unknown')} · {asString(item.message, '未知错误')}
                </li>
              )) : <li>暂无近期错误</li>}
            </ul>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="系统与运行时" kicker="统一诊断快照">
        <pre className="dcc-pre">{JSON.stringify(snapshot?.system ?? {}, null, 2)}</pre>
      </SectionPanel>

      <SectionPanel title="Alpha / 风控 / 数据" kicker="核心运行域诊断">
        <div className="dcc-three-col">
          <div>
            <h3 className="dcc-subtitle">Alpha Brain</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot?.alpha_brain_diag ?? {}, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">风险矩阵</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot?.risk_diag ?? {}, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">数据源</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot?.data_sources ?? {}, null, 2)}</pre>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="执行 / 进化 / Phase 3" kicker="扩展运行域诊断">
        <div className="dcc-three-col">
          <div>
            <h3 className="dcc-subtitle">执行链路</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot?.execution_diag ?? {}, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">进化系统</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot?.evolution_diag ?? {}, null, 2)}</pre>
          </div>
          <div>
            <h3 className="dcc-subtitle">Phase 3</h3>
            <pre className="dcc-pre dcc-pre--compact">{JSON.stringify(snapshot?.phase3_diag ?? {}, null, 2)}</pre>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="诊断面板说明" kicker="当前实施阶段">
        <div className="dcc-two-col">
          <div>
            <MetricCard
              label="后端统一诊断"
              value="已接入"
              accent="bull"
              subtitle="统一 diagnostics snapshot + diagnostics ws"
              icon={<BrainCircuit size={18} />}
            />
          </div>
          <div>
            <MetricCard
              label="前端传输遥测"
              value="已接入"
              accent="info"
              subtitle="共享 WS / HTTP / Polling 诊断已统一接入"
              icon={<DatabaseZap size={18} />}
            />
          </div>
        </div>
      </SectionPanel>
    </div>
  );
}