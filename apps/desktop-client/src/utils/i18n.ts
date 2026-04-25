// ─── 精确词汇表 ──────────────────────────────────────────────
// 复合短语（下划线分隔）优先查找，匹配不到时拆词翻译。
const exactMap: Record<string, string> = {
  // 通用状态
  unknown: '未知',
  none: '无',
  null: '无',
  na: '暂无',
  pending: '待处理',
  running: '运行中',
  stopped: '已停止',
  paused: '已暂停',
  loading: '加载中',
  initializing: '初始化中',
  ready: '就绪',
  waiting: '等待中',
  complete: '已完成',
  completed: '已完成',
  success: '成功',
  warning: '警告',
  info: '信息',

  // 健康状态
  healthy: '健康',
  degraded: '降级',
  stale: '过期',
  fresh: '新鲜',
  connected: '已连接',
  disconnected: '已断开',
  error: '错误',
  fail: '失败',
  failed: '失败',
  ok: '正常',
  unavailable: '不可用',

  // 风险等级
  critical: '严重',
  normal: '正常',
  low: '低',
  medium: '中等',
  high: '高',
  elevated: '偏高',
  minimal: '极低',

  // 市场状态
  bull: '牛市',
  bear: '熊市',
  sideways: '震荡',
  volatile: '高波动',

  // 启用/禁用
  active: '活跃',
  inactive: '不活跃',
  enabled: '启用',
  disabled: '禁用',

  // 交易模式
  paper: '模拟盘',
  live: '实盘',
  sandbox: '沙盒',

  // 买卖方向
  buy: '买入',
  sell: '卖出',
  long: '做多',
  short: '做空',
  hold: '观望',
  market: '市价',
  limit: '限价',

  // 进化阶段
  shadow: '影子',
  candidate: '候选',
  testing: '测试中',
  deployed: '已部署',
  promoted: '已晋升',
  retired: '已退役',
  rollback: '回滚',

  // 门控动作
  allow: '允许',
  block: '阻断',
  partial: '部分',

  // 仓位规模模式
  fixed: '固定仓位',
  dynamic: '动态仓位',
  kelly: 'Kelly仓位',
  fractional: '比例仓位',

  // 风控单词
  triggered: '已触发',
  breached: '已突破',
  exceeded: '已超限',
  loss: '亏损',
  losses: '亏损',
  profit: '利润',
  target: '目标',
  reset: '重置',
  circuit: '熔断',
  breaker: '断路器',
  budget: '预算',
  regime: '市场状态',
  stable: '稳定',
  unstable: '不稳定',
  consecutive: '连续',
  exhausted: '耗尽',
  cooldown: '冷却',
  drawdown: '回撤',
  max: '最大',
  daily: '每日',
  weekly: '每周',
  intraday: '日内',
  overnight: '隔夜',

  // 策略类型单词
  momentum: '动量',
  breakout: '突破',
  arbitrage: '套利',
  trend: '趋势',
  following: '跟踪',
  reversion: '回归',
  mean: '均值',

  // ─── 复合短语（下划线形式）────────────────────────────────────
  // 熔断 / 风控
  circuit_broken: '熔断已触发',
  circuit_break: '熔断触发',
  circuit_breaker: '熔断器',
  circuit_reset: '熔断重置',
  circuit_test: '熔断测试',
  kill_switch: '熔断开关',
  max_drawdown: '最大回撤',
  max_drawdown_exceeded: '最大回撤超限',
  daily_loss_limit: '日亏损限额触发',
  daily_pnl_limit: '日盈亏限额触发',
  loss_limit: '亏损限额触发',
  loss_limit_hit: '亏损限额触发',
  consecutive_losses: '连续亏损超限',
  consecutive_losses_limit: '连续亏损超限',
  risk_budget_exhausted: '风险预算耗尽',
  risk_budget: '风险预算',
  cooldown_active: '冷却期中',
  regime_unstable: '市场状态不稳',
  position_sizing: '仓位规模',
  no_reason: '无原因',

  // 门控
  partial_block: '部分阻断',
  block_all: '全部阻断',
  partial_allow: '部分允许',

  // 进化 / 策略
  mean_reversion: '均值回归',
  trend_following: '趋势跟踪',
  high_vol: '高波动',
  shadow_phase: '影子阶段',
  init_loaded: '初始加载',

  // 控制动作
  reset_circuit: '重置熔断',
  trigger_circuit_test: '触发熔断测试',
  rollback_evolution: '回滚进化',
  emergency_stop: '紧急停止',
  force_close: '强制平仓',
  force_close_all: '强制全部平仓',
  stop: '停止',
};

// 正则短语替换（处理含空格的自然语言）
const phraseMap: Array<[RegExp, string]> = [
  [/\bhigh[\s_-]?vol\b/gi, '高波动'],
  [/\bmax[\s_-]?drawdown[\s_-]?exceeded\b/gi, '最大回撤超限'],
  [/\bcircuit[\s_-]?breaker[\s_-]?triggered\b/gi, '熔断已触发'],
  [/\bdaily[\s_-]?loss[\s_-]?limit\b/gi, '日亏损限额触发'],
  [/\bconsecutive[\s_-]?loss(es)?\b/gi, '连续亏损'],
  [/\brisk[\s_-]?budget[\s_-]?exhaust(ed)?\b/gi, '风险预算耗尽'],
  [/\bcooldown[\s_-]?active\b/gi, '冷却期中'],
  [/\bno\s+active\s+alerts\b/gi, '暂无活动告警'],
  [/\bno\s+active\s+positions\b/gi, '暂无持仓'],
  [/\bnot\s+available\b/gi, '暂无'],
  [/\bno\s+reason\s+provided\b/gi, '未提供原因'],
];

// ─── 翻译单个 token（复合词先整体查，再拆词）────────────────────
function translateToken(input: string): string {
  const key = input.toLowerCase();
  if (exactMap[key]) return exactMap[key];
  // 下划线复合词：整体未命中则拆开逐词翻译
  if (key.includes('_')) {
    return input.split('_').map((p) => exactMap[p.toLowerCase()] ?? p).join('');
  }
  return input;
}

// ─── 主函数 ─────────────────────────────────────────────────────
export function zh(value: unknown, fallback = '暂无'): string {
  if (value === null || value === undefined || value === '') return fallback;
  let text = String(value);

  // 1. 先做正则短语替换（多词自然语言）
  for (const [pattern, replacement] of phraseMap) {
    text = text.replace(pattern, replacement);
  }

  // 2. 替换下划线/连字符复合标识符
  text = text.replace(/[A-Za-z]+(?:[_-][A-Za-z]+)+/g, (m) => translateToken(m));

  // 3. 替换剩余单个英文词
  text = text.replace(/[A-Za-z]+/g, (m) => exactMap[m.toLowerCase()] ?? m);

  return text;
}
