"""
modules/alpha/orchestration/strategy_orchestrator.py — 策略编排器

设计说明：
- StrategyOrchestrator 是 W6 的核心：把"多策略信号 + Regime + 表现历史"整合为最终决策
- 输入：OrchestrationInput（regime、strategy_results、性能快照、权益、回撤）
- 输出：OrchestrationDecision（通过的结果、权重、阻断原因、debug）
- 内部调用顺序：
    1. GatingEngine.evaluate()     — 环境门控（block/reduce/allow）
    2. AffinityMatrix.get_weight() — 亲和度权重调整
    3. ConflictResolver.resolve()  — 同 symbol 冲突处理
    4. 表现折扣（低 avg_confidence 的策略权重下调）
    5. 归一化权重输出

遵循模块化硬规则：
- Orchestrator 不读 OHLCV 明细，只处理结构化结果
- 不直接依赖具体策略对象，只消费 StrategyResult
- 所有决策必须可通过日志回放

日志标签：[Orchestrator]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from modules.alpha.contracts.regime_types import RegimeState
from modules.alpha.contracts.strategy_result import StrategyResult
from modules.alpha.orchestration.gating import (
    GatingAction,
    GatingConfig,
    GatingDecision,
    GatingEngine,
)
from modules.alpha.orchestration.performance_store import (
    PerformanceStore,
    StrategyPerformance,
)
from modules.alpha.orchestration.policy import (
    AffinityMatrix,
    ConflictResolver,
    PolicyConfig,
)

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 输入输出结构
# ══════════════════════════════════════════════════════════════

@dataclass
class OrchestrationInput:
    """策略编排器的输入快照。"""
    regime: RegimeState
    strategy_results: List[StrategyResult]
    equity: float = 10000.0
    current_drawdown: float = 0.0   # 当前回撤（0~1）
    is_regime_stable: bool = True
    bar_seq: int = 0


@dataclass
class OrchestrationDecision:
    """策略编排器的输出决策。"""
    # 通过门控的策略结果（action 已经过冲突处理）
    selected_results: List[StrategyResult]
    # 各策略的最终权重（strategy_id -> weight，归一化后）
    weights: Dict[str, float]
    # 阻断原因列表（调试 / 日志回放用）
    block_reasons: List[str]
    # 门控决策
    gating: GatingDecision
    # 调试载荷
    debug_payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_blocked(self) -> bool:
        return self.gating.blocks_all and not self.selected_results

    @property
    def has_signals(self) -> bool:
        return any(r.action in ("BUY", "SELL") for r in self.selected_results)


# ══════════════════════════════════════════════════════════════
# 编排器配置
# ══════════════════════════════════════════════════════════════

@dataclass
class OrchestratorConfig:
    """StrategyOrchestrator 顶层配置。"""
    policy_config: PolicyConfig = field(default_factory=PolicyConfig)
    gating_config: GatingConfig = field(default_factory=GatingConfig)

    # 性能存储窗口
    perf_window: int = 50
    perf_min_count: int = 5

    # 表现折扣：avg_confidence 低于此值时，权重额外乘以 perf_discount_factor
    perf_discount_threshold: float = 0.45
    perf_discount_factor: float = 0.7

    # 高回撤时全局缩减权重（当前回撤 > max_drawdown_for_full_weight 时触发）
    max_drawdown_for_full_weight: float = 0.15
    drawdown_reduce_factor: float = 0.5


# ══════════════════════════════════════════════════════════════
# 编排器
# ══════════════════════════════════════════════════════════════

class StrategyOrchestrator:
    """
    策略编排器。

    使用示例（在 AlphaRuntime.process_bar 之后调用）：
        orchestrator = StrategyOrchestrator()

        inp = OrchestrationInput(
            regime=regime_state,
            strategy_results=results,
            equity=10000.0,
            current_drawdown=0.05,
            is_regime_stable=detector.is_stable,
            bar_seq=loop_seq,
        )
        decision = orchestrator.orchestrate(inp)

        if not decision.is_blocked:
            for result in decision.selected_results:
                weight = decision.weights.get(result.strategy_id, 1.0)
                # 提交加权订单...

    Args:
        config: OrchestratorConfig 实例
    """

    def __init__(self, config: OrchestratorConfig | None = None) -> None:
        self.config = config or OrchestratorConfig()

        self._gating = GatingEngine(self.config.gating_config)
        self._affinity = AffinityMatrix(self.config.policy_config)
        self._conflict = ConflictResolver(self.config.policy_config)
        self._perf_store = PerformanceStore(
            window=self.config.perf_window,
            min_count=self.config.perf_min_count,
        )

        log.info(
            "[Orchestrator] 初始化完成: perf_window={} perf_min_count={} "
            "max_drawdown={} drawdown_reduce={}",
            self.config.perf_window,
            self.config.perf_min_count,
            self.config.max_drawdown_for_full_weight,
            self.config.drawdown_reduce_factor,
        )

    # ────────────────────────────────────────────────────────────
    # 主接口
    # ────────────────────────────────────────────────────────────

    def orchestrate(self, inp: OrchestrationInput) -> OrchestrationDecision:
        """
        执行完整的编排流程，返回最终决策。

        Args:
            inp: OrchestrationInput 快照

        Returns:
            OrchestrationDecision
        """
        regime = inp.regime
        results = inp.strategy_results
        bar_seq = inp.bar_seq
        block_reasons: List[str] = []

        log.info(
            "[Orchestrator] 开始编排: bar_seq={} regime={} conf={:.3f} "
            "策略数={} equity={:.0f} drawdown={:.2%}",
            bar_seq, regime.dominant_regime, regime.confidence,
            len(results), inp.equity, inp.current_drawdown,
        )

        # ── 记录本次所有策略输出到 PerformanceStore ──────────
        for r in results:
            self._perf_store.record(r, bar_seq=bar_seq)

        # ── Step 1: 门控评估 ──────────────────────────────────
        gating = self._gating.evaluate(regime, is_regime_stable=inp.is_regime_stable)

        if gating.action == GatingAction.BLOCK_ALL:
            block_reasons.extend(gating.triggered_rules)
            log.info(
                "[Orchestrator] 全部阻断: triggered={} bar_seq={}",
                gating.triggered_rules, bar_seq,
            )
            return OrchestrationDecision(
                selected_results=[],
                weights={},
                block_reasons=block_reasons,
                gating=gating,
                debug_payload={"stage": "gating_block_all", "bar_seq": bar_seq},
            )

        # ── Step 2: 按 symbol 分组，逐组冲突解决 ─────────────
        groups: Dict[str, List[StrategyResult]] = {}
        for r in results:
            groups.setdefault(r.symbol, []).append(r)

        resolved_results: List[StrategyResult] = []
        for symbol, group in groups.items():
            resolved, conflict_reasons = self._conflict.resolve(group)
            block_reasons.extend(conflict_reasons)
            resolved_results.extend(resolved)

        # ── Step 3: high_vol BLOCK_BUY 过滤 ──────────────────
        if gating.action == GatingAction.BLOCK_BUY:
            before = len(resolved_results)
            resolved_results = [
                r for r in resolved_results if r.action != "BUY"
            ]
            blocked = before - len(resolved_results)
            if blocked > 0:
                block_reasons.append(f"high_vol 阻断 {blocked} 个 BUY 信号")
                log.debug("[Orchestrator] BLOCK_BUY 过滤: 阻断 {} 个 BUY 信号", blocked)

        # ── Step 4: 计算亲和度权重 + 表现折扣 ────────────────
        weights: Dict[str, float] = {}
        for r in resolved_results:
            # 4a: 亲和度权重
            w = self._affinity.get_weight(r.strategy_id, regime.dominant_regime)

            # 4b: REDUCE 门控缩减
            if gating.action == GatingAction.REDUCE:
                w *= gating.reduce_factor

            # 4c: 表现折扣（历史数据不足时跳过）
            perf = self._perf_store.get_performance(r.strategy_id)
            if perf is not None and perf.avg_confidence < self.config.perf_discount_threshold:
                w *= self.config.perf_discount_factor
                block_reasons.append(
                    f"{r.strategy_id} 表现折扣(avg_conf={perf.avg_confidence:.3f})"
                )
                log.debug(
                    "[Orchestrator] 表现折扣: strategy={} avg_conf={:.3f} discount={}",
                    r.strategy_id, perf.avg_confidence, self.config.perf_discount_factor,
                )

            # 4d: 高回撤缩减
            if inp.current_drawdown > self.config.max_drawdown_for_full_weight:
                w *= self.config.drawdown_reduce_factor
                log.debug(
                    "[Orchestrator] 回撤缩减: drawdown={:.2%} factor={}",
                    inp.current_drawdown, self.config.drawdown_reduce_factor,
                )

            weights[r.strategy_id] = w

        # ── Step 5: 归一化权重 ────────────────────────────────
        total_w = sum(weights.values())
        if total_w > 0:
            weights = {sid: w / total_w for sid, w in weights.items()}

        log.info(
            "[Orchestrator] 编排完成: bar_seq={} 通过策略={}/{} gating={} "
            "有方向信号={} block_reasons={}",
            bar_seq,
            len(resolved_results), len(results),
            gating.action.value,
            sum(1 for r in resolved_results if r.action in ("BUY", "SELL")),
            len(block_reasons),
        )

        for sid, w in weights.items():
            log.debug("[Orchestrator] 权重: strategy={} weight={:.4f}", sid, w)

        return OrchestrationDecision(
            selected_results=resolved_results,
            weights=weights,
            block_reasons=block_reasons,
            gating=gating,
            debug_payload={
                "bar_seq": bar_seq,
                "regime": regime.dominant_regime,
                "regime_conf": regime.confidence,
                "gating_action": gating.action.value,
                "total_input": len(results),
                "total_output": len(resolved_results),
            },
        )

    # ────────────────────────────────────────────────────────────
    # 辅助接口
    # ────────────────────────────────────────────────────────────

    @property
    def performance_store(self) -> PerformanceStore:
        """返回 PerformanceStore 引用（供 health_snapshot 或调试读取）。"""
        return self._perf_store

    def health_snapshot(self) -> dict:
        """返回编排器当前状态的诊断快照。"""
        return {
            "perf_store": self._perf_store.diagnostics(),
            "config": {
                "perf_window": self.config.perf_window,
                "max_drawdown_for_full_weight": self.config.max_drawdown_for_full_weight,
                "perf_discount_threshold": self.config.perf_discount_threshold,
            },
        }
