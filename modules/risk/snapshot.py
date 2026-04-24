"""
modules/risk/snapshot.py — W9 Phase 2 风控结构化快照

设计说明：
- RiskSnapshot: 从 RiskManager 运行时状态提取的只读结构化快照
    * 提供结构化字段给策略/编排器消费，取代松散的 risk_snapshot 字典
    * 实现 Mapping 接口以兼容 StrategyContext.risk_snapshot（当前为 Mapping[str, Any]）
- RiskPlan: AdaptiveRiskMatrix 输出的风险决策规划
    * 告诉调用方：允不允许入场、什么止损、什么仓位系数
    * 不直接执行，只是决策 + 理由

日志标签：[RiskSnapshot] [RiskPlan]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping


@dataclass(frozen=True)
class RiskSnapshot:
    """
    从 RiskManager 运行时状态提取的只读结构化快照。

    注意：这是对外只读 view，不是 RiskManager 的内部状态。
    实现 Mapping[str, Any] 以兼容 StrategyContext.risk_snapshot 字段。
    """

    current_drawdown: float           # 当前净值相对峰值的回撤（0~1）
    daily_loss_pct: float             # 当日已实现亏损占净值比例（0~1）
    consecutive_losses: int           # 连续亏损次数
    circuit_broken: bool              # 是否已触发组合熔断
    kill_switch_active: bool          # 操作级 Kill Switch 是否激活
    budget_remaining_pct: float       # 剩余可用风险预算比例（0~1）
    cooldown_symbols: dict[str, datetime] = field(default_factory=dict)  # symbol -> 冷却过期时间
    last_updated_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    metadata: dict[str, Any] = field(default_factory=dict)   # 可选附加字段

    # ──────────────────────────────────────────────────────────────
    # 快速判断接口
    # ──────────────────────────────────────────────────────────────

    def is_safe_to_trade(self) -> bool:
        """
        快速判断是否可以入场（不替代 RiskManager，只提供策略层参考）。

        Returns:
            True 当且仅当：未熔断、未激活 Kill Switch、有剩余预算、日损未达上限
        """
        return (
            not self.circuit_broken
            and not self.kill_switch_active
            and self.budget_remaining_pct > 0.0
            and self.daily_loss_pct < 1.0
        )

    def symbol_in_cooldown(self, symbol: str) -> bool:
        """判断指定 symbol 是否仍在冷却期内。"""
        if symbol not in self.cooldown_symbols:
            return False
        return datetime.now(tz=timezone.utc) < self.cooldown_symbols[symbol]

    # ──────────────────────────────────────────────────────────────
    # 序列化 / Mapping 兼容
    # ──────────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """
        转换为普通字典，兼容 StrategyContext.risk_snapshot（Mapping 接口）。
        """
        return {
            "current_drawdown": self.current_drawdown,
            "daily_loss_pct": self.daily_loss_pct,
            "consecutive_losses": self.consecutive_losses,
            "circuit_broken": self.circuit_broken,
            "kill_switch_active": self.kill_switch_active,
            "budget_remaining_pct": self.budget_remaining_pct,
            "cooldown_symbols": {
                k: v.isoformat() for k, v in self.cooldown_symbols.items()
            },
            "last_updated_at": self.last_updated_at.isoformat(),
            **self.metadata,
        }

    # Mapping 协议（支持 ctx.risk_snapshot["current_drawdown"] 访问方式）
    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __len__(self) -> int:
        return len(self.to_dict())

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    # ──────────────────────────────────────────────────────────────
    # 工厂方法
    # ──────────────────────────────────────────────────────────────

    @classmethod
    def make_default(cls) -> "RiskSnapshot":
        """构造一个"安全初始状态"快照（用于冷启动或无法获取 RiskManager 状态时）。"""
        return cls(
            current_drawdown=0.0,
            daily_loss_pct=0.0,
            consecutive_losses=0,
            circuit_broken=False,
            kill_switch_active=False,
            budget_remaining_pct=1.0,
        )

    @classmethod
    def make_blocked(cls, reason: str = "系统不可用") -> "RiskSnapshot":
        """构造一个"全面阻断"快照（用于紧急停机或初始化失败时）。"""
        return cls(
            current_drawdown=1.0,
            daily_loss_pct=1.0,
            consecutive_losses=999,
            circuit_broken=True,
            kill_switch_active=True,
            budget_remaining_pct=0.0,
            metadata={"block_reason": reason},
        )


# ══════════════════════════════════════════════════════════════
# RiskPlan — AdaptiveRiskMatrix 的输出结构
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RiskPlan:
    """
    AdaptiveRiskMatrix 输出的风险决策规划。

    这是一个"计划文档"：告诉调用方怎么入场、怎么出场，但不直接执行。
    调用方（main.py 或 StrategyOrchestrator）负责按此计划构造实际订单。
    """

    allow_entry: bool                          # 是否允许入场
    position_scalar: float                     # 仓位乘数（0~1，与 PositionSizer 结果相乘）
    stop_loss_pct: float | None                # 止损距离（例如 0.03 = 3% 止损）
    trailing_trigger_pct: float | None         # 追踪止盈激活阈值（浮盈超过此比例时启用）
    trailing_callback_pct: float | None        # 追踪止盈回调比例
    take_profit_ladder: list[float]            # ROI 阶梯止盈（例如 [0.03, 0.06, 0.10]）
    dca_levels: list[float]                    # DCA 价格偏移列表（例如 [-0.02, -0.04]）
    cooldown_minutes: int                      # 入场后冷却期（分钟）
    block_reasons: list[str]                   # 阻断原因（空列表 = 无阻断）
    debug_payload: dict[str, Any] = field(default_factory=dict)

    # ──────────────────────────────────────────────────────────────
    # 属性
    # ──────────────────────────────────────────────────────────────

    @property
    def is_blocked(self) -> bool:
        return not self.allow_entry

    @property
    def has_exit_plan(self) -> bool:
        return self.stop_loss_pct is not None or bool(self.take_profit_ladder)

    @property
    def has_dca_plan(self) -> bool:
        return bool(self.dca_levels)

    # ──────────────────────────────────────────────────────────────
    # 工厂方法
    # ──────────────────────────────────────────────────────────────

    @classmethod
    def blocked(cls, reason: str, **extra_debug: Any) -> "RiskPlan":
        """快速创建一个阻断 RiskPlan（测试和降级路径常用）。"""
        return cls(
            allow_entry=False,
            position_scalar=0.0,
            stop_loss_pct=None,
            trailing_trigger_pct=None,
            trailing_callback_pct=None,
            take_profit_ladder=[],
            dca_levels=[],
            cooldown_minutes=0,
            block_reasons=[reason],
            debug_payload=dict(extra_debug),
        )
