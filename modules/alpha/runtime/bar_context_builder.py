from __future__ import annotations

from datetime import timezone
from typing import Any

import pandas as pd

from core.event import KlineEvent
from core.logger import get_logger
from modules.alpha.contracts.regime_types import RegimeState
from modules.alpha.contracts.strategy_context import StrategyContext

log = get_logger(__name__)


class BarContextBuilder:
    """Build StrategyContext with stable trace id for one closed bar."""

    def build(
        self,
        *,
        loop_seq: int,
        event: KlineEvent,
        latest_prices: dict[str, float] | None = None,
        feature_frame: pd.DataFrame | None = None,
        regime: RegimeState | None = None,
        portfolio_snapshot: dict[str, Any] | None = None,
        risk_snapshot: dict[str, Any] | None = None,
        debug_enabled: bool = False,
    ) -> StrategyContext:
        trace_id = self._make_trace_id(loop_seq=loop_seq, event=event)
        ctx = StrategyContext(
            loop_seq=loop_seq,
            trace_id=trace_id,
            symbol=event.symbol,
            timeframe=event.timeframe,
            kline_event=event,
            latest_prices=latest_prices or {},
            feature_frame=feature_frame,
            regime=regime,
            portfolio_snapshot=portfolio_snapshot or {},
            risk_snapshot=risk_snapshot or {},
            debug_enabled=debug_enabled,
        )
        if debug_enabled:
            log.debug(
                "[BarContext] loop={} trace={} symbol={} feature={} regime={}",
                loop_seq,
                trace_id,
                event.symbol,
                feature_frame is not None,
                regime.dominant_regime if regime else "none",
            )
        return ctx

    @staticmethod
    def _make_trace_id(loop_seq: int, event: KlineEvent) -> str:
        ts = event.timestamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        sym = event.symbol.replace("/", "")
        return f"{sym}-{ts}-{loop_seq:06d}"
