import { useRef, useEffect } from 'react';
import { zh } from '../utils/i18n';
import { useExecutionStream } from '../hooks/useExecutionStream';
import { useAuditLogStream } from '../hooks/useAuditLogStream';
import { SectionPanel } from '../components/layout/SectionPanel';
import type { ExecutionSnapshot } from '../types/dashboard';

interface ExecutionAuditPageProps {
  snapshot: ExecutionSnapshot;
}

export function ExecutionAuditPage({ snapshot }: ExecutionAuditPageProps) {
  const { liveData, connected: execConnected } = useExecutionStream();
  const { logs, connected: logsConnected } = useAuditLogStream();
  const data = liveData ?? snapshot;
  const logEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    });
    return () => cancelAnimationFrame(frame);
  }, [logs]);

  return (
    <div className="dcc-page-grid">
      <SectionPanel title="模拟盘执行汇总" kicker={execConnected ? 'execution ws 实时连接' : '执行与审计工作区'}>
        <pre className="dcc-pre">{JSON.stringify(data.paper_summary, null, 2)}</pre>
      </SectionPanel>

      <SectionPanel title="订单与成交" kicker="执行链路">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">未完成订单 ({data.open_orders.length})</h3>
            {data.open_orders.length > 0 ? (
              <table className="dcc-table">
                <thead><tr><th>ID</th><th>标的</th><th>方向</th><th>数量</th><th>价格</th></tr></thead>
                <tbody>
                  {data.open_orders.map((order, index) => (
                    <tr key={String(order.order_id ?? `order-${index}`)}>
                      <td>{String(order.order_id ?? '暂无')}</td>
                      <td>{String(order.symbol ?? '暂无')}</td>
                      <td>{zh(order.side, '暂无')}</td>
                      <td>{String(order.amount ?? '暂无')}</td>
                      <td>{String(order.price ?? '市价')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <p className="dcc-paragraph">暂无未完成订单</p>}
          </div>
          <div>
            <h3 className="dcc-subtitle">最近成交 ({data.recent_fills.length})</h3>
            {data.recent_fills.length > 0 ? (
              <table className="dcc-table">
                <thead><tr><th>标的</th><th>方向</th><th>数量</th><th>价格</th></tr></thead>
                <tbody>
                  {data.recent_fills.map((fill, index) => (
                    <tr key={`fill-${index}`}>
                      <td>{String(fill.symbol ?? '暂无')}</td>
                      <td>{zh(fill.side, '暂无')}</td>
                      <td>{String(fill.amount ?? '暂无')}</td>
                      <td>{String(fill.price ?? '暂无')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <p className="dcc-paragraph">暂无最近成交</p>}
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="仓位敞口" kicker="当前持仓">
        <table className="dcc-table">
          <thead><tr><th>标的</th><th>数量</th><th>开仓价</th><th>最新价</th><th>未实现盈亏</th><th>名义价值</th></tr></thead>
          <tbody>
            {data.positions.items.length > 0 ? data.positions.items.map((pos) => (
              <tr key={pos.symbol}>
                <td>{pos.symbol}</td>
                <td>{pos.quantity}</td>
                <td>{pos.entry_price == null ? '暂无' : pos.entry_price.toFixed(2)}</td>
                <td>{pos.last_price.toFixed(2)}</td>
                <td className={pos.unrealized_pnl != null && pos.unrealized_pnl < 0 ? 'dcc-log-line--error' : ''}>
                  {pos.unrealized_pnl == null ? '暂无' : pos.unrealized_pnl.toFixed(2)}
                </td>
                <td>{pos.notional.toFixed(2)}</td>
              </tr>
            )) : <tr><td colSpan={6}>暂无持仓</td></tr>}
          </tbody>
        </table>
      </SectionPanel>

      <SectionPanel title="控制动作" kicker="后端暴露的可操作项">
        <table className="dcc-table">
          <thead><tr><th>动作</th><th>是否启用</th></tr></thead>
          <tbody>
            {data.control_actions.length > 0 ? data.control_actions.map((action, index) => (
              <tr key={`${String(action.action ?? 'action')}-${index}`}>
                <td>{zh(action.action, '未知')}</td>
                <td>
                  <span className={`dcc-badge ${action.enabled ? 'dcc-badge--fresh' : 'dcc-badge--stale'}`}>
                    {action.enabled ? '启用' : '禁用'}
                  </span>
                </td>
              </tr>
            )) : <tr><td colSpan={2}>暂无可用控制动作</td></tr>}
          </tbody>
        </table>
      </SectionPanel>

      <SectionPanel title="审计日志流" kicker={logsConnected ? 'ws/logs 已连接' : 'ws/logs 重连中...'}>
        <div className="dcc-log-stream">
          {logs.length === 0 && <span className="dcc-log-line dcc-log-line--muted">正在等待日志事件...</span>}
          {logs.map((line, index) => (
            <div
              key={index}
              className={`dcc-log-line ${line.includes('ERROR') ? 'dcc-log-line--error' : line.includes('WARNING') ? 'dcc-log-line--warning' : ''}`}
            >
              {line}
            </div>
          ))}
          <div ref={logEndRef} />
        </div>
      </SectionPanel>
    </div>
  );
}