"""
modules/alpha/rl/action_adapter.py — RL 动作映射器

设计说明：
- 将离散动作索引 (0~N) 映射为 RLAction（ActionType + action_value）
- 将 RLAction 进一步映射为风险守卫审核后的 PolicyDecision
- 安全覆写规则：
    * Kill Switch 激活 → 强制 HOLD / WIDEN_QUOTE
    * circuit_broken   → 强制 HOLD
    * budget_remaining_pct < 0.1 → 禁止 BUY / NARROW_QUOTE
    * confidence < floor → 降级为 HOLD
- 所有映射都可审计（reason_codes）

日志标签：[RLAction]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
import math

from core.logger import get_logger
from modules.alpha.contracts.rl_types import (
    ActionType,
    PolicyDecision,
    RLAction,
    RLObservation,
)
from modules.risk.snapshot import RiskSnapshot

log = get_logger(__name__)

# 默认离散动作空间（8 个动作，顺序严格对应 ppo_agent 输出）
DEFAULT_ACTION_SPACE: list[ActionType] = [
    ActionType.HOLD,          # 0 — 默认安全动作
    ActionType.BUY,           # 1
    ActionType.SELL,          # 2
    ActionType.REDUCE,        # 3
    ActionType.WIDEN_QUOTE,   # 4
    ActionType.NARROW_QUOTE,  # 5
    ActionType.BIAS_BID,      # 6
    ActionType.BIAS_ASK,      # 7
]


@dataclass
class ActionAdapterConfig:
    """
    动作映射器配置。

    Attributes:
        action_space:            离散动作空间顺序（索引 → ActionType）
        confidence_floor:        置信度低于此值时强制 HOLD
        action_value_scale:      动作强度缩放（乘以 action_value）
        hold_on_blocked_mode:    risk_mode == "blocked" 时强制 HOLD
        hold_on_reduced_mode:    risk_mode == "reduced" 时禁止 BUY / NARROW_QUOTE
    """

    action_space: list[ActionType] = field(
        default_factory=lambda: list(DEFAULT_ACTION_SPACE)
    )
    confidence_floor: float = 0.55
    action_value_scale: float = 1.0
    hold_on_blocked_mode: bool = True
    hold_on_reduced_mode: bool = True


class ActionAdapter:
    """
    RL 动作映射器。

    职责：
    1. action_index → RLAction
    2. RLAction + RiskSnapshot → PolicyDecision（含安全覆写）
    """

    def __init__(self, config: Optional[ActionAdapterConfig] = None) -> None:
        self.config = config or ActionAdapterConfig()
        self.n_actions = len(self.config.action_space)
        log.info(
            "[RLAction] ActionAdapter 初始化: n_actions={} confidence_floor={}",
            self.n_actions, self.config.confidence_floor,
        )

    def index_to_action(
        self,
        action_index: int,
        action_value: float = 1.0,
        confidence: float = 1.0,
        logits: Optional[list[float]] = None,
    ) -> RLAction:
        """
        将离散动作索引转换为 RLAction。

        Args:
            action_index: 动作索引（0 ~ n_actions-1）
            action_value: 动作强度（0 ~ 1）
            confidence:   策略置信度（policy head 输出的 prob 或 normalized logit）
            logits:       完整 logit 向量（调试用）

        Returns:
            RLAction
        """
        idx = int(action_index) % self.n_actions
        action_type = self.config.action_space[idx]
        scaled_value = min(1.0, max(0.0, action_value * self.config.action_value_scale))

        return RLAction(
            action_type=action_type,
            action_value=scaled_value,
            confidence=min(1.0, max(0.0, confidence)),
            action_index=idx,
            debug_payload={"logits": logits or [], "raw_index": action_index},
        )

    def apply_safety(
        self,
        action: RLAction,
        risk_snapshot: RiskSnapshot,
        obs: Optional[RLObservation] = None,
        policy_id: str = "rl_policy",
        policy_version: str = "v0",
    ) -> PolicyDecision:
        """
        将 RLAction 经安全守卫审核后封装为 PolicyDecision。

        安全覆写优先级（高到低）：
        1. Kill Switch 激活 → HOLD
        2. circuit_broken   → HOLD
        3. risk_mode blocked → HOLD
        4. confidence < floor → HOLD
        5. risk_mode reduced + BUY/NARROW_QUOTE → HOLD
        6. budget <= 0 → 禁止 BUY

        Args:
            action:         原始 RLAction
            risk_snapshot:  当前风险状态
            obs:            当前 RLObservation（可选，用于 risk_mode）
            policy_id:      policy 标识
            policy_version: policy 版本

        Returns:
            PolicyDecision
        """
        override = False
        override_reason = ""
        final_action = action

        risk_mode = obs.risk_mode if obs else "normal"

        # 优先级 1: Kill Switch
        if risk_snapshot.kill_switch_active:
            override = True
            override_reason = "KILL_SWITCH"
            final_action = self._make_hold(action)

        # 优先级 2: circuit_broken
        elif risk_snapshot.circuit_broken:
            override = True
            override_reason = "CIRCUIT_BROKEN"
            final_action = self._make_hold(action)

        # 优先级 3: blocked mode
        elif risk_mode == "blocked" and self.config.hold_on_blocked_mode:
            override = True
            override_reason = "RISK_BLOCKED"
            final_action = self._make_hold(action)

        # 优先级 4: 低置信度
        elif action.confidence < self.config.confidence_floor:
            override = True
            override_reason = f"LOW_CONFIDENCE={action.confidence:.3f}"
            final_action = self._make_hold(action)

        # 优先级 5: reduced mode 限制
        elif risk_mode == "reduced" and self.config.hold_on_reduced_mode:
            if action.action_type in (ActionType.BUY, ActionType.NARROW_QUOTE):
                override = True
                override_reason = f"REDUCED_RISK_BLOCKS_{action.action_type.value}"
                final_action = self._make_hold(action)

        # 优先级 6: 零预算禁止 BUY
        elif risk_snapshot.budget_remaining_pct <= 0.0:
            if action.action_type == ActionType.BUY:
                override = True
                override_reason = "ZERO_BUDGET_BLOCKS_BUY"
                final_action = self._make_hold(action)

        if override:
            log.warning(
                "[RLAction] 安全覆写: reason={} orig_action={} → HOLD "
                "kill_switch={} circuit_broken={} budget_pct={:.2f}",
                override_reason,
                action.action_type.value,
                risk_snapshot.kill_switch_active,
                risk_snapshot.circuit_broken,
                risk_snapshot.budget_remaining_pct,
            )
        else:
            log.debug(
                "[RLAction] 动作通过守卫: action={} value={:.3f} confidence={:.3f}",
                final_action.action_type.value,
                final_action.action_value,
                final_action.confidence,
            )

        return PolicyDecision(
            policy_id=policy_id,
            policy_version=policy_version,
            action=final_action,
            reward_estimate=None,
            safety_override=override,
            override_reason=override_reason,
        )

    def n_action_space(self) -> int:
        return self.n_actions

    def action_names(self) -> list[str]:
        return [a.value for a in self.config.action_space]

    # ──────────────────────────────────────────────────────────
    # 内部辅助
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _make_hold(original: RLAction) -> RLAction:
        """将动作强制替换为 HOLD，保留原始信息在 debug_payload 中。"""
        return RLAction(
            action_type=ActionType.HOLD,
            action_value=0.0,
            confidence=1.0,
            action_index=0,
            debug_payload={
                "overridden_from": original.action_type.value,
                "overridden_value": original.action_value,
                "overridden_confidence": original.confidence,
            },
        )
