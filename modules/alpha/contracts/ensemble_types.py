from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelVote:
    """Single model vote in a meta-learner decision."""

    model_name: str
    buy_probability: float
    action: str
    weight: float = 1.0
    debug_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetaSignal:
    """Meta-learner fused output."""

    final_action: str
    final_confidence: float
    dominant_model: str
    model_votes: list[ModelVote] = field(default_factory=list)
    debug_payload: dict[str, Any] = field(default_factory=dict)
