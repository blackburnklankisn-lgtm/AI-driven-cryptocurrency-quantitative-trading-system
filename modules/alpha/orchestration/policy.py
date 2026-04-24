"""
modules/alpha/orchestration/policy.py — 策略编排策略规则

设计说明：
- AffinityMatrix: 定义 regime → 策略类型 的亲和度映射（0.0~1.0）
  * 高亲和度策略在当前 regime 下权重放大
  * 低亲和度策略权重压缩（但不一定禁止）
- ConflictResolver: 处理同一 symbol 上多个策略信号冲突
  * 冲突定义：同时有 BUY + SELL 信号
  * 默认策略：保留置信度最高的一个，丢弃其他
- PolicyConfig: 可注入的超参数配置

日志标签：[Policy]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.logger import get_logger
from modules.alpha.contracts.regime_types import RegimeName
from modules.alpha.contracts.strategy_result import StrategyResult

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 亲和度矩阵
# ══════════════════════════════════════════════════════════════

# 默认亲和度表：regime_name -> strategy_type_keyword -> affinity_score
# strategy_type_keyword 会与 strategy_id 做部分匹配（lower case contains）
_DEFAULT_AFFINITY: Dict[str, Dict[str, float]] = {
    "bull": {
        "momentum":   1.0,   # 趋势跟踪策略在牛市强
        "ml":         0.9,   # ML 模型在有方向时效果好
        "ma_cross":   0.8,   # MA Cross 在趋势市有效
        "mean_revert": 0.3,  # 均值回归在趋势市弱
    },
    "bear": {
        "momentum":   0.9,   # 做空方向的动量策略有效
        "ml":         0.85,
        "ma_cross":   0.7,
        "mean_revert": 0.3,
    },
    "sideways": {
        "mean_revert": 1.0,  # 横盘市场均值回归最强
        "momentum":   0.3,   # 动量策略在横盘弱
        "ml":         0.6,
        "ma_cross":   0.4,
    },
    "high_vol": {
        "momentum":   0.5,   # 高波动方向不稳定
        "ml":         0.5,
        "ma_cross":   0.4,
        "mean_revert": 0.4,
    },
    "unknown": {
        "momentum":   0.5,
        "ml":         0.6,   # ML 模型有内生的不确定性建模
        "ma_cross":   0.5,
        "mean_revert": 0.5,
    },
}

# 默认基础权重（若策略 ID 无任何关键词匹配时使用）
_DEFAULT_BASE_AFFINITY = 0.5


@dataclass
class PolicyConfig:
    """策略规则超参数配置。"""

    # 亲和度矩阵（可整体替换）
    affinity_table: Dict[str, Dict[str, float]] = field(
        default_factory=lambda: _DEFAULT_AFFINITY
    )

    # 权重放大上限（affinity * boost_factor 上限）
    max_weight: float = 2.0

    # 权重下限（affinity 过低时的保底权重，设为 0 则可完全屏蔽）
    min_weight: float = 0.1

    # 冲突解决策略：'highest_confidence' | 'hold_on_conflict'
    conflict_resolution: str = "highest_confidence"

    # 置信度低于此值的信号直接过滤（BUY/SELL 降级为 HOLD）
    min_confidence_threshold: float = 0.0   # 0.0 = 不过滤


class AffinityMatrix:
    """
    Regime → 策略亲和度映射。

    使用：
        matrix = AffinityMatrix(config)
        weight = matrix.get_weight("ml_predictor_v2", regime_name="bull")
        # -> 1.8 (affinity=0.9 * 2.0 boost_factor 但受 max_weight 限制)
    """

    def __init__(self, config: PolicyConfig) -> None:
        self._config = config

    def get_weight(
        self,
        strategy_id: str,
        regime_name: RegimeName,
    ) -> float:
        """
        查询 strategy_id 在当前 regime 下的权重系数。

        匹配规则：strategy_id 小写后，对亲和度表的关键词做"包含"匹配，
        取匹配分数最高的关键词对应的 affinity。

        Args:
            strategy_id:  策略 ID（如 "ml_predictor_v2", "ma_cross_btc"）
            regime_name:  当前 dominant regime 名称

        Returns:
            权重系数 [min_weight, max_weight]
        """
        cfg = self._config
        sid_lower = strategy_id.lower()

        # unknown regime 不调整权重
        if regime_name == "unknown":
            return 1.0

        regime_affinities = cfg.affinity_table.get(regime_name, {})
        if not regime_affinities:
            return 1.0

        # 按关键词匹配，取最高匹配分（有匹配则用匹配值，哪怕低于 base）
        best_affinity: float | None = None
        matched_key = None
        for keyword, affinity in regime_affinities.items():
            if keyword in sid_lower:
                if best_affinity is None or affinity > best_affinity:
                    best_affinity = affinity
                    matched_key = keyword

        if best_affinity is None:
            best_affinity = _DEFAULT_BASE_AFFINITY

        # 映射到权重：affinity 0~1 → weight [min_weight, max_weight]
        weight = cfg.min_weight + best_affinity * (cfg.max_weight - cfg.min_weight)
        weight = max(cfg.min_weight, min(weight, cfg.max_weight))

        log.debug(
            "[Policy] 亲和度: strategy={} regime={} keyword={} affinity={:.2f} weight={:.2f}",
            strategy_id, regime_name, matched_key, best_affinity, weight,
        )
        return weight


# ══════════════════════════════════════════════════════════════
# 冲突解决器
# ══════════════════════════════════════════════════════════════

class ConflictResolver:
    """
    处理同一 symbol 上多个策略信号冲突。

    冲突定义：同一 symbol 在同一 bar 内同时有 BUY 和 SELL 信号。
    """

    def __init__(self, config: PolicyConfig) -> None:
        self._config = config

    def resolve(
        self,
        results: List[StrategyResult],
    ) -> tuple[List[StrategyResult], List[str]]:
        """
        处理信号冲突，返回处理后的结果列表和阻断原因列表。

        Args:
            results: 同一 symbol 的多个 StrategyResult

        Returns:
            (resolved_results, block_reasons)
        """
        if not results:
            return [], []

        cfg = self._config

        # 置信度过滤（BUY/SELL 低于阈值降级为 HOLD）
        filtered = []
        block_reasons = []
        for r in results:
            if (
                r.action in ("BUY", "SELL")
                and cfg.min_confidence_threshold > 0
                and r.confidence < cfg.min_confidence_threshold
            ):
                block_reasons.append(
                    f"{r.strategy_id} 置信度不足({r.confidence:.3f}<{cfg.min_confidence_threshold})"
                )
                log.debug(
                    "[Policy] 置信度过滤: strategy={} action={} conf={:.3f}",
                    r.strategy_id, r.action, r.confidence,
                )
            else:
                filtered.append(r)

        if not filtered:
            return [], block_reasons

        # 检测冲突
        buy_results = [r for r in filtered if r.action == "BUY"]
        sell_results = [r for r in filtered if r.action == "SELL"]
        hold_results = [r for r in filtered if r.action == "HOLD"]

        has_conflict = bool(buy_results) and bool(sell_results)

        if not has_conflict:
            return filtered, block_reasons

        # 冲突解决
        if cfg.conflict_resolution == "highest_confidence":
            all_directional = buy_results + sell_results
            winner = max(all_directional, key=lambda r: r.confidence)
            losers = [r for r in all_directional if r is not winner]

            for loser in losers:
                block_reasons.append(
                    f"{loser.strategy_id} 冲突被压制(action={loser.action} conf={loser.confidence:.3f})"
                )

            log.debug(
                "[Policy] 冲突解决: 保留={} action={} conf={:.3f} 压制={} 个",
                winner.strategy_id, winner.action, winner.confidence, len(losers),
            )
            return [winner] + hold_results, block_reasons

        elif cfg.conflict_resolution == "hold_on_conflict":
            block_reasons.append(
                f"BUY/SELL 冲突({len(buy_results)} vs {len(sell_results)})，全部降级为 HOLD"
            )
            log.debug(
                "[Policy] 冲突解决: hold_on_conflict 触发，{} 个方向信号被阻断",
                len(buy_results) + len(sell_results),
            )
            return hold_results, block_reasons

        # 默认：原样返回（不干预）
        return filtered, block_reasons
