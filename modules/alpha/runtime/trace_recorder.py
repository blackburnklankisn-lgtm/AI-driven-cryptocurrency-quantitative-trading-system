from __future__ import annotations

import json
import random
from datetime import timezone
from pathlib import Path
from typing import Any

from core.logger import get_logger
from modules.alpha.contracts.regime_types import RegimeState
from modules.alpha.contracts.strategy_context import StrategyContext
from modules.alpha.contracts.strategy_result import StrategyResult

log = get_logger(__name__)


class TraceRecorder:
    """Write bar-level trace records as JSONL for replay and debugging."""

    def __init__(
        self,
        enabled: bool = True,
        sample_rate: float = 1.0,
        output_path: str = "./logs/phase1_trace.jsonl",
    ) -> None:
        self.enabled = enabled
        self.sample_rate = max(0.0, min(1.0, sample_rate))
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        context: StrategyContext,
        results: list[StrategyResult],
        regime: RegimeState | None,
        orchestration: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        if self.sample_rate < 1.0 and random.random() > self.sample_rate:
            return

        payload = {
            "trace_id": context.trace_id,
            "loop_seq": context.loop_seq,
            "timestamp": context.kline_event.timestamp.astimezone(timezone.utc).isoformat(),
            "symbol": context.symbol,
            "timeframe": context.timeframe,
            "regime": {
                "dominant": regime.dominant_regime,
                "confidence": regime.confidence,
                "bull_prob": regime.bull_prob,
                "bear_prob": regime.bear_prob,
                "sideways_prob": regime.sideways_prob,
                "high_vol_prob": regime.high_vol_prob,
            } if regime else None,
            "results": [
                {
                    "strategy_id": r.strategy_id,
                    "action": r.action,
                    "confidence": r.confidence,
                    "score": r.score,
                    "reason_codes": r.reason_codes,
                    "order_count": len(r.order_requests),
                }
                for r in results
            ],
            "orchestration": orchestration or {},
        }

        try:
            with self.output_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            log.warning("[TraceRecorder] write failed: {}", exc)
