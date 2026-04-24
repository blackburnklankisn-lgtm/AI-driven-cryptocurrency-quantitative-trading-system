"""
modules/risk/dca_engine.py — W9 DCA（成本平均）加仓规划器

设计说明：
- 在预算约束内规划最多 N 次加仓价格偏移
- 仅在 dominant_regime 与信号置信度满足条件时才允许 DCA
- 输出加仓触发价格偏移比例列表（例如 [-0.02, -0.04] 表示跌 2% / 4% 时加仓）
- 不执行实际下单，只提供价格计划

设计边界：
- DCAEngine 不跟踪当前持仓状态，只做规划
- 预算由 BudgetChecker 独立管理，DCAEngine 只核对传入的 budget_remaining_pct
- 高波动或不确定环境下禁用 DCA，避免"接刀"

日志标签：[DCA]
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class DCAConfig:
    """DCA 引擎超参数配置。"""

    max_dca_levels: int = 2               # 最多几层 DCA
    dca_step_pct: float = 0.02            # 每层加仓价格偏移（2%）
    dca_budget_per_level: float = 0.25    # 每层 DCA 占剩余预算比例（估算用）
    min_budget_remaining_pct: float = 0.3 # 剩余预算低于此比例时禁用 DCA

    # 只在以下 regime 允许 DCA（其他环境视为过于不确定）
    allowed_regimes: list[str] = field(
        default_factory=lambda: ["bull", "sideways"]
    )
    min_confidence_for_dca: float = 0.55  # 信号置信度低于此值时禁用 DCA


class DCAEngine:
    """
    DCA 加仓规划器。

    使用示例：
        dca = DCAEngine(DCAConfig(max_dca_levels=2, dca_step_pct=0.02))
        levels = dca.plan(
            dominant_regime="bull",
            signal_confidence=0.72,
            budget_remaining_pct=0.80,
        )
        # levels = [-0.02, -0.04]
        # 含义：比入场价跌 2% / 跌 4% 时各加一仓

    Args:
        config: DCAConfig 配置
    """

    def __init__(self, config: DCAConfig | None = None) -> None:
        self.config = config or DCAConfig()
        log.info(
            "[DCA] DCAEngine 初始化: max_levels={} step={:.2%} "
            "min_budget={:.2%} allowed_regimes={}",
            self.config.max_dca_levels,
            self.config.dca_step_pct,
            self.config.min_budget_remaining_pct,
            self.config.allowed_regimes,
        )

    def plan(
        self,
        dominant_regime: str,
        signal_confidence: float,
        budget_remaining_pct: float,
    ) -> list[float]:
        """
        规划 DCA 加仓价格偏移列表。

        Args:
            dominant_regime:      当前市场 regime
            signal_confidence:    信号置信度（0~1）
            budget_remaining_pct: 剩余可用预算比例（0~1）

        Returns:
            DCA 价格偏移列表（负数 = 比入场价低多少比例时加仓）
            空列表 = 不开启 DCA
        """
        cfg = self.config

        # ── 条件检查（任一不满足则返回空列表）──────────────────
        if dominant_regime not in cfg.allowed_regimes:
            log.debug(
                "[DCA] 禁用: regime={} 不在允许列表 {}",
                dominant_regime, cfg.allowed_regimes,
            )
            return []

        if signal_confidence < cfg.min_confidence_for_dca:
            log.debug(
                "[DCA] 禁用: signal_confidence={:.3f} < min={:.3f}",
                signal_confidence, cfg.min_confidence_for_dca,
            )
            return []

        if budget_remaining_pct < cfg.min_budget_remaining_pct:
            log.debug(
                "[DCA] 禁用: budget_remaining={:.2%} < min={:.2%}",
                budget_remaining_pct, cfg.min_budget_remaining_pct,
            )
            return []

        # ── 计算层数（预算越充裕，层数越多，不超过 max_dca_levels）──
        budget_span = 1.0 - cfg.min_budget_remaining_pct
        if budget_span <= 0:
            n_levels = cfg.max_dca_levels
        else:
            budget_factor = (budget_remaining_pct - cfg.min_budget_remaining_pct) / budget_span
            n_levels = max(1, round(cfg.max_dca_levels * budget_factor))
            n_levels = min(n_levels, cfg.max_dca_levels)

        levels = [
            round(-(i + 1) * cfg.dca_step_pct, 4)
            for i in range(n_levels)
        ]

        log.debug(
            "[DCA] 规划完成: regime={} conf={:.3f} budget={:.2%} n_levels={} levels={}",
            dominant_regime, signal_confidence, budget_remaining_pct, n_levels, levels,
        )
        return levels

    def max_budget_usage_pct(self, n_levels: int) -> float:
        """
        估算 DCA 最多消耗的预算比例（用于 BudgetChecker 预检）。

        Args:
            n_levels: 计划加仓层数

        Returns:
            预估最大预算占用比例
        """
        return self.config.dca_budget_per_level * n_levels
