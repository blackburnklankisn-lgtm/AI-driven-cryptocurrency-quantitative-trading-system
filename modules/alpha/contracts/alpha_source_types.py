"""
modules/alpha/contracts/alpha_source_types.py — 多维 Alpha 信号合约（Phase 2 W14）

设计说明：
- 定义跨 source 的统一信号合约：SourceSignal / FusionDecision
- source 类型：technical（技术面）、onchain（链上）、sentiment（情绪）
- score 范围 [-1, 1]：负值偏空、正值偏多、0 中性
    * BUY  信号 → score > 0，大小代表多头强度
    * SELL 信号 → score < 0，大小代表空头强度
    * HOLD 信号 → score ≈ 0
- freshness_ok：source freshness 不通过时设为 False，融合层会自动置零权重
- weight：source 在融合层的基础权重（融合层可进一步调整）
- FusionDecision 包含完整的 source_signals 快照，供 trace 和回放使用

用法：
    # 将 MetaSignal 转换为 SourceSignal
    sig = SourceSignal.from_meta_signal(meta_signal, freshness_ok=True)

    # 从链上/情绪得分构建 SourceSignal
    sig = SourceSignal.from_score("onchain", score=0.3, freshness_ok=True)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SourceName = Literal["technical", "onchain", "sentiment"]
Action = Literal["BUY", "SELL", "HOLD"]


@dataclass(frozen=True)
class SourceSignal:
    """
    单个 Alpha Source 的输出信号。

    Attributes:
        source_name:   信号来源（technical / onchain / sentiment）
        action:        交易意向（BUY / SELL / HOLD）
        confidence:    置信度 [0, 1]（0=不确定，1=极高确定性）
        score:         归一化方向得分 [-1, 1]（负=空头，正=多头）
        freshness_ok:  数据是否在 TTL 内（False 时融合层置零权重）
        weight:        在融合层的基础权重（可被风险状态和 freshness 调整）
        debug_payload: 调试信息（不参与融合计算）
    """

    source_name: SourceName
    action: Action
    confidence: float
    score: float
    freshness_ok: bool = True
    weight: float = 1.0
    debug_payload: dict[str, Any] = field(default_factory=dict)

    # ── 工厂方法 ────────────────────────────────────────────

    @classmethod
    def from_meta_signal(
        cls,
        meta_signal: Any,  # MetaSignal（避免循环导入，用 Any）
        freshness_ok: bool = True,
        weight: float = 1.0,
    ) -> "SourceSignal":
        """
        将 Phase 1 MetaSignal 转换为 SourceSignal。

        score 映射规则：
            BUY  → confidence
            SELL → -confidence
            HOLD → 0.0
        """
        action: Action = meta_signal.final_action  # type: ignore[assignment]
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"

        confidence = float(meta_signal.final_confidence)
        if action == "BUY":
            score = confidence
        elif action == "SELL":
            score = -confidence
        else:
            score = 0.0

        return cls(
            source_name="technical",
            action=action,
            confidence=confidence,
            score=score,
            freshness_ok=freshness_ok,
            weight=weight,
            debug_payload={
                "dominant_model": meta_signal.dominant_model,
                "n_votes": len(meta_signal.model_votes),
            },
        )

    @classmethod
    def from_score(
        cls,
        source_name: SourceName,
        score: float,
        freshness_ok: bool = True,
        weight: float = 1.0,
        buy_threshold: float = 0.20,
        sell_threshold: float = -0.20,
        debug_payload: dict[str, Any] | None = None,
    ) -> "SourceSignal":
        """
        从归一化得分 [-1, 1] 构建 SourceSignal。

        action 映射：
            score > buy_threshold   → BUY
            score < sell_threshold  → SELL
            otherwise               → HOLD
        """
        score = max(-1.0, min(1.0, score))
        confidence = abs(score)
        if score > buy_threshold:
            action: Action = "BUY"
        elif score < sell_threshold:
            action = "SELL"
        else:
            action = "HOLD"

        return cls(
            source_name=source_name,
            action=action,
            confidence=confidence,
            score=score,
            freshness_ok=freshness_ok,
            weight=weight,
            debug_payload=debug_payload or {},
        )

    def effective_score(self) -> float:
        """返回考虑 freshness 后的有效得分（stale 时为 0.0）。"""
        return self.score if self.freshness_ok else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "action": self.action,
            "confidence": self.confidence,
            "score": self.score,
            "freshness_ok": self.freshness_ok,
            "weight": self.weight,
        }


@dataclass(frozen=True)
class FusionDecision:
    """
    OmniSignalFusion 的最终融合输出。

    Attributes:
        final_action:      最终交易意向（BUY / SELL / HOLD）
        final_confidence:  最终置信度 [0, 1]
        dominant_source:   贡献最大的 source 名称
        source_signals:    参与融合的所有 SourceSignal 快照（供 trace 使用）
        debug_payload:     调试信息（加权细节、降级原因等）
    """

    final_action: Action
    final_confidence: float
    dominant_source: str
    source_signals: list[SourceSignal] = field(default_factory=list)
    debug_payload: dict[str, Any] = field(default_factory=dict)

    def is_actionable(self) -> bool:
        """是否为可执行信号（BUY 或 SELL）。"""
        return self.final_action in ("BUY", "SELL")

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_action": self.final_action,
            "final_confidence": self.final_confidence,
            "dominant_source": self.dominant_source,
            "source_signals": [s.to_dict() for s in self.source_signals],
            "debug_payload": self.debug_payload,
        }
