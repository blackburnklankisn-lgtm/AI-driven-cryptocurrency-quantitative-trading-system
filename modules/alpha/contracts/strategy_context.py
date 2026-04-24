from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import pandas as pd

from core.event import KlineEvent
from modules.alpha.contracts.regime_types import RegimeState


@dataclass(frozen=True)
class StrategyContext:
    """Unified strategy input context for one bar."""

    loop_seq: int
    trace_id: str
    symbol: str
    timeframe: str
    kline_event: KlineEvent
    latest_prices: Mapping[str, float] = field(default_factory=dict)
    feature_frame: pd.DataFrame | None = None
    regime: RegimeState | None = None
    portfolio_snapshot: Mapping[str, Any] = field(default_factory=dict)
    risk_snapshot: Mapping[str, Any] = field(default_factory=dict)
    debug_enabled: bool = False
