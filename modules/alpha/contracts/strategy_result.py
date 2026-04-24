from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from core.event import OrderRequestEvent


StrategyAction = Literal["BUY", "SELL", "HOLD"]


@dataclass(frozen=True)
class StrategyResult:
    """Unified strategy output that can be orchestrated and traced."""

    strategy_id: str
    symbol: str
    action: StrategyAction
    confidence: float = 0.0
    score: float = 0.0
    reason_codes: list[str] = field(default_factory=list)
    order_requests: list[OrderRequestEvent] = field(default_factory=list)
    debug_payload: dict[str, Any] = field(default_factory=dict)
