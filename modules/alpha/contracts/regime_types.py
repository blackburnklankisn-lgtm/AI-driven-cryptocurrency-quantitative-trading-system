from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RegimeName = Literal["bull", "bear", "sideways", "high_vol", "unknown"]


@dataclass(frozen=True)
class RegimeState:
    """Lightweight market regime state used by strategy orchestration."""

    bull_prob: float = 0.0
    bear_prob: float = 0.0
    sideways_prob: float = 0.0
    high_vol_prob: float = 0.0
    confidence: float = 0.0
    dominant_regime: RegimeName = "unknown"
