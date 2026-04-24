from __future__ import annotations

from typing import Any

import pandas as pd

from core.event import KlineEvent, OrderRequestEvent
from core.logger import get_logger
from modules.alpha.contracts.regime_types import RegimeState
from modules.alpha.contracts.strategy_context import StrategyContext
from modules.alpha.contracts.strategy_result import StrategyResult
from modules.alpha.runtime.bar_context_builder import BarContextBuilder
from modules.alpha.runtime.signal_pipeline import SignalPipeline
from modules.alpha.runtime.strategy_registry import StrategyRegistry
from modules.alpha.runtime.trace_recorder import TraceRecorder

log = get_logger(__name__)


class AlphaRuntime:
    """Phase 1 runtime coordinator for context->strategy->result->trace flow."""

    def __init__(
        self,
        registry: StrategyRegistry,
        *,
        context_builder: BarContextBuilder | None = None,
        signal_pipeline: SignalPipeline | None = None,
        trace_recorder: TraceRecorder | None = None,
        debug_enabled: bool = False,
    ) -> None:
        self.registry = registry
        self.context_builder = context_builder or BarContextBuilder()
        self.signal_pipeline = signal_pipeline or SignalPipeline()
        self.trace_recorder = trace_recorder or TraceRecorder(enabled=False)
        self.debug_enabled = debug_enabled
        self.loop_seq = 0

        log.info("[AlphaRuntime] initialized: strategies={}", len(self.registry))

    def process_bar(
        self,
        *,
        event: KlineEvent,
        latest_prices: dict[str, float] | None = None,
        feature_frame: pd.DataFrame | None = None,
        regime: RegimeState | None = None,
        portfolio_snapshot: dict[str, Any] | None = None,
        risk_snapshot: dict[str, Any] | None = None,
    ) -> tuple[StrategyContext, list[StrategyResult]]:
        self.loop_seq += 1

        context = self.context_builder.build(
            loop_seq=self.loop_seq,
            event=event,
            latest_prices=latest_prices,
            feature_frame=feature_frame,
            regime=regime,
            portfolio_snapshot=portfolio_snapshot,
            risk_snapshot=risk_snapshot,
            debug_enabled=self.debug_enabled,
        )

        strategies = self.registry.get_for_symbol(event.symbol, enabled_only=True)
        results = self.signal_pipeline.run(context, strategies)

        self.trace_recorder.record(
            context=context,
            results=results,
            regime=regime,
        )

        if self.debug_enabled:
            log.debug(
                "[AlphaRuntime] trace={} symbol={} strategies={} results={} order_requests={}",
                context.trace_id,
                event.symbol,
                len(strategies),
                len(results),
                sum(len(r.order_requests) for r in results),
            )

        return context, results

    @staticmethod
    def collect_order_requests(results: list[StrategyResult]) -> list[OrderRequestEvent]:
        all_orders: list[OrderRequestEvent] = []
        for result in results:
            all_orders.extend(result.order_requests)
        return all_orders
