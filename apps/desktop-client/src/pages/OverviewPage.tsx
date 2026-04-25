import { Activity, ShieldAlert, TrendingDown, TrendingUp } from 'lucide-react';
import { KlineChart } from '../components/charts/KlineChart';
import { MetricCard } from '../components/cards/MetricCard';
import { SectionPanel } from '../components/layout/SectionPanel';
import type { OverviewSnapshot } from '../types/dashboard';
import { zh } from '../utils/i18n';

interface OverviewPageProps {
  snapshot: OverviewSnapshot;
}

export function OverviewPage({ snapshot }: OverviewPageProps) {
  const positions = snapshot.positions_summary?.items ?? [];
  const weights = Object.entries(snapshot.strategy_weight_summary ?? {});

  return (
    <div className="dcc-page-grid">
      <div className="dcc-metric-grid">
        <MetricCard
          label="总权益"
          value={`$${(snapshot.equity ?? 0).toLocaleString('zh-CN', { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`}
          accent="info"
          subtitle={`模式 ${snapshot.mode ?? '未知'} · 交易所 ${snapshot.exchange ?? '未知'}`}
          icon={<TrendingUp size={18} />}
        />
        <MetricCard
          label="当日盈亏"
          value={`${(snapshot.daily_pnl ?? 0) >= 0 ? '+' : ''}${(snapshot.daily_pnl ?? 0).toFixed(2)}`}
          accent={(snapshot.daily_pnl ?? 0) >= 0 ? 'bull' : 'bear'}
          subtitle={`峰值权益 $${(snapshot.peak_equity ?? 0).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`}
          icon={(snapshot.daily_pnl ?? 0) >= 0 ? <TrendingUp size={18} /> : <TrendingDown size={18} />}
        />
        <MetricCard
          label="回撤"
          value={`${((snapshot.drawdown_pct ?? 0) * 100).toFixed(2)}%`}
          accent={(snapshot.drawdown_pct ?? 0) > 0.1 ? 'risk' : 'neutral'}
          subtitle={`峰值 $${(snapshot.peak_equity ?? 0).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`}
          icon={<TrendingDown size={18} />}
        />
        <MetricCard
          label="主导市场状态"
          value={zh(snapshot.dominant_regime, '未知')}
          accent={snapshot.dominant_regime === 'bear' ? 'bear' : 'bull'}
          subtitle={`置信度 ${((snapshot.regime_confidence ?? 0) * 100).toFixed(1)}% · 稳定 ${snapshot.is_regime_stable ? '是' : '否'}`}
          icon={<Activity size={18} />}
        />
        <MetricCard
          label="风险等级"
          value={zh(snapshot.risk_level, '未知')}
          accent={snapshot.risk_level === 'critical' ? 'risk' : 'neutral'}
          subtitle={`数据源 ${zh(snapshot.feed_health?.health, '未知')} · 重连 ${snapshot.feed_health?.reconnect_count ?? 0}`}
          icon={<ShieldAlert size={18} />}
        />
      </div>

      <SectionPanel title="BTC/USDT K线" kicker="每 60 秒自动刷新">
        <KlineChart symbol="BTC/USDT" height={280} />
      </SectionPanel>

      <SectionPanel title="全局态势" kicker="总览工作区">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">告警</h3>
            <ul className="dcc-list">
              {(snapshot.alerts?.length ? snapshot.alerts : ['暂无活动告警']).map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
          <div>
            <h3 className="dcc-subtitle">数据源健康</h3>
            <dl className="dcc-definition-list">
              <div><dt>状态</dt><dd>{zh(snapshot.feed_health?.health, '未知')}</dd></div>
              <div><dt>交易所</dt><dd>{snapshot.feed_health?.exchange ?? '未知'}</dd></div>
              <div><dt>重连次数</dt><dd>{snapshot.feed_health?.reconnect_count ?? 0}</dd></div>
            </dl>
          </div>
        </div>
      </SectionPanel>

      <SectionPanel title="敞口与仓位" kicker="组合视图">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">持仓 ({snapshot.positions_summary?.count ?? 0})</h3>
            <table className="dcc-table">
              <thead>
                <tr><th>标的</th><th>数量</th><th>最新价</th><th>名义价值</th></tr>
              </thead>
              <tbody>
                {positions.length > 0 ? positions.map((item) => (
                  <tr key={item.symbol}>
                    <td>{item.symbol}</td>
                    <td>{item.quantity}</td>
                    <td>{item.last_price.toFixed(2)}</td>
                    <td>{item.notional.toFixed(2)}</td>
                  </tr>
                )) : (
                  <tr><td colSpan={4}>暂无持仓</td></tr>
                )}
              </tbody>
            </table>
          </div>
          <div>
            <h3 className="dcc-subtitle">策略权重汇总</h3>
            <ul className="dcc-list">
              {weights.length > 0 ? weights.map(([key, value]) => (
                <li key={key}>{zh(key)}: {(value * 100).toFixed(1)}%</li>
              )) : <li>暂无编排权重数据</li>}
            </ul>
          </div>
        </div>
      </SectionPanel>
    </div>
  );
}