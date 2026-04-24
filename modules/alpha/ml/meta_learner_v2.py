"""
modules/alpha/ml/meta_learner_v2.py — source-aware MetaLearner v2（Phase 2 W14 + Phase 3 扩展）

设计说明：
- Phase 1 MetaLearner 保持不变（仅负责技术面模型投票融合）
- MetaLearnerV2 是一个薄包装层，负责：
    1. 调用 Phase 1 MetaLearner 得到技术面 MetaSignal
    2. 将 MetaSignal 转换为 SourceSignal（technical）
    3. 接受可选的外部 SourceSignal（onchain / sentiment / microstructure / rl）
    4. 委托 OmniSignalFusion 做多源融合
    5. 返回 FusionDecision（可直接传给策略层）
- 完全向后兼容：不传外部 source 时，退化为 technical-only 融合
  （等价于 Phase 1 MetaLearner 输出）

Phase 3 新增 source（W19-W21）：
    microstructure：来自 MicroFeatureBuilder + 规则评分器的订单簿 Alpha 信号
                    source_name="microstructure"，高更新频率（tick 级）
    rl：            来自 RL policy 推理的置信度信号（只有通过 paper/shadow 晋升的
                    policy 才应传入），source_name="rl"

接口：
    MetaLearnerV2Config(meta_config, fusion_config)
    MetaLearnerV2(config)
        .fuse(votes, external_signals=None, risk_snapshot=None) -> FusionDecision
        .fuse_from_meta_signal(meta_signal, external_signals=None, risk_snapshot=None) -> FusionDecision
        .fuse_with_phase3_sources(votes, microstructure_signal=None, rl_signal=None,
                                   other_signals=None, risk_snapshot=None) -> FusionDecision
        .diagnostics() -> dict

日志标签：[MetaV2]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional

from core.logger import get_logger
from modules.alpha.contracts.alpha_source_types import FusionDecision, SourceSignal
from modules.alpha.contracts.ensemble_types import ModelVote
from modules.alpha.ml.meta_learner import MetaLearner, MetaLearnerConfig
from modules.alpha.ml.omni_signal_fusion import OmniSignalFusion, OmniSignalFusionConfig

log = get_logger(__name__)


@dataclass
class MetaLearnerV2Config:
    """
    MetaLearnerV2 配置。

    Attributes:
        meta_config:   技术面 MetaLearner 配置（Phase 1 沿用）
        fusion_config: OmniSignalFusion 配置（多源融合）
    """

    meta_config: MetaLearnerConfig = field(default_factory=MetaLearnerConfig)
    fusion_config: OmniSignalFusionConfig = field(default_factory=OmniSignalFusionConfig)


class MetaLearnerV2:
    """
    source-aware 多维 Alpha 融合器（Phase 2 MetaLearner v2）。

    与 Phase 1 MetaLearner 的区别：
        Phase 1：ModelVote[] → MetaSignal（技术面专用）
        Phase 2：ModelVote[] + [SourceSignal...] → FusionDecision（多维融合）

    向后兼容：不传 external_signals 时等价于 technical-only 模式。

    Args:
        config: MetaLearnerV2Config
    """

    def __init__(self, config: Optional[MetaLearnerV2Config] = None) -> None:
        self._config = config or MetaLearnerV2Config()
        self._meta = MetaLearner(self._config.meta_config)
        self._fusion = OmniSignalFusion(self._config.fusion_config)
        self._n_calls = 0
        log.info(
            "[MetaV2] 初始化: fusion={} buy_thr={} sell_thr={}",
            self._config.meta_config.fusion_strategy,
            self._config.fusion_config.buy_threshold,
            self._config.fusion_config.sell_threshold,
        )

    # ──────────────────────────────────────────────────────────────
    # 核心融合接口
    # ──────────────────────────────────────────────────────────────

    def fuse(
        self,
        votes: List[ModelVote],
        external_signals: Optional[List[SourceSignal]] = None,
        risk_snapshot: Optional[Mapping[str, Any]] = None,
    ) -> FusionDecision:
        """
        完整融合入口：技术面投票 + 外部 source 信号 → FusionDecision。

        Args:
            votes:            Phase 1 ModelEnsemble.predict() 输出的 ModelVote 列表
            external_signals: 可选的外部 SourceSignal 列表
                              （来自 OnChainFeatureBuilder / SentimentFeatureBuilder
                              经规则评分器后产出的 SourceSignal）
            risk_snapshot:    可选的风险状态字典（含 current_drawdown 等）

        Returns:
            FusionDecision
        """
        self._n_calls += 1

        # ── 技术面：Phase 1 MetaLearner → MetaSignal → SourceSignal ──
        meta_signal = self._meta.fuse(votes)
        tech_signal = SourceSignal.from_meta_signal(
            meta_signal,
            freshness_ok=True,  # 技术面基于当前 bar，始终 fresh
            weight=1.0,
        )

        log.debug(
            "[MetaV2] technical signal: action={} confidence={:.3f} score={:.3f}",
            tech_signal.action,
            tech_signal.confidence,
            tech_signal.score,
        )

        # ── 组合所有信号 ──────────────────────────────────────────
        all_signals: list[SourceSignal] = [tech_signal]
        if external_signals:
            for sig in external_signals:
                log.debug(
                    "[MetaV2] external signal: source={} action={} "
                    "confidence={:.3f} freshness_ok={}",
                    sig.source_name,
                    sig.action,
                    sig.confidence,
                    sig.freshness_ok,
                )
            all_signals.extend(external_signals)

        # ── OmniSignalFusion 融合 ─────────────────────────────────
        decision = self._fusion.fuse(all_signals, risk_snapshot=risk_snapshot)

        log.info(
            "[MetaV2] 融合完成: n_signals={} action={} confidence={:.3f} "
            "dominant={} high_risk={}",
            len(all_signals),
            decision.final_action,
            decision.final_confidence,
            decision.dominant_source,
            decision.debug_payload.get("high_risk", False),
        )
        return decision

    def fuse_from_meta_signal(
        self,
        meta_signal: Any,  # MetaSignal，避免循环导入
        external_signals: Optional[List[SourceSignal]] = None,
        risk_snapshot: Optional[Mapping[str, Any]] = None,
    ) -> FusionDecision:
        """
        跳过 ModelVote → MetaSignal 步骤，直接从 MetaSignal 开始融合。

        适用于技术面信号已由上游产出、只需做多源融合的场景。

        Args:
            meta_signal:      Phase 1 MetaLearner.fuse() 的输出
            external_signals: 可选的外部 SourceSignal 列表
                              （onchain / sentiment / microstructure / rl）
            risk_snapshot:    可选的风险状态字典

        Returns:
            FusionDecision
        """
        self._n_calls += 1

        tech_signal = SourceSignal.from_meta_signal(
            meta_signal, freshness_ok=True, weight=1.0
        )
        all_signals: list[SourceSignal] = [tech_signal]
        if external_signals:
            all_signals.extend(external_signals)

        return self._fusion.fuse(all_signals, risk_snapshot=risk_snapshot)

    def fuse_with_phase3_sources(
        self,
        votes: List[ModelVote],
        microstructure_signal: Optional[SourceSignal] = None,
        rl_signal: Optional[SourceSignal] = None,
        other_signals: Optional[List[SourceSignal]] = None,
        risk_snapshot: Optional[Mapping[str, Any]] = None,
    ) -> FusionDecision:
        """
        Phase 3 便捷融合入口：明确接受 microstructure 和 rl 源。

        设计意图：
        - microstructure_signal 来自 MicroFeatureBuilder + 规则评分器，
          应在 tick 级别以 source_name="microstructure" 传入。
        - rl_signal 来自 RL policy 推理的置信度输出，
          只有已通过 paper/shadow 晋升的 policy 才应传入，
          以 source_name="rl" 传入。
        - 两者均为可选：未传入时自动跳过，不影响融合结果。
        - other_signals 可继续传入 onchain / sentiment 等 Phase 2 信号。

        Args:
            votes:                 Phase 1 ModelEnsemble.predict() 的 ModelVote 列表
            microstructure_signal: 订单簿微观结构 Alpha 信号（可选）
            rl_signal:             RL policy 置信度信号（可选）
            other_signals:         其他外部 SourceSignal（onchain / sentiment 等，可选）
            risk_snapshot:         可选的风险状态字典

        Returns:
            FusionDecision
        """
        external: list[SourceSignal] = []
        if other_signals:
            external.extend(other_signals)
        if microstructure_signal is not None:
            external.append(microstructure_signal)
        if rl_signal is not None:
            external.append(rl_signal)

        log.debug(
            "[MetaV2] fuse_with_phase3_sources: n_votes={} has_micro={} has_rl={} "
            "n_other={}",
            len(votes),
            microstructure_signal is not None,
            rl_signal is not None,
            len(other_signals) if other_signals else 0,
        )
        return self.fuse(votes, external_signals=external or None, risk_snapshot=risk_snapshot)

    # ──────────────────────────────────────────────────────────────
    # 诊断
    # ──────────────────────────────────────────────────────────────

    def diagnostics(self) -> dict[str, Any]:
        return {
            "n_calls": self._n_calls,
            "meta_learner": self._meta.diagnostics(),
            "fusion": self._fusion.diagnostics(),
        }
