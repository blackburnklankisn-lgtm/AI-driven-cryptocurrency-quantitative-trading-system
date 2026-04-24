"""
modules/alpha/ml/meta_learner_v2.py — source-aware MetaLearner v2（Phase 2 W14）

设计说明：
- Phase 1 MetaLearner 保持不变（仅负责技术面模型投票融合）
- MetaLearnerV2 是一个薄包装层，负责：
    1. 调用 Phase 1 MetaLearner 得到技术面 MetaSignal
    2. 将 MetaSignal 转换为 SourceSignal（technical）
    3. 接受可选的外部 SourceSignal（onchain / sentiment）
    4. 委托 OmniSignalFusion 做多源融合
    5. 返回 FusionDecision（可直接传给策略层）
- 完全向后兼容：不传外部 source 时，退化为 technical-only 融合
  （等价于 Phase 1 MetaLearner 输出）

接口：
    MetaLearnerV2Config(meta_config, fusion_config)
    MetaLearnerV2(config)
        .fuse(votes, external_signals=None, risk_snapshot=None) -> FusionDecision
        .fuse_from_meta_signal(meta_signal, external_signals=None, risk_snapshot=None) -> FusionDecision
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

    # ──────────────────────────────────────────────────────────────
    # 诊断
    # ──────────────────────────────────────────────────────────────

    def diagnostics(self) -> dict[str, Any]:
        return {
            "n_calls": self._n_calls,
            "meta_learner": self._meta.diagnostics(),
            "fusion": self._fusion.diagnostics(),
        }
