"""
modules/alpha/ml/omni_signal_fusion.py — 多维 Alpha 融合中枢（Phase 2 W14 + Phase 3 扩展）

设计说明：
- 消费五类独立 Alpha 源（technical / onchain / sentiment / microstructure / rl）的 SourceSignal
- Phase 2 W14：支持 technical / onchain / sentiment
- Phase 3 W19-W21 扩展：新增 microstructure（订单簿微观结构）和 rl（RL policy 置信度信号）
- 使用"可解释的加权融合"，不引入复杂 stacking
- 融合规则（按优先级）：
    1. freshness 过滤：freshness_ok=False 的 source 权重强制为 0
    2. 风险状态压制：risk_snapshot 中风险偏高时，自动降低外部 source 权重
    3. 加权得分求和：Σ(effective_score × effective_weight)
    4. 阈值判断：aggregate_score → BUY / SELL / HOLD
    5. 降级：所有外部 source 均不可用时，fallback 到 technical-only
- dominant_source：有效权重最大的那个 source
- 新 source 权重设计：
    microstructure：高更新频率（tick 级），捕获即时订单流信息，默认权重 0.6
    rl：policy confidence 信号，只有 RL 通过 paper/shadow 晋升后才应传入，默认权重 0.4

接口：
    OmniSignalFusionConfig(...)
    OmniSignalFusion(config)
        .fuse(signals, risk_snapshot=None) -> FusionDecision
        .diagnostics() -> dict

日志标签：[OmniFusion]  [SourceAlpha]  [MetaV2]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from core.logger import get_logger
from modules.alpha.contracts.alpha_source_types import (
    Action,
    FusionDecision,
    SourceSignal,
)

log = get_logger(__name__)

# 有效 source 名称集合（Phase 2 W14 + Phase 3 W19-W21 扩展）
_VALID_SOURCES = {"technical", "onchain", "sentiment", "microstructure", "rl"}


@dataclass
class OmniSignalFusionConfig:
    """
    OmniSignalFusion 配置项。

    Attributes:
        buy_threshold:                aggregate_score 超过此值 → BUY
        sell_threshold:               aggregate_score 低于此值 → SELL（应为负值）
        technical_base_weight:        技术面基础权重
        onchain_base_weight:          链上基础权重
        sentiment_base_weight:        情绪基础权重
        microstructure_base_weight:   订单簿微观结构基础权重（Phase 3 新增）
        rl_base_weight:               RL policy 信号基础权重（Phase 3 新增）
        risk_penalty_threshold:       风险状态触发外部 source 降权的 drawdown 阈值
        risk_penalty_factor:          高风险时外部 source 权重乘以此系数 [0, 1]
        min_active_sources:           最少需要几个 freshness_ok=True 的 source
                                      （低于此数时打 WARNING，但仍运行）
        technical_only_fallback:      True 时当所有外部 source 均 stale 时
                                      强制降级为 technical-only
    """

    buy_threshold: float = 0.15
    sell_threshold: float = -0.15
    technical_base_weight: float = 1.0
    onchain_base_weight: float = 0.5
    sentiment_base_weight: float = 0.5
    microstructure_base_weight: float = 0.6  # Phase 3: tick 级微观结构信号
    rl_base_weight: float = 0.4              # Phase 3: RL policy 置信度信号
    risk_penalty_threshold: float = 0.05     # drawdown > 5% 时触发外部 source 降权
    risk_penalty_factor: float = 0.3         # 高风险时外部权重 × 0.3
    min_active_sources: int = 1
    technical_only_fallback: bool = True

    def __post_init__(self) -> None:
        if self.sell_threshold >= 0:
            raise ValueError(
                f"sell_threshold 应为负值，实际: {self.sell_threshold}"
            )
        if self.buy_threshold <= 0:
            raise ValueError(
                f"buy_threshold 应为正值，实际: {self.buy_threshold}"
            )
        if not (0.0 <= self.risk_penalty_factor <= 1.0):
            raise ValueError(
                f"risk_penalty_factor 应在 [0, 1]，实际: {self.risk_penalty_factor}"
            )
        if self.microstructure_base_weight < 0:
            raise ValueError(
                f"microstructure_base_weight 应 >= 0，实际: {self.microstructure_base_weight}"
            )
        if self.rl_base_weight < 0:
            raise ValueError(
                f"rl_base_weight 应 >= 0，实际: {self.rl_base_weight}"
            )

    def base_weight(self, source_name: str) -> float:
        """返回指定 source 的基础权重。未知 source 默认权重 1.0。"""
        if source_name == "technical":
            return self.technical_base_weight
        if source_name == "onchain":
            return self.onchain_base_weight
        if source_name == "sentiment":
            return self.sentiment_base_weight
        if source_name == "microstructure":
            return self.microstructure_base_weight
        if source_name == "rl":
            return self.rl_base_weight
        return 1.0


class OmniSignalFusion:
    """
    多维 Alpha 融合中枢。

    将 technical / onchain / sentiment 三个维度的 SourceSignal 加权融合，
    输出单一 FusionDecision。

    融合逻辑：
        1. 确认每个 source 的 effective_weight（freshness + 风险压制）
        2. 计算 aggregate_score = Σ(score_i × weight_i) / Σ(weight_i)
        3. 阈值映射 aggregate_score → BUY / SELL / HOLD
        4. dominant_source = weight_i 最大的 source
        5. 降级：外部 source 全 stale 时自动 technical-only

    Args:
        config: OmniSignalFusionConfig
    """

    def __init__(self, config: Optional[OmniSignalFusionConfig] = None) -> None:
        self.config = config or OmniSignalFusionConfig()
        self._n_fuse_calls = 0
        self._n_degraded = 0
        log.info(
            "[OmniFusion] 初始化: buy_thr={} sell_thr={} "
            "weights=tech:{}/onchain:{}/sentiment:{}/micro:{}/rl:{} risk_penalty={}@{}",
            self.config.buy_threshold,
            self.config.sell_threshold,
            self.config.technical_base_weight,
            self.config.onchain_base_weight,
            self.config.sentiment_base_weight,
            self.config.microstructure_base_weight,
            self.config.rl_base_weight,
            self.config.risk_penalty_factor,
            self.config.risk_penalty_threshold,
        )

    # ──────────────────────────────────────────────────────────────
    # 核心融合接口
    # ──────────────────────────────────────────────────────────────

    def fuse(
        self,
        signals: list[SourceSignal],
        risk_snapshot: Optional[Mapping[str, Any]] = None,
    ) -> FusionDecision:
        """
        融合多个 SourceSignal，输出 FusionDecision。

        Args:
            signals:       SourceSignal 列表（来自 technical / onchain / sentiment）
            risk_snapshot: 可选的风险状态字典（含 current_drawdown 等）
                           来自 RiskSnapshot 或 RiskManager 的摘要

        Returns:
            FusionDecision
        """
        self._n_fuse_calls += 1

        if not signals:
            log.warning("[OmniFusion] 收到空 signals 列表，返回 HOLD")
            return self._hold_decision(signals, reason="empty_signals")

        # ── 去重（同一 source_name 只保留最后一个）────────────────
        deduplicated: dict[str, SourceSignal] = {}
        for sig in signals:
            deduplicated[sig.source_name] = sig
        signals = list(deduplicated.values())

        # ── 判断是否处于高风险状态 ────────────────────────────────
        high_risk = self._is_high_risk(risk_snapshot)

        # ── 计算各 source 的有效权重 ──────────────────────────────
        effective_weights: dict[str, float] = {}
        for sig in signals:
            base_w = sig.weight * self.config.base_weight(sig.source_name)
            if not sig.freshness_ok:
                eff_w = 0.0
            elif high_risk and sig.source_name != "technical":
                eff_w = base_w * self.config.risk_penalty_factor
            else:
                eff_w = base_w
            effective_weights[sig.source_name] = eff_w

            log.debug(
                "[SourceAlpha] source={} action={} confidence={:.3f} score={:.3f} "
                "freshness_ok={} base_w={:.3f} eff_w={:.3f}",
                sig.source_name,
                sig.action,
                sig.confidence,
                sig.score,
                sig.freshness_ok,
                base_w,
                eff_w,
            )

        # ── 检查活跃 source 数量 ──────────────────────────────────
        active_count = sum(1 for w in effective_weights.values() if w > 0)
        if active_count < self.config.min_active_sources:
            log.warning(
                "[OmniFusion] 活跃 source 数量不足: active={} min={}",
                active_count,
                self.config.min_active_sources,
            )

        # ── 技术面 fallback 检查 ──────────────────────────────────
        external_active = sum(
            1 for sig in signals
            if sig.source_name != "technical" and effective_weights.get(sig.source_name, 0) > 0
        )
        tech_sig = next((s for s in signals if s.source_name == "technical"), None)

        if (
            self.config.technical_only_fallback
            and external_active == 0
            and tech_sig is not None
            and effective_weights.get("technical", 0) > 0
        ):
            log.warning(
                "[OmniFusion] 所有外部 source 均不可用，降级为 technical-only"
            )
            self._n_degraded += 1
            return self._build_decision(
                signals=signals,
                effective_weights=effective_weights,
                reason="technical_only_fallback",
                high_risk=high_risk,
            )

        # ── 常规融合路径 ──────────────────────────────────────────
        return self._build_decision(
            signals=signals,
            effective_weights=effective_weights,
            reason="normal",
            high_risk=high_risk,
        )

    # ──────────────────────────────────────────────────────────────
    # 辅助：构建 FusionDecision
    # ──────────────────────────────────────────────────────────────

    def _build_decision(
        self,
        signals: list[SourceSignal],
        effective_weights: dict[str, float],
        reason: str,
        high_risk: bool,
    ) -> FusionDecision:
        """加权融合信号，生成 FusionDecision。"""
        total_weight = sum(effective_weights.values())

        if total_weight == 0.0:
            log.warning("[OmniFusion] 所有 source 有效权重为 0，返回 HOLD")
            return self._hold_decision(signals, reason="zero_total_weight")

        # 加权平均得分
        agg_score = sum(
            sig.score * effective_weights.get(sig.source_name, 0.0)
            for sig in signals
        ) / total_weight

        # 阈值判断
        if agg_score > self.config.buy_threshold:
            final_action: Action = "BUY"
        elif agg_score < self.config.sell_threshold:
            final_action = "SELL"
        else:
            final_action = "HOLD"

        final_confidence = min(1.0, abs(agg_score))

        # dominant source = 有效权重最大的 source（且 freshness_ok）
        dominant_source = max(
            effective_weights,
            key=lambda k: effective_weights[k],
            default="technical",
        )

        log.info(
            "[OmniFusion] 融合完成: n_sources={} agg_score={:.4f} "
            "action={} confidence={:.3f} dominant={} reason={} high_risk={}",
            len(signals),
            agg_score,
            final_action,
            final_confidence,
            dominant_source,
            reason,
            high_risk,
        )

        weight_detail = [
            {
                "source": sig.source_name,
                "action": sig.action,
                "score": sig.score,
                "confidence": sig.confidence,
                "eff_weight": effective_weights.get(sig.source_name, 0.0),
                "freshness_ok": sig.freshness_ok,
            }
            for sig in signals
        ]
        log.debug("[MetaV2] 加权明细: {}", weight_detail)

        return FusionDecision(
            final_action=final_action,
            final_confidence=final_confidence,
            dominant_source=dominant_source,
            source_signals=list(signals),
            debug_payload={
                "aggregate_score": agg_score,
                "total_weight": total_weight,
                "high_risk": high_risk,
                "reason": reason,
                "weight_detail": weight_detail,
                "buy_threshold": self.config.buy_threshold,
                "sell_threshold": self.config.sell_threshold,
            },
        )

    def _hold_decision(
        self, signals: list[SourceSignal], reason: str
    ) -> FusionDecision:
        """返回 HOLD 决策（降级路径）。"""
        self._n_degraded += 1
        return FusionDecision(
            final_action="HOLD",
            final_confidence=0.0,
            dominant_source="none",
            source_signals=list(signals),
            debug_payload={"reason": reason},
        )

    # ──────────────────────────────────────────────────────────────
    # 辅助：风险状态判断
    # ──────────────────────────────────────────────────────────────

    def _is_high_risk(self, risk_snapshot: Optional[Mapping[str, Any]]) -> bool:
        """
        判断是否处于高风险状态（用于外部 source 降权）。

        检查以下字段（任一满足即为高风险）：
            - current_drawdown >= risk_penalty_threshold
            - kill_switch_active == True
        """
        if risk_snapshot is None:
            return False
        drawdown = float(risk_snapshot.get("current_drawdown", 0.0))
        kill_switch = bool(risk_snapshot.get("kill_switch_active", False))
        if drawdown >= self.config.risk_penalty_threshold or kill_switch:
            log.debug(
                "[OmniFusion] 高风险状态: drawdown={:.3f} kill_switch={}",
                drawdown,
                kill_switch,
            )
            return True
        return False

    # ──────────────────────────────────────────────────────────────
    # 诊断
    # ──────────────────────────────────────────────────────────────

    def diagnostics(self) -> dict[str, Any]:
        return {
            "n_fuse_calls": self._n_fuse_calls,
            "n_degraded": self._n_degraded,
            "config": {
                "buy_threshold": self.config.buy_threshold,
                "sell_threshold": self.config.sell_threshold,
                "technical_base_weight": self.config.technical_base_weight,
                "onchain_base_weight": self.config.onchain_base_weight,
                "sentiment_base_weight": self.config.sentiment_base_weight,
                "risk_penalty_threshold": self.config.risk_penalty_threshold,
                "risk_penalty_factor": self.config.risk_penalty_factor,
                "technical_only_fallback": self.config.technical_only_fallback,
            },
        }
