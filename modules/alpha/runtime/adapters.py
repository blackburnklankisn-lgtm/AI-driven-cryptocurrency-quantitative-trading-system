from __future__ import annotations

from typing import Any

from core.logger import get_logger
from modules.alpha.base import BaseAlpha
from modules.alpha.contracts.strategy_context import StrategyContext
from modules.alpha.contracts.strategy_result import StrategyResult

log = get_logger(__name__)


class BaseAlphaAdapter:
    """Adapter to run legacy BaseAlpha strategies under Phase 1 protocol."""

    def __init__(self, strategy: BaseAlpha) -> None:
        self._strategy = strategy
        self.strategy_id = strategy.strategy_id
        self.symbol = strategy.symbol
        self.timeframe = strategy.timeframe

    def on_bar(self, context: StrategyContext) -> StrategyResult:
        orders = self._strategy.on_kline(context.kline_event)

        action = "HOLD"
        confidence = 0.0
        score = 0.0
        if orders:
            first_side = orders[0].side.lower()
            if first_side == "buy":
                action = "BUY"
                confidence = 1.0
                score = 1.0
            elif first_side == "sell":
                action = "SELL"
                confidence = 1.0
                score = -1.0

        return StrategyResult(
            strategy_id=self.strategy_id,
            symbol=self.symbol,
            action=action,  # type: ignore[arg-type]
            confidence=confidence,
            score=score,
            reason_codes=["legacy_base_alpha_adapter"],
            order_requests=orders,
            debug_payload={
                "order_count": len(orders),
                "bar_count": getattr(self._strategy, "_bar_count", 0),
            },
        )

    def sync_position(self, quantity: float) -> None:
        if hasattr(self._strategy, "sync_position"):
            try:
                self._strategy.sync_position(quantity)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                log.warning("[AlphaAdapter] sync_position failed: {} error={}", self.strategy_id, exc)

    def health_snapshot(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bar_count": getattr(self._strategy, "_bar_count", 0),
        }
