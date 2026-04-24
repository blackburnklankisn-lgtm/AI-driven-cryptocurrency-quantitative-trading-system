"""
modules/alpha/orchestration/gating.py — 环境不明确时的降级门控

设计说明：
- GatingRule: 单条降级规则（可组合为规则列表）
- GatingEngine: 依次评估所有规则，输出 GatingDecision
- 支持的门控类型：
    1. regime_low_confidence  — Regime 置信度低于阈值时触发
    2. regime_unknown         — dominant_regime == "unknown" 时触发
    3. high_vol_block         — 高波动市场整体禁止做多/空
    4. regime_unstable        — Regime 短期内频繁切换时触发（需 RegimeCache）
- 门控动作：
    ALLOW     — 正常放行
    REDUCE    — 降低仓位（交给 Orchestrator 实施，gating 只标记）
    BLOCK_BUY — 禁止开多
    BLOCK_ALL — 禁止所有方向信号（只允许 HOLD / 平仓）

日志标签：[Gating]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from core.logger import get_logger
from modules.alpha.contracts.regime_types import RegimeState

log = get_logger(__name__)


class GatingAction(str, Enum):
    ALLOW     = "ALLOW"
    REDUCE    = "REDUCE"
    BLOCK_BUY = "BLOCK_BUY"
    BLOCK_ALL = "BLOCK_ALL"


@dataclass
class GatingDecision:
    """GatingEngine 的输出决策。"""
    action: GatingAction
    triggered_rules: List[str] = field(default_factory=list)
    reduce_factor: float = 1.0  # 仓位缩减系数（REDUCE 时生效；1.0 = 不缩减）
    debug_payload: dict = field(default_factory=dict)

    @property
    def is_blocked(self) -> bool:
        return self.action in (GatingAction.BLOCK_ALL, GatingAction.BLOCK_BUY)

    @property
    def blocks_all(self) -> bool:
        return self.action == GatingAction.BLOCK_ALL


@dataclass
class GatingConfig:
    """GatingEngine 超参数配置。"""

    # Regime 置信度低于此值时触发 REDUCE
    regime_low_conf_threshold: float = 0.45
    # Regime 置信度极低时触发 BLOCK_ALL
    regime_very_low_conf_threshold: float = 0.25

    # unknown regime 时的门控动作
    unknown_regime_action: GatingAction = GatingAction.REDUCE
    unknown_reduce_factor: float = 0.5   # REDUCE 时的仓位系数

    # high_vol 下禁止做多
    block_buy_on_high_vol: bool = True
    high_vol_conf_threshold: float = 0.5  # high_vol 概率超过此值且置信高才触发

    # 低置信 REDUCE 时的系数
    low_conf_reduce_factor: float = 0.6


class GatingEngine:
    """
    环境门控引擎。

    使用：
        engine = GatingEngine(GatingConfig())
        decision = engine.evaluate(regime_state, is_regime_stable=True)

        if decision.blocks_all:
            # 跳过所有 BUY/SELL 信号
            ...
        elif decision.action == GatingAction.REDUCE:
            # 缩减仓位权重
            weight *= decision.reduce_factor
    """

    def __init__(self, config: GatingConfig | None = None) -> None:
        self.config = config or GatingConfig()

    def evaluate(
        self,
        regime: RegimeState,
        is_regime_stable: bool = True,
    ) -> GatingDecision:
        """
        评估当前 regime 状态，输出门控决策。

        Args:
            regime:           最新 RegimeState
            is_regime_stable: 最近几根 bar 的 regime 是否一致（来自 RegimeCache.is_stable()）

        Returns:
            GatingDecision
        """
        cfg = self.config
        triggered: List[str] = []

        # ── Rule 1: regime == unknown ──────────────────────────
        if regime.dominant_regime == "unknown":
            triggered.append("regime_unknown")
            log.debug("[Gating] 规则触发: regime_unknown → {}", cfg.unknown_regime_action.value)
            return GatingDecision(
                action=cfg.unknown_regime_action,
                triggered_rules=triggered,
                reduce_factor=cfg.unknown_reduce_factor,
                debug_payload={"dominant": "unknown", "confidence": regime.confidence},
            )

        # ── Rule 2: 极低置信度 → BLOCK_ALL ────────────────────
        if regime.confidence < cfg.regime_very_low_conf_threshold:
            triggered.append("regime_very_low_confidence")
            log.debug(
                "[Gating] 规则触发: regime_very_low_confidence conf={:.3f} → BLOCK_ALL",
                regime.confidence,
            )
            return GatingDecision(
                action=GatingAction.BLOCK_ALL,
                triggered_rules=triggered,
                debug_payload={"confidence": regime.confidence},
            )

        # ── Rule 3: 低置信度 → REDUCE ─────────────────────────
        if regime.confidence < cfg.regime_low_conf_threshold:
            triggered.append("regime_low_confidence")
            log.debug(
                "[Gating] 规则触发: regime_low_confidence conf={:.3f} → REDUCE factor={}",
                regime.confidence, cfg.low_conf_reduce_factor,
            )
            # 继续评估其他规则（不提前返回）

        # ── Rule 4: high_vol 禁止做多 ─────────────────────────
        if (
            cfg.block_buy_on_high_vol
            and regime.dominant_regime == "high_vol"
            and regime.confidence >= cfg.high_vol_conf_threshold
        ):
            triggered.append("high_vol_block_buy")
            log.debug(
                "[Gating] 规则触发: high_vol_block_buy conf={:.3f} → BLOCK_BUY",
                regime.confidence,
            )
            return GatingDecision(
                action=GatingAction.BLOCK_BUY,
                triggered_rules=triggered,
                debug_payload={"dominant": regime.dominant_regime, "confidence": regime.confidence},
            )

        # ── Rule 5: regime 不稳定 → REDUCE ───────────────────
        if not is_regime_stable:
            triggered.append("regime_unstable")
            log.debug("[Gating] 规则触发: regime_unstable → REDUCE")
            return GatingDecision(
                action=GatingAction.REDUCE,
                triggered_rules=triggered,
                reduce_factor=cfg.low_conf_reduce_factor,
                debug_payload={"stable": False, "dominant": regime.dominant_regime},
            )

        # ── 如果 Rule 3 已触发（低置信）但未被 Rule 4/5 覆盖 ──
        if "regime_low_confidence" in triggered:
            return GatingDecision(
                action=GatingAction.REDUCE,
                triggered_rules=triggered,
                reduce_factor=cfg.low_conf_reduce_factor,
                debug_payload={"confidence": regime.confidence},
            )

        # ── 全部通过 ──────────────────────────────────────────
        log.debug(
            "[Gating] ALLOW: dominant={} conf={:.3f} stable={}",
            regime.dominant_regime, regime.confidence, is_regime_stable,
        )
        return GatingDecision(
            action=GatingAction.ALLOW,
            triggered_rules=[],
            reduce_factor=1.0,
        )
