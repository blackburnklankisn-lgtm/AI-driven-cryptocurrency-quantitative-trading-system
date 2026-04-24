from __future__ import annotations

from core.logger import get_logger
from modules.alpha.contracts.strategy_context import StrategyContext
from modules.alpha.contracts.strategy_protocol import StrategyProtocol
from modules.alpha.contracts.strategy_result import StrategyResult

log = get_logger(__name__)


class SignalPipeline:
    """Execute strategy list for one context and collect StrategyResult."""

    def run(self, context: StrategyContext, strategies: list[StrategyProtocol]) -> list[StrategyResult]:
        results: list[StrategyResult] = []
        for strategy in strategies:
            try:
                result = strategy.on_bar(context)
                results.append(result)
                if context.debug_enabled:
                    log.debug(
                        "[SignalPipeline] trace={} strategy={} action={} conf={:.3f} orders={}",
                        context.trace_id,
                        result.strategy_id,
                        result.action,
                        result.confidence,
                        len(result.order_requests),
                    )
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "[SignalPipeline] strategy failed: trace={} strategy={} error={}",
                    context.trace_id,
                    strategy.strategy_id,
                    exc,
                )
        return results
