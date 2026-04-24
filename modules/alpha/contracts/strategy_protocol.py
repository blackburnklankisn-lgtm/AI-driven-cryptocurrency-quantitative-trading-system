from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from modules.alpha.contracts.strategy_context import StrategyContext
from modules.alpha.contracts.strategy_result import StrategyResult


@runtime_checkable
class StrategyProtocol(Protocol):
    """Phase 1 strategy contract for runtime decoupling."""

    strategy_id: str
    symbol: str
    timeframe: str

    def on_bar(self, context: StrategyContext) -> StrategyResult:
        """Handle one closed bar and return a structured strategy result."""

    def sync_position(self, quantity: float) -> None:
        """Sync external position state into strategy internal state."""

    def health_snapshot(self) -> dict[str, Any]:
        """Return lightweight health/debug metrics for observability."""
