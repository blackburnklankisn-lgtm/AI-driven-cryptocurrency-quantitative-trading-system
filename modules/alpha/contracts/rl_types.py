"""
modules/alpha/contracts/rl_types.py — RL 策略代理核心数据契约 (W19-W21)

设计说明：
- RLObservation:  策略代理的输入观测向量（聚合 technical/onchain/sentiment/microstructure/risk）
- RLAction:       策略代理的输出动作（方向 or 做市偏置）
- PolicyDecision: 携带 policy 版本、安全覆写标志的最终决策容器
- RolloutStep:    训练轨迹中的单步经验 (s, a, r, s', done)
- EvalResult:     OOS/paper/shadow 评估指标快照
- PolicyStatus:   policy 版本生命周期状态枚举

约束：
- 所有输出类型为 frozen dataclass（不可变）
- RL 动作不直接提交订单，只输出 ActionType 和 action_value
- RLObservation 保留 feature_names 以支持可解释性分析

日志标签：[RLAction] [RLPolicy] [RLEval]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional


# ══════════════════════════════════════════════════════════════
# 一、动作类型
# ══════════════════════════════════════════════════════════════

class ActionType(str, Enum):
    """
    RL 代理可输出的动作类型。

    方向模式：
        BUY    - 开多 / 加仓
        SELL   - 开空 / 减仓 / 平多
        HOLD   - 观望，不操作
        REDUCE - 降低当前仓位（方向不变，只减量）

    做市偏置模式：
        WIDEN_QUOTE   - 扩大 bid/ask 点差（风险上升时）
        NARROW_QUOTE  - 收窄 bid/ask 点差（竞争时）
        BIAS_BID      - 偏置买方（库存偏空时激励买入）
        BIAS_ASK      - 偏置卖方（库存偏多时激励卖出）
    """
    BUY          = "BUY"
    SELL         = "SELL"
    HOLD         = "HOLD"
    REDUCE       = "REDUCE"
    WIDEN_QUOTE  = "WIDEN_QUOTE"
    NARROW_QUOTE = "NARROW_QUOTE"
    BIAS_BID     = "BIAS_BID"
    BIAS_ASK     = "BIAS_ASK"


# ══════════════════════════════════════════════════════════════
# 二、Policy 生命周期状态
# ══════════════════════════════════════════════════════════════

class PolicyStatus(str, Enum):
    """
    Policy 版本生命周期状态。

    流转路径（正向）：
        candidate → shadow → paper → active

    降级路径：
        active → paused → retired
        active → rollback → active（切回旧版本）
    """
    CANDIDATE = "candidate"  # 刚训练完，待 replay/OOS 评估
    SHADOW    = "shadow"     # OOS 通过，在 shadow 模式监控（不影响真实仓位）
    PAPER     = "paper"      # shadow 稳定，进入 paper A/B 验证
    ACTIVE    = "active"     # A/B 通过，作为主策略运行
    PAUSED    = "paused"     # 临时暂停（等待人工审核或市场异常）
    RETIRED   = "retired"    # 淘汰，不再使用


# ══════════════════════════════════════════════════════════════
# 三、RL 观测
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RLObservation:
    """
    RL 策略代理的输入观测（对策略代理层透明、完整）。

    Attributes:
        symbol:           交易对
        trace_id:         全链路 trace ID
        feature_vector:   归一化特征向量（[-1, 1] or [0, 1]）
        feature_names:    与 feature_vector 对应的特征名称列表（可解释性）
        regime:           市场状态（趋势/震荡/崩溃等）
        risk_mode:        风险模式（normal / reduced / blocked）
        inventory_pct:    当前库存占比 ∈ [0, 1]
        position_pct:     方向性仓位占比 ∈ [-1, 1]（正=多，负=空）
        source_freshness: 各数据源 freshness 状态 dict
        timestamp:        观测时间（UTC）
        episode_step:     在当前 episode 中的步数（replay 训练用）
        debug_payload:    调试信息
    """

    symbol: str
    trace_id: str
    feature_vector: list[float]
    feature_names: list[str]
    regime: str
    risk_mode: str
    inventory_pct: float
    position_pct: float
    source_freshness: dict[str, bool]
    timestamp: datetime
    episode_step: int = 0
    debug_payload: dict[str, Any] = field(default_factory=dict)

    def dim(self) -> int:
        return len(self.feature_vector)

    def is_fresh(self) -> bool:
        """所有数据源均有效时返回 True。"""
        return all(self.source_freshness.values()) if self.source_freshness else False

    def to_numpy(self) -> "list[float]":
        """直接返回 feature_vector（numpy 转换由调用方执行）。"""
        return list(self.feature_vector)


# ══════════════════════════════════════════════════════════════
# 四、RL 动作
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RLAction:
    """
    RL 策略代理的输出动作。

    Attributes:
        action_type:   动作类型（ActionType 枚举）
        action_value:  动作强度 ∈ [0, 1]（0=最小，1=最大）
                       - 方向模式：仓位调整幅度比例
                       - 做市偏置模式：偏置幅度比例
        confidence:    策略置信度 ∈ [0, 1]
        action_index:  原始离散动作索引（用于训练 log）
        debug_payload: 完整的 logits/probs 等调试信息
    """

    action_type: ActionType
    action_value: float
    confidence: float
    action_index: int = 0
    debug_payload: dict[str, Any] = field(default_factory=dict)

    def is_directional(self) -> bool:
        """是否为方向性动作（BUY/SELL/HOLD/REDUCE）。"""
        return self.action_type in (
            ActionType.BUY, ActionType.SELL,
            ActionType.HOLD, ActionType.REDUCE
        )

    def is_mm_bias(self) -> bool:
        """是否为做市偏置动作。"""
        return self.action_type in (
            ActionType.WIDEN_QUOTE, ActionType.NARROW_QUOTE,
            ActionType.BIAS_BID, ActionType.BIAS_ASK
        )


# ══════════════════════════════════════════════════════════════
# 五、Policy 决策
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class PolicyDecision:
    """
    携带元信息的最终 Policy 决策。

    Attributes:
        policy_id:       policy 唯一标识
        policy_version:  policy 版本字符串
        action:          经安全审核的 RLAction
        reward_estimate: 当前状态的 value function 估计（None = 未启用）
        safety_override: True = 风险守卫覆写了原始 RL 动作
        override_reason: 覆写原因（safety_override=True 时填写）
        generated_at:    决策生成时间
        debug_payload:   完整调试信息（原始动作、logits 等）
    """

    policy_id: str
    policy_version: str
    action: RLAction
    reward_estimate: Optional[float]
    safety_override: bool
    override_reason: str = ""
    generated_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    debug_payload: dict[str, Any] = field(default_factory=dict)

    def effective_action(self) -> ActionType:
        """返回最终执行的动作类型（可能已被安全守卫覆写为 HOLD）。"""
        return self.action.action_type


# ══════════════════════════════════════════════════════════════
# 六、训练轨迹 (Rollout)
# ══════════════════════════════════════════════════════════════

@dataclass
class RolloutStep:
    """
    单步训练经验 (s, a, r, s', done, info)。

    Mutable dataclass（允许填充 next_obs 和 reward）。

    Attributes:
        obs:          当前状态观测（feature_vector）
        action_index: 动作索引
        action_type:  动作类型
        reward:       即时奖励（由 RewardEngine 计算）
        next_obs:     下一步观测（可能为 None = episode 结束）
        done:         episode 是否结束
        value_est:    当前状态的 value function 估计
        log_prob:     动作的 log 概率（PPO 更新用）
        info:         调试信息
        timestamp:    步骤时间戳
    """

    obs: list[float]
    action_index: int
    action_type: ActionType
    reward: float
    next_obs: Optional[list[float]]
    done: bool
    value_est: float = 0.0
    log_prob: float = 0.0
    info: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )

    def advantage(self, gamma: float = 0.99, lambda_: float = 0.95) -> float:
        """
        简单 TD advantage 估计（完整 GAE 由 RolloutStore 计算）。

        返回 r + gamma * next_value_est - value_est
        """
        next_val = 0.0  # 由 RolloutStore 在 done=True 时填充
        return self.reward + gamma * next_val - self.value_est


# ══════════════════════════════════════════════════════════════
# 七、评估结果
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class EvalResult:
    """
    OOS / paper / shadow 评估结果快照。

    Attributes:
        policy_id:          被评估的 policy ID
        policy_version:     被评估的 policy 版本
        eval_mode:          评估类型（oos / paper / shadow）
        total_return:       评估期总收益率
        sharpe:             Sharpe 比率（年化，无风险利率 = 0）
        max_drawdown:       最大回撤（0~1）
        win_rate:           胜率（有盈利的步/episode 比率）
        avg_turnover:       日均换手率
        risk_violations:    风险守卫触发次数
        n_episodes:         评估 episode 数量
        n_steps:            评估总步数
        eval_start:         评估开始时间
        eval_end:           评估结束时间
        passes_gate:        是否通过晋升门禁
        reason_codes:       通过/未通过的原因码列表
        debug_payload:      详细指标分解
    """

    policy_id: str
    policy_version: str
    eval_mode: Literal["oos", "paper", "shadow"]
    total_return: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    avg_turnover: float
    risk_violations: int
    n_episodes: int
    n_steps: int
    eval_start: datetime
    eval_end: datetime
    passes_gate: bool
    reason_codes: list[str] = field(default_factory=list)
    debug_payload: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"EvalResult[{self.eval_mode}] policy={self.policy_version} "
            f"sharpe={self.sharpe:.3f} mdd={self.max_drawdown:.3f} "
            f"win_rate={self.win_rate:.3f} passes={self.passes_gate}"
        )
