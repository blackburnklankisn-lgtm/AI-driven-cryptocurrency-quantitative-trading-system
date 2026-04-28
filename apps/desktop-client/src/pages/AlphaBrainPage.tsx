import { BrainCircuit, Info, ShieldCheck } from 'lucide-react';
import { MetricCard } from '../components/cards/MetricCard';
import { SectionPanel } from '../components/layout/SectionPanel';
import type { AlphaBrainSnapshot } from '../types/dashboard';
import { zh } from '../utils/i18n';

interface AlphaBrainPageProps {
  snapshot: AlphaBrainSnapshot;
}

/** 行内小字说明，消除字面歧义 */
function Footnote({ children }: { children: React.ReactNode }) {
  return (
    <p style={{ fontSize: '11px', color: 'var(--color-text-muted, #888)', marginTop: '6px', lineHeight: 1.5 }}>
      <Info size={10} style={{ display: 'inline', marginRight: '3px', verticalAlign: 'middle' }} />
      {children}
    </p>
  );
}

export function AlphaBrainPage({ snapshot }: AlphaBrainPageProps) {
  const probs = snapshot.regime_probs;
  const weights = Object.entries(snapshot.orchestrator.weights ?? {});

  return (
    <div className="dcc-page-grid">

      {/* ── 顶部概要卡片 ── */}
      <div className="dcc-metric-grid">
        <MetricCard
          label="主导市场状态"
          value={zh(snapshot.dominant_regime, '未知')}
          accent={snapshot.dominant_regime === 'bear' ? 'bear' : 'bull'}
          subtitle={`状态置信度 ${(snapshot.confidence * 100).toFixed(1)}%（= 第1类概率 − 第2类概率）`}
          icon={<BrainCircuit size={18} />}
        />
        <MetricCard
          label="门控动作"
          value={zh(snapshot.orchestrator.gating_action, '未知')}
          accent={snapshot.orchestrator.gating_action.includes('block') ? 'risk' : 'info'}
          subtitle={`最近5根K线状态一致：${snapshot.is_regime_stable ? '是（状态稳定）' : '否（状态切换中）'}`}
          icon={<ShieldCheck size={18} />}
        />
      </div>

      {/* ── 概率分布 ── */}
      <SectionPanel title="市场状态概率分布" kicker="阿尔法大脑 · 基于过去14~20根1h K线的滚动指标">
        <div className="dcc-prob-grid">
          <div className="dcc-prob-item"><span>牛市</span><strong>{(probs.bull * 100).toFixed(1)}%</strong></div>
          <div className="dcc-prob-item"><span>熊市</span><strong>{(probs.bear * 100).toFixed(1)}%</strong></div>
          <div className="dcc-prob-item"><span>震荡</span><strong>{(probs.sideways * 100).toFixed(1)}%</strong></div>
          <div className="dcc-prob-item"><span>高波动</span><strong>{(probs.high_vol * 100).toFixed(1)}%</strong></div>
        </div>
        <Footnote>
          概率 = softmax 归一化 + 5% 最低保底。当高波动概率约 87% 时，其余三类各约 4.3% 为数学下限，不是"轻微看涨/看跌"信号。
          高波动判断基于 ATR%（每根K线内部振幅均值）/ 布林带宽度 / 20期收益率标准差，与今日全天价格区间无直接关系。
        </Footnote>
      </SectionPanel>

      {/* ── 编排器 ── */}
      <SectionPanel title="编排器" kicker={`门控规则 · 当前动作: ${zh(snapshot.orchestrator.gating_action, '未知')}`}>
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">
              策略环境亲和度权重
              <span style={{ fontWeight: 'normal', fontSize: '11px', marginLeft: '6px', color: 'var(--color-text-muted, #888)' }}>
                （非盈利贡献，非胜率）
              </span>
            </h3>
            <ul className="dcc-list">
              {weights.length
                ? weights.map(([key, value]) => (
                    <li key={key}>{zh(key)}: {(value * 100).toFixed(1)}%</li>
                  ))
                : <li>暂无权重数据（本轮无策略信号，或信号已被阻断）</li>}
            </ul>
            <Footnote>
              权重 = 当前 regime 下该策略亲和度的线性映射后归一化。
              55% vs 45% 表示"在当前市场状态下哪个策略更匹配"，与历史盈利无关。
            </Footnote>
          </div>
          <div>
            <h3 className="dcc-subtitle">阻断 / 折扣原因</h3>
            <ul className="dcc-list">
              {snapshot.orchestrator.block_reasons.length
                ? snapshot.orchestrator.block_reasons.map((reason) => (
                    <li key={reason}>{zh(reason)}</li>
                  ))
                : <li>本轮无阻断原因</li>}
            </ul>
            <Footnote>
              "权重折扣[信号置信度偏低]"：策略历史上主要发出 HOLD 信号（confidence 字段 = 0.0 占位值），导致均值偏低触发权重折扣，不代表胜率为0或策略无效。
              此处仅覆盖编排层阻断；风控熔断见系统总览页。
            </Footnote>
          </div>
        </div>
      </SectionPanel>

      {/* ── 已选策略结果 ── */}
      {snapshot.orchestrator.selected_results.length > 0 && (
        <SectionPanel title="已选策略结果" kicker="编排器输出 · 通过门控的信号">
          <table className="dcc-table">
            <thead>
              <tr>
                <th>策略</th>
                <th>标的</th>
                <th>动作</th>
                <th>信号强度（非胜率）</th>
              </tr>
            </thead>
            <tbody>
              {snapshot.orchestrator.selected_results.map((item, index) => (
                <tr key={`${item.strategy_id}-${index}`}>
                  <td>{item.strategy_id}</td>
                  <td>{item.symbol}</td>
                  <td>{zh(item.action)}</td>
                  <td>
                    {(item.confidence * 100).toFixed(1)}%
                    {item.action === 'HOLD' && item.confidence === 0 && (
                      <span style={{ fontSize: '10px', color: 'var(--color-text-muted, #888)', marginLeft: '4px' }}>
                        （HOLD占位值）
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Footnote>
            "信号强度"为策略发出信号时的 confidence 字段值，不是预测胜率。规则策略在无持仓时发出 HOLD 信号，confidence 固定为 0.0（占位），非"模型认为胜率0%"。
          </Footnote>
        </SectionPanel>
      )}

      {/* ── 持续学习器 ── */}
      <SectionPanel title="自适应机器学习" kicker="持续学习器 · 需 models/ 目录存在 ML 模型文件">
        <div className="dcc-two-col">
          <div>
            <h3 className="dcc-subtitle">当前模型</h3>
            <dl className="dcc-definition-list">
              <div><dt>学习器数量</dt><dd>{snapshot.continuous_learner.count || '0（未加载模型）'}</dd></div>
              <div><dt>版本</dt><dd>{snapshot.continuous_learner.active_version ?? '无（models/ 目录为空）'}</dd></div>
              <div><dt>模型类型</dt><dd>{zh(snapshot.continuous_learner.model_type, '未知')}</dd></div>
              <div><dt>模型路径</dt><dd>{snapshot.continuous_learner.model_path ?? '无'}</dd></div>
              <div><dt>阈值来源</dt><dd>{snapshot.continuous_learner.threshold_source ?? '默认'}</dd></div>
              <div><dt>最近重训</dt><dd>{snapshot.continuous_learner.last_retrain_at ?? '从未（尚无ML模型）'}</dd></div>
            </dl>
            <Footnote>
              学习器数量为0是正常初始状态，不是程序错误。系统累积足够 K 线数据后（≥500根）会自动触发首次训练。
            </Footnote>
            <h3 className="dcc-subtitle" style={{ marginTop: '18px' }}>决策阈值</h3>
            <ul className="dcc-list">
              {Object.entries(snapshot.continuous_learner.thresholds).length
                ? Object.entries(snapshot.continuous_learner.thresholds).map(([key, value]) => (
                    <li key={key}>{key}: {typeof value === 'number' ? value.toFixed(4) : String(value)}</li>
                  ))
                : <li>暂无阈值数据（模型未加载）</li>}
            </ul>
          </div>
          <div>
            <h3 className="dcc-subtitle">AI 盘面解读</h3>
            <p className="dcc-paragraph">{snapshot.ai_analysis}</p>
            <Footnote>
              AI 解读优先使用 GEMINI_API_KEY，兼容旧的 GOOGLE_API_KEY。未配置时会明确提示缺少 key；已配置但程序刚重启时，会先显示“等待行情数据稳定后自动生成本次启动后的 AI 解读”。
            </Footnote>

            <h3 className="dcc-subtitle" style={{ marginTop: '18px' }}>版本历史</h3>
            <ul className="dcc-list">
              {snapshot.continuous_learner.items.length > 0
                ? snapshot.continuous_learner.items.map((item, index) => (
                    <li key={`${item.id ?? 'learner'}-${index}`}>
                      <strong>{item.id ?? `学习器-${index + 1}`}</strong>:{' '}
                      {item.versions?.length ? item.versions.join(' → ') : (item.active_version ?? '无版本历史')}
                      {item.model_type ? ` · ${item.model_type}` : ''}
                      {item.threshold_source ? ` · 阈值来源 ${item.threshold_source}` : ''}
                    </li>
                  ))
                : <li>暂无版本历史（models/ 目录为空）</li>}
            </ul>
          </div>
        </div>
      </SectionPanel>

    </div>
  );
}