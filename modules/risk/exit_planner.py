"""
modules/risk/exit_planner.py — W9 退出路径规划器

设计说明：
- 根据 regime、volatility、confidence 规划具体止损 / 止盈参数
- 输出都是"计划值"，不直接修改订单，由调用方决定如何应用
- 支持三类出场规划：
    1. Static Stop Loss:  固定止损距离（基础值）
    2. ATR-based Stop:   基于 ATR 的动态止损（有 ATR 时优先使用）
    3. Trailing Stop:    追踪止盈（触发后启用追踪回调）
    4. ROI Ladder:       阶梯止盈（分批了结）

设计边界：
- ExitPlanner 只产出参数，不维护持仓状态
- 不实现止损价格的实时跟踪（那是执行层的工作）
- 所有参数基于"入场时快照"计算，适用于入场时规划

日志标签：[ExitPlan]
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class ExitPlanConfig:
    """ExitPlanner 超参数配置。"""

    # ── 静态止损 ────────────────────────────────────────────────
    base_stop_loss_pct: float = 0.03      # 基础止损距离（无 ATR 时使用）
    max_stop_loss_pct: float = 0.08       # 止损距离上限
    min_stop_loss_pct: float = 0.01       # 止损距离下限

    # ── ATR 止损 ─────────────────────────────────────────────────
    atr_stop_multiplier: float = 2.0      # stop = ATR_pct * multiplier

    # ── 追踪止盈 ─────────────────────────────────────────────────
    trailing_trigger_pct: float = 0.04    # 浮盈达到此比例时激活追踪
    trailing_callback_pct: float = 0.015  # 追踪回调比例

    # ── ROI 阶梯止盈 ──────────────────────────────────────────────
    roi_ladder: list[float] = field(
        default_factory=lambda: [0.03, 0.06, 0.10]
    )

    # ── Regime 调整系数 ───────────────────────────────────────────
    high_vol_stop_multiplier: float = 1.4        # 高波动：扩宽止损（避免被洗出）
    high_vol_trailing_multiplier: float = 1.3    # 高波动：追踪触发点更远
    low_confidence_stop_multiplier: float = 0.8  # 低置信度：收窄止损（更早离场）


@dataclass(frozen=True)
class ExitPlan:
    """单次入场的退出规划（只读快照）。"""
    stop_loss_pct: float              # 止损距离
    trailing_trigger_pct: float       # 追踪止盈激活阈值
    trailing_callback_pct: float      # 追踪回调比例
    take_profit_ladder: list[float]   # ROI 阶梯止盈目标
    debug_payload: dict = field(default_factory=dict)


class ExitPlanner:
    """
    退出路径规划器。

    使用示例（入场时调用）：
        planner = ExitPlanner()
        plan = planner.plan(
            dominant_regime="high_vol",
            signal_confidence=0.65,
            atr_pct=0.025,
        )
        # plan.stop_loss_pct ≈ 0.025 * 2.0 * 1.4 = 0.07 → clipped to 0.08

    Args:
        config: ExitPlanConfig 配置对象
    """

    def __init__(self, config: ExitPlanConfig | None = None) -> None:
        self.config = config or ExitPlanConfig()
        log.info(
            "[ExitPlan] 初始化: base_stop={:.2%} atr_mult={} "
            "trailing_trigger={:.2%} trailing_callback={:.2%}",
            self.config.base_stop_loss_pct,
            self.config.atr_stop_multiplier,
            self.config.trailing_trigger_pct,
            self.config.trailing_callback_pct,
        )

    def plan(
        self,
        dominant_regime: str = "unknown",
        signal_confidence: float = 0.5,
        atr_pct: float | None = None,
    ) -> ExitPlan:
        """
        根据市场状态规划退出参数。

        Args:
            dominant_regime:    当前 dominant regime
            signal_confidence:  信号置信度（0~1）
            atr_pct:            ATR / close 比例（可选，无时使用 base stop）

        Returns:
            ExitPlan（冻结只读）
        """
        cfg = self.config

        # ── 计算基础止损距离 ────────────────────────────────────
        if atr_pct is not None and atr_pct > 0:
            raw_stop = atr_pct * cfg.atr_stop_multiplier
        else:
            raw_stop = cfg.base_stop_loss_pct

        # ── Regime 调整 ─────────────────────────────────────────
        if dominant_regime == "high_vol":
            raw_stop *= cfg.high_vol_stop_multiplier

        # ── 置信度调整 ──────────────────────────────────────────
        if signal_confidence < 0.5:
            raw_stop *= cfg.low_confidence_stop_multiplier

        # ── 止损边界约束 ────────────────────────────────────────
        stop_loss_pct = max(cfg.min_stop_loss_pct, min(raw_stop, cfg.max_stop_loss_pct))

        # ── 追踪止盈参数 ────────────────────────────────────────
        # 高波动时追踪触发更远（避免过早锁定浮盈）
        trailing_trigger = cfg.trailing_trigger_pct
        if dominant_regime == "high_vol":
            trailing_trigger *= cfg.high_vol_trailing_multiplier

        plan = ExitPlan(
            stop_loss_pct=round(stop_loss_pct, 4),
            trailing_trigger_pct=round(trailing_trigger, 4),
            trailing_callback_pct=cfg.trailing_callback_pct,
            take_profit_ladder=list(cfg.roi_ladder),
            debug_payload={
                "regime": dominant_regime,
                "confidence": signal_confidence,
                "atr_pct": atr_pct,
                "raw_stop": round(raw_stop, 6),
                "stop_adjusted": round(stop_loss_pct, 4),
            },
        )

        log.debug(
            "[ExitPlan] 规划完成: regime={} conf={:.3f} atr={} "
            "stop={:.3%} trailing_trigger={:.3%} ladder={}",
            dominant_regime, signal_confidence,
            f"{atr_pct:.4f}" if atr_pct else "N/A",
            plan.stop_loss_pct, plan.trailing_trigger_pct, plan.take_profit_ladder,
        )
        return plan
