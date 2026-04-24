"""
modules/alpha/rl/reward_engine.py — RL 奖励计算引擎

设计说明：
- 只计算 reward，不更新仓位或提交订单
- 奖励构成：
    * realized_pnl_norm:    已实现 PnL（归一化）
    * unrealized_pnl_delta: 未实现 PnL 变化（mark-to-market）
    * fee_penalty:          手续费惩罚（负值）
    * drawdown_penalty:     回撤惩罚（负值，二次惩罚）
    * turnover_penalty:     换手率惩罚（过度交易惩罚）
    * kill_switch_penalty:  触发 Kill Switch 的惩罚（大负值）
    * inventory_penalty:    库存极端偏离惩罚
- 所有分量可配置权重
- 输出 RewardBreakdown（可审计）

日志标签：[RewardEngine]
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from core.logger import get_logger

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class RewardConfig:
    """
    奖励函数配置。

    Attributes:
        pnl_scale:              PnL 奖励缩放系数（控制量纲）
        fee_penalty:            手续费惩罚权重 > 0
        drawdown_penalty:       回撤惩罚权重 > 0（二次曲线）
        turnover_penalty:       换手率惩罚权重 ≥ 0
        kill_switch_penalty:    Kill Switch 触发的固定惩罚（大负值）
        inventory_penalty:      库存极端偏离惩罚权重 ≥ 0
        unrealized_weight:      未实现 PnL delta 的权重（一般 < 1 以减少 mark-to-market 噪声）
        risk_violation_penalty: 任何风险违规的固定惩罚
        max_reward_clip:        奖励上限裁剪（防止梯度爆炸）
        min_reward_clip:        奖励下限裁剪
    """

    pnl_scale: float = 100.0
    fee_penalty: float = 1.0
    drawdown_penalty: float = 2.0
    turnover_penalty: float = 0.2
    kill_switch_penalty: float = -10.0
    inventory_penalty: float = 0.5
    unrealized_weight: float = 0.3
    risk_violation_penalty: float = -5.0
    max_reward_clip: float = 10.0
    min_reward_clip: float = -10.0


# ══════════════════════════════════════════════════════════════
# 二、奖励分解结果
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RewardBreakdown:
    """
    奖励分量明细（可审计、可日志、可 trace）。

    Attributes:
        total:               最终总奖励（经 clip）
        realized_pnl:        已实现 PnL 分量
        unrealized_delta:    未实现 PnL 变化分量
        fee:                 手续费惩罚分量
        drawdown:            回撤惩罚分量
        turnover:            换手率惩罚分量
        kill_switch:         Kill Switch 惩罚分量
        inventory:           库存偏离惩罚分量
        risk_violation:      风险违规惩罚分量
        raw_total:           未 clip 的总奖励
    """

    total: float
    realized_pnl: float
    unrealized_delta: float
    fee: float
    drawdown: float
    turnover: float
    kill_switch: float
    inventory: float
    risk_violation: float
    raw_total: float

    def to_dict(self) -> dict[str, float]:
        return {
            "total": self.total,
            "realized_pnl": self.realized_pnl,
            "unrealized_delta": self.unrealized_delta,
            "fee": self.fee,
            "drawdown": self.drawdown,
            "turnover": self.turnover,
            "kill_switch": self.kill_switch,
            "inventory": self.inventory,
            "risk_violation": self.risk_violation,
            "raw_total": self.raw_total,
        }


# ══════════════════════════════════════════════════════════════
# 三、RewardEngine 主体
# ══════════════════════════════════════════════════════════════

class RewardEngine:
    """
    RL 奖励计算引擎。

    无状态（每次 compute() 是独立计算）。
    仅在 RewardConfig 中配置行为。
    """

    def __init__(self, config: Optional[RewardConfig] = None) -> None:
        self.config = config or RewardConfig()
        log.info(
            "[RewardEngine] 初始化: pnl_scale={} drawdown_penalty={} "
            "turnover_penalty={} kill_switch_penalty={}",
            self.config.pnl_scale,
            self.config.drawdown_penalty,
            self.config.turnover_penalty,
            self.config.kill_switch_penalty,
        )

    def compute(
        self,
        realized_pnl: float = 0.0,
        prev_unrealized_pnl: float = 0.0,
        curr_unrealized_pnl: float = 0.0,
        fee_paid: float = 0.0,
        current_drawdown: float = 0.0,
        turnover: float = 0.0,
        kill_switch_active: bool = False,
        inventory_deviation: float = 0.0,
        risk_violated: bool = False,
        portfolio_value: float = 10000.0,
    ) -> RewardBreakdown:
        """
        计算单步奖励。

        Args:
            realized_pnl:         本步已实现 PnL（USDT）
            prev_unrealized_pnl:  前一步未实现 PnL
            curr_unrealized_pnl:  当前未实现 PnL
            fee_paid:             本步手续费（USDT，正值）
            current_drawdown:     当前组合回撤（0~1）
            turnover:             本步换手率（交易量/组合价值）
            kill_switch_active:   是否已激活 Kill Switch
            inventory_deviation:  库存偏离度（|deviation| ∈ [0, 1]）
            risk_violated:        是否发生风险违规
            portfolio_value:      组合总价值（用于归一化 PnL）

        Returns:
            RewardBreakdown（含各分量及总奖励）
        """
        scale = self.config.pnl_scale / max(portfolio_value, 1.0)

        # ── 已实现 PnL
        r_realized = realized_pnl * scale

        # ── 未实现 PnL delta（mark-to-market，噪声较大，权重低）
        unrealized_delta = (curr_unrealized_pnl - prev_unrealized_pnl) * scale
        r_unrealized = unrealized_delta * self.config.unrealized_weight

        # ── 手续费惩罚（负值）
        r_fee = -(fee_paid * scale * self.config.fee_penalty)

        # ── 回撤惩罚（二次曲线，加速惩罚大回撤）
        r_drawdown = -(current_drawdown ** 2) * self.config.drawdown_penalty

        # ── 换手率惩罚
        r_turnover = -(turnover * self.config.turnover_penalty)

        # ── Kill Switch 惩罚
        r_ks = self.config.kill_switch_penalty if kill_switch_active else 0.0

        # ── 库存偏离惩罚
        r_inv = -(abs(inventory_deviation) * self.config.inventory_penalty)

        # ── 风险违规惩罚
        r_rv = self.config.risk_violation_penalty if risk_violated else 0.0

        raw = r_realized + r_unrealized + r_fee + r_drawdown + r_turnover + r_ks + r_inv + r_rv
        clipped = max(self.config.min_reward_clip, min(self.config.max_reward_clip, raw))

        breakdown = RewardBreakdown(
            total=clipped,
            realized_pnl=r_realized,
            unrealized_delta=r_unrealized,
            fee=r_fee,
            drawdown=r_drawdown,
            turnover=r_turnover,
            kill_switch=r_ks,
            inventory=r_inv,
            risk_violation=r_rv,
            raw_total=raw,
        )

        log.debug(
            "[RewardEngine] 奖励: total={:.4f} realized={:.4f} unreal={:.4f} "
            "fee={:.4f} dd={:.4f} turnover={:.4f} ks={} inv={:.4f}",
            clipped, r_realized, r_unrealized, r_fee, r_drawdown,
            r_turnover, kill_switch_active, r_inv,
        )

        return breakdown
