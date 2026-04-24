"""
modules/risk/adaptive_matrix.py — W9-W10 自适应风险决策中枢

设计说明：
- 组合 RegimeState、信号置信度、回撤状态、波动率，产出 RiskPlan
- 是"规划层"：
    * 决定允不允许入场
    * 决定仓位乘数
    * 产出止损、追踪、ROI 止盈、DCA 计划
- 不是"执行层"：
    * 不直接修改订单
    * 不依赖具体策略实现
    * 不替代 RiskManager 的最后防线审核

决策流程：
    1. 硬约束前检（快速失败路径）
    2. 回撤 / 日损 → 仓位压缩
    3. Regime + 置信度 → 联合仓位调整
    4. ExitPlanner → 退出路径规划
    5. DCAEngine → DCA 层数规划
    6. CooldownManager → 冷却期检查
    7. 输出 RiskPlan

日志标签：[RiskMatrix]
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.logger import get_logger
from modules.alpha.contracts.regime_types import RegimeState
from modules.risk.cooldown import CooldownManager
from modules.risk.dca_engine import DCAConfig, DCAEngine
from modules.risk.exit_planner import ExitPlanConfig, ExitPlanner
from modules.risk.snapshot import RiskPlan, RiskSnapshot

log = get_logger(__name__)


@dataclass
class AdaptiveRiskMatrixConfig:
    """AdaptiveRiskMatrix 全局配置（所有阈值均可通过 Optuna 在 Phase 3 调优）。"""

    # ── 硬约束阈值 ───────────────────────────────────────────────
    max_drawdown_for_entry: float = 0.08     # 回撤超过此值时禁止新开仓
    max_daily_loss_for_entry: float = 0.025  # 单日亏损超过此值时禁止新开仓

    # ── 回撤 → 仓位压缩 ─────────────────────────────────────────
    # 线性模型：scalar = 1.0 - current_drawdown_pct * drawdown_scalar_per_pct
    drawdown_scalar_per_pct: float = 0.15   # 每 1% 回撤压缩仓位的比例
    drawdown_scalar_floor: float = 0.20     # 仓位乘数下限（最低 20%）

    # ── 置信度 → 仓位调整 ────────────────────────────────────────
    low_confidence_threshold: float = 0.50
    high_confidence_threshold: float = 0.70
    low_confidence_scalar: float = 0.60     # 低置信时仓位压缩到 60%
    high_confidence_scalar: float = 1.10   # 高置信时仓位轻微放大（受 max_scalar 约束）

    # ── Regime → 仓位调整 ────────────────────────────────────────
    high_vol_scalar: float = 0.60          # high_vol 时仓位压缩到 60%
    unknown_regime_scalar: float = 0.50    # unknown 时仓位压缩到 50%

    # ── 综合上限 ─────────────────────────────────────────────────
    max_position_scalar: float = 1.0       # 仓位乘数绝对上限（不允许超过 100%）

    # ── 子模块配置 ───────────────────────────────────────────────
    exit_config: ExitPlanConfig = field(default_factory=ExitPlanConfig)
    dca_config: DCAConfig = field(default_factory=DCAConfig)

    # 默认入场后冷却期（分钟）
    default_cooldown_minutes: int = 30

    # 配置版本（用于日志和 trace）
    config_version: str = "v1.0"


class AdaptiveRiskMatrix:
    """
    自适应风险决策中枢（Phase 2 W9-W10）。

    使用示例（在 AlphaRuntime.process_bar 之后、下单之前调用）：
        matrix = AdaptiveRiskMatrix()

        risk_plan = matrix.evaluate(
            symbol="BTC/USDT",
            risk_snapshot=current_risk_snapshot,
            regime=regime_state,
            signal_confidence=0.72,
            atr_pct=0.022,
        )

        if risk_plan.is_blocked:
            # 跳过本次信号
            for reason in risk_plan.block_reasons:
                log.info("[Main] 信号阻断: {}", reason)
        else:
            qty = position_sizer.volatility_target(...) * risk_plan.position_scalar
            # 提交订单，并记录入场
            matrix.record_entry("BTC/USDT")

    Args:
        config: AdaptiveRiskMatrixConfig 配置对象
    """

    def __init__(self, config: AdaptiveRiskMatrixConfig | None = None) -> None:
        self.config = config or AdaptiveRiskMatrixConfig()
        self._exit_planner = ExitPlanner(config=self.config.exit_config)
        self._dca_engine = DCAEngine(config=self.config.dca_config)
        self._cooldown = CooldownManager()

        log.info(
            "[RiskMatrix] 初始化: version={} max_dd_entry={:.2%} "
            "max_daily_loss={:.2%} drawdown_floor={:.2%} "
            "high_vol_scalar={:.2%} unknown_scalar={:.2%}",
            self.config.config_version,
            self.config.max_drawdown_for_entry,
            self.config.max_daily_loss_for_entry,
            self.config.drawdown_scalar_floor,
            self.config.high_vol_scalar,
            self.config.unknown_regime_scalar,
        )

    # ──────────────────────────────────────────────────────────────
    # 主评估接口
    # ──────────────────────────────────────────────────────────────

    def evaluate(
        self,
        symbol: str,
        risk_snapshot: RiskSnapshot,
        regime: RegimeState | None = None,
        signal_confidence: float = 0.5,
        atr_pct: float | None = None,
    ) -> RiskPlan:
        """
        综合风险状态和市场环境，输出 RiskPlan。

        Args:
            symbol:            交易对（用于冷却期检查）
            risk_snapshot:     当前风险快照（来自 RiskManager 或 BudgetChecker）
            regime:            当前 RegimeState（可选，缺失时走降级路径）
            signal_confidence: 信号置信度（0~1）
            atr_pct:           ATR / close 比例（可选，用于动态止损计算）

        Returns:
            RiskPlan（不允许入场时 allow_entry=False）
        """
        cfg = self.config
        dominant_regime = regime.dominant_regime if regime else "unknown"
        regime_confidence = regime.confidence if regime else 0.0

        log.debug(
            "[RiskMatrix] 评估: symbol={} regime={} regime_conf={:.3f} "
            "signal_conf={:.3f} drawdown={:.2%} daily_loss={:.2%} "
            "circuit={} kill_switch={}",
            symbol, dominant_regime, regime_confidence,
            signal_confidence,
            risk_snapshot.current_drawdown,
            risk_snapshot.daily_loss_pct,
            risk_snapshot.circuit_broken,
            risk_snapshot.kill_switch_active,
        )

        # ── Step 1: 硬约束前检（快速失败路径）──────────────────
        if risk_snapshot.circuit_broken:
            return RiskPlan.blocked("组合熔断已触发", symbol=symbol)

        if risk_snapshot.kill_switch_active:
            return RiskPlan.blocked("Kill Switch 已激活", symbol=symbol)

        if risk_snapshot.current_drawdown >= cfg.max_drawdown_for_entry:
            return RiskPlan.blocked(
                f"当前回撤 {risk_snapshot.current_drawdown:.2%} "
                f">= 禁入阈值 {cfg.max_drawdown_for_entry:.2%}",
                symbol=symbol,
                current_drawdown=risk_snapshot.current_drawdown,
            )

        if risk_snapshot.daily_loss_pct >= cfg.max_daily_loss_for_entry:
            return RiskPlan.blocked(
                f"单日亏损 {risk_snapshot.daily_loss_pct:.2%} "
                f">= 禁入阈值 {cfg.max_daily_loss_for_entry:.2%}",
                symbol=symbol,
            )

        if self._cooldown.is_cooling(symbol):
            remaining = self._cooldown.remaining_minutes(symbol)
            return RiskPlan.blocked(
                f"{symbol} 冷却期内（剩余 {remaining:.1f} 分钟）",
                symbol=symbol,
                remaining_min=remaining,
            )

        # ── Step 2: 回撤 → 仓位压缩 ────────────────────────────
        # 线性压缩：drawdown 每多 1%，乘数减少 drawdown_scalar_per_pct
        drawdown_pct = risk_snapshot.current_drawdown * 100.0  # 转为百分比
        drawdown_scalar = max(
            cfg.drawdown_scalar_floor,
            1.0 - drawdown_pct * cfg.drawdown_scalar_per_pct,
        )

        # ── Step 3: 置信度 → 仓位调整 ───────────────────────────
        if signal_confidence < cfg.low_confidence_threshold:
            confidence_scalar = cfg.low_confidence_scalar
        elif signal_confidence > cfg.high_confidence_threshold:
            confidence_scalar = cfg.high_confidence_scalar
        else:
            confidence_scalar = 1.0

        # ── Step 4: Regime → 仓位调整 ───────────────────────────
        if dominant_regime == "high_vol":
            regime_scalar = cfg.high_vol_scalar
        elif dominant_regime == "unknown":
            regime_scalar = cfg.unknown_regime_scalar
        else:
            regime_scalar = 1.0

        # ── Step 5: 综合仓位乘数 ────────────────────────────────
        position_scalar = drawdown_scalar * confidence_scalar * regime_scalar
        position_scalar = max(0.0, min(position_scalar, cfg.max_position_scalar))

        # ── Step 6: 退出路径规划 ────────────────────────────────
        exit_plan = self._exit_planner.plan(
            dominant_regime=dominant_regime,
            signal_confidence=signal_confidence,
            atr_pct=atr_pct,
        )

        # ── Step 7: DCA 规划 ─────────────────────────────────────
        dca_levels = self._dca_engine.plan(
            dominant_regime=dominant_regime,
            signal_confidence=signal_confidence,
            budget_remaining_pct=risk_snapshot.budget_remaining_pct,
        )

        plan = RiskPlan(
            allow_entry=True,
            position_scalar=round(position_scalar, 4),
            stop_loss_pct=exit_plan.stop_loss_pct,
            trailing_trigger_pct=exit_plan.trailing_trigger_pct,
            trailing_callback_pct=exit_plan.trailing_callback_pct,
            take_profit_ladder=exit_plan.take_profit_ladder,
            dca_levels=dca_levels,
            cooldown_minutes=cfg.default_cooldown_minutes,
            block_reasons=[],
            debug_payload={
                "symbol": symbol,
                "regime": dominant_regime,
                "regime_confidence": regime_confidence,
                "signal_confidence": signal_confidence,
                "drawdown_scalar": round(drawdown_scalar, 4),
                "confidence_scalar": round(confidence_scalar, 4),
                "regime_scalar": round(regime_scalar, 4),
                "position_scalar_final": round(position_scalar, 4),
                "exit_debug": exit_plan.debug_payload,
                "dca_levels": dca_levels,
                "config_version": cfg.config_version,
            },
        )

        log.info(
            "[RiskMatrix] 评估完成: symbol={} allow=True "
            "position_scalar={:.3f} stop={:.3%} "
            "dca_levels={} regime={} regime_conf={:.3f}",
            symbol,
            plan.position_scalar,
            plan.stop_loss_pct or 0.0,
            len(dca_levels),
            dominant_regime,
            regime_confidence,
        )
        return plan

    # ──────────────────────────────────────────────────────────────
    # 事件记录接口（入场 / 止损 → 更新冷却期）
    # ──────────────────────────────────────────────────────────────

    def record_entry(self, symbol: str, cooldown_minutes: int | None = None) -> None:
        """
        记录入场事件，启动冷却期。

        Args:
            symbol:           入场的交易对
            cooldown_minutes: 冷却期时长（None 时使用 default_cooldown_minutes）
        """
        minutes = (
            cooldown_minutes
            if cooldown_minutes is not None
            else self.config.default_cooldown_minutes
        )
        if minutes > 0:
            self._cooldown.set(symbol=symbol, minutes=minutes, reason="入场触发冷却")

    def record_stop_loss(self, symbol: str, extra_cooldown_minutes: int = 60) -> None:
        """
        记录止损触发事件，设置更长的冷却期。

        Args:
            symbol:                 触发止损的交易对
            extra_cooldown_minutes: 止损后冷却期（分钟）
        """
        self._cooldown.set(
            symbol=symbol,
            minutes=extra_cooldown_minutes,
            reason="止损触发冷却",
        )
        log.info(
            "[RiskMatrix] 止损冷却记录: symbol={} cooldown_minutes={}",
            symbol, extra_cooldown_minutes,
        )

    def release_cooldown(self, symbol: str) -> bool:
        """手动解除指定 symbol 的冷却期（人工介入场景）。"""
        released = self._cooldown.release(symbol)
        if released:
            log.info("[RiskMatrix] 手动解除冷却: symbol={}", symbol)
        return released

    # ──────────────────────────────────────────────────────────────
    # 诊断接口
    # ──────────────────────────────────────────────────────────────

    def health_snapshot(self) -> dict:
        """返回 AdaptiveRiskMatrix 当前状态的诊断快照。"""
        return {
            "config_version": self.config.config_version,
            "cooldown": self._cooldown.diagnostics(),
            "config": {
                "max_drawdown_for_entry": self.config.max_drawdown_for_entry,
                "max_daily_loss_for_entry": self.config.max_daily_loss_for_entry,
                "high_vol_scalar": self.config.high_vol_scalar,
                "unknown_regime_scalar": self.config.unknown_regime_scalar,
                "default_cooldown_minutes": self.config.default_cooldown_minutes,
            },
        }
