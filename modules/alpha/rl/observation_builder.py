"""
modules/alpha/rl/observation_builder.py — RL 观测向量构建器

设计说明：
- 聚合 technical / onchain / sentiment / microstructure / risk 多个维度特征
- 输出固定维度的 RLObservation（含 feature_names 支持可解释性）
- 所有特征归一化到 [-1, 1] 或 [0, 1]
- 数据源缺失时降级（用 0.0 填充该维度，freshness_ok=False）
- 线程安全（ObservationBuilder 自身无状态，可并发调用）

特征维度设计（共 24 个，可扩展）：
  [0-4]   technical:     ma_cross, rsi_norm, macd_norm, vol_norm, price_mom_5
  [5-8]   onchain:       nvt_norm, supply_shock, whale_flow, funding_rate_norm
  [9-12]  sentiment:     fear_greed_norm, social_vol_norm, news_score, funding_norm
  [13-17] microstructure: spread_bps_norm, imbalance, micro_price_dev, bid_pressure, ask_pressure
  [18-19] risk:          budget_remaining, drawdown_norm
  [20-21] inventory:     inventory_pct_dev, position_pct
  [22-23] time:          hour_sin, hour_cos（日内周期性）

日志标签：[ObsBuilder]
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.alpha.contracts.rl_types import RLObservation
from modules.risk.snapshot import RiskSnapshot

log = get_logger(__name__)

# 特征名称列表（与特征向量顺序严格对应）
FEATURE_NAMES: list[str] = [
    # technical (0-4)
    "tech_ma_cross",
    "tech_rsi_norm",
    "tech_macd_norm",
    "tech_vol_norm",
    "tech_price_mom_5",
    # onchain (5-8)
    "chain_nvt_norm",
    "chain_supply_shock",
    "chain_whale_flow",
    "chain_funding_norm",
    # sentiment (9-12)
    "sent_fear_greed_norm",
    "sent_social_vol_norm",
    "sent_news_score",
    "sent_funding_norm",
    # microstructure (13-17)
    "micro_spread_bps_norm",
    "micro_imbalance",
    "micro_price_dev",
    "micro_bid_pressure",
    "micro_ask_pressure",
    # risk (18-19)
    "risk_budget_remaining",
    "risk_drawdown_norm",
    # inventory (20-21)
    "inv_pct_dev",
    "pos_pct",
    # time (22-23)
    "time_hour_sin",
    "time_hour_cos",
]

OBS_DIM = len(FEATURE_NAMES)  # 24


@dataclass
class ObservationBuilderConfig:
    """
    ObservationBuilder 配置。

    Attributes:
        target_inventory_pct:  做市目标库存比例（用于计算 deviation）
        spread_norm_max_bps:   spread 归一化上限（超过则截断到 1.0）
        rsi_center:            RSI 中性值（通常 50）
        use_time_features:     是否添加日内时间特征（sin/cos）
    """

    target_inventory_pct: float = 0.5
    spread_norm_max_bps: float = 50.0
    rsi_center: float = 50.0
    use_time_features: bool = True


def _clip(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _safe(v: Optional[float], default: float = 0.0) -> float:
    if v is None or not math.isfinite(v):
        return default
    return float(v)


class ObservationBuilder:
    """
    多维特征聚合器 → 固定维度 RLObservation。

    无状态设计，每次 build() 返回新的 RLObservation。
    缺失数据源会降级填充 0.0，并在 source_freshness 中标记。
    """

    def __init__(self, config: Optional[ObservationBuilderConfig] = None) -> None:
        self.config = config or ObservationBuilderConfig()
        log.info(
            "[ObsBuilder] 初始化: obs_dim={} time_features={}",
            OBS_DIM, self.config.use_time_features,
        )

    def build(
        self,
        symbol: str,
        trace_id: str,
        risk_snapshot: RiskSnapshot,
        technical: Optional[dict[str, Any]] = None,
        onchain: Optional[dict[str, Any]] = None,
        sentiment: Optional[dict[str, Any]] = None,
        microstructure: Optional[dict[str, Any]] = None,
        inventory_pct: float = 0.5,
        position_pct: float = 0.0,
        episode_step: int = 0,
        timestamp: Optional[datetime] = None,
    ) -> RLObservation:
        """
        构建 RLObservation。

        Args:
            symbol:         交易对
            trace_id:       全链路 trace ID
            risk_snapshot:  当前风险状态
            technical:      技术面特征 dict（可选，缺失时填 0）
            onchain:        链上特征 dict（可选）
            sentiment:      情绪特征 dict（可选）
            microstructure: 微观结构特征 dict（可选，来自 feature_builder.py 的 mb_* 字段）
            inventory_pct:  当前库存比例（来自 InventoryManager）
            position_pct:   当前方向仓位比例 ∈ [-1, 1]
            episode_step:   episode 内步数
            timestamp:      观测时间戳

        Returns:
            RLObservation（固定 24 维）
        """
        ts = timestamp or datetime.now(tz=timezone.utc)
        tech = technical or {}
        oc = onchain or {}
        sent = sentiment or {}
        micro = microstructure or {}

        freshness = {
            "technical":     bool(tech),
            "onchain":       bool(oc),
            "sentiment":     bool(sent),
            "microstructure": bool(micro),
        }

        # ── technical (0-4)
        vec: list[float] = [
            _clip(_safe(tech.get("ma_cross"), 0.0)),
            _clip((_safe(tech.get("rsi"), self.config.rsi_center) - self.config.rsi_center) / 50.0),
            _clip(_safe(tech.get("macd_norm"), 0.0)),
            _clip(_safe(tech.get("vol_norm"), 0.0), 0.0, 1.0),
            _clip(_safe(tech.get("price_mom_5"), 0.0)),
        ]

        # ── onchain (5-8)
        vec += [
            _clip(_safe(oc.get("nvt_norm"), 0.0)),
            _clip(_safe(oc.get("supply_shock"), 0.0)),
            _clip(_safe(oc.get("whale_flow"), 0.0)),
            _clip(_safe(oc.get("funding_norm"), 0.0)),
        ]

        # ── sentiment (9-12)
        vec += [
            _clip(_safe(sent.get("fear_greed_norm"), 0.0)),
            _clip(_safe(sent.get("social_vol_norm"), 0.0), 0.0, 1.0),
            _clip(_safe(sent.get("news_score"), 0.0)),
            _clip(_safe(sent.get("funding_norm"), 0.0)),
        ]

        # ── microstructure (13-17)
        raw_spread = _safe(micro.get("mb_spread_bps") or micro.get("spread_bps"), 0.0)
        spread_norm = _clip(raw_spread / max(self.config.spread_norm_max_bps, 1e-6), 0.0, 1.0)
        vec += [
            spread_norm,
            _clip(_safe(micro.get("mb_imbalance") or micro.get("imbalance"), 0.0)),
            _clip(_safe(micro.get("mb_micro_price_dev") or micro.get("micro_price_dev"), 0.0)),
            _clip(_safe(micro.get("mb_bid_pressure") or micro.get("bid_pressure"), 0.5), 0.0, 1.0),
            _clip(_safe(micro.get("mb_ask_pressure") or micro.get("ask_pressure"), 0.5), 0.0, 1.0),
        ]

        # ── risk (18-19)
        vec += [
            _clip(risk_snapshot.budget_remaining_pct, 0.0, 1.0),
            _clip(risk_snapshot.current_drawdown, 0.0, 1.0),
        ]

        # ── inventory (20-21)
        inv_dev = _clip((inventory_pct - self.config.target_inventory_pct) * 2.0)
        vec += [
            inv_dev,
            _clip(position_pct),
        ]

        # ── time (22-23)
        if self.config.use_time_features:
            hour = ts.hour + ts.minute / 60.0
            vec += [
                math.sin(2 * math.pi * hour / 24.0),
                math.cos(2 * math.pi * hour / 24.0),
            ]
        else:
            vec += [0.0, 0.0]

        # 确定 regime 与 risk_mode
        regime = str(tech.get("regime", "unknown"))
        if risk_snapshot.kill_switch_active or risk_snapshot.circuit_broken:
            risk_mode = "blocked"
        elif risk_snapshot.budget_remaining_pct < 0.3:
            risk_mode = "reduced"
        else:
            risk_mode = "normal"

        obs = RLObservation(
            symbol=symbol,
            trace_id=trace_id,
            feature_vector=vec,
            feature_names=FEATURE_NAMES,
            regime=regime,
            risk_mode=risk_mode,
            inventory_pct=inventory_pct,
            position_pct=position_pct,
            source_freshness=freshness,
            timestamp=ts,
            episode_step=episode_step,
            debug_payload={
                "n_fresh_sources": sum(freshness.values()),
                "risk_mode": risk_mode,
            },
        )

        log.debug(
            "[ObsBuilder] 构建 obs: symbol={} dim={} fresh_sources={} risk_mode={}",
            symbol, len(vec), sum(freshness.values()), risk_mode,
        )
        return obs
