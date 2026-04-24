from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from core.logger import get_logger
from modules.alpha.contracts.strategy_protocol import StrategyProtocol

log = get_logger(__name__)


@dataclass
class StrategyEntry:
    strategy: StrategyProtocol
    enabled: bool = True


class StrategyRegistry:
    """Registry for strategy lifecycle and lookup."""

    def __init__(self) -> None:
        self._entries: dict[str, StrategyEntry] = {}

    def register(self, strategy: StrategyProtocol, enabled: bool = True) -> bool:
        sid = strategy.strategy_id
        if sid in self._entries:
            log.warning("[StrategyRegistry] duplicate register ignored: {}", sid)
            return False
        self._entries[sid] = StrategyEntry(strategy=strategy, enabled=enabled)
        log.info(
            "[StrategyRegistry] registered: id={} symbol={} timeframe={} enabled={}",
            sid,
            strategy.symbol,
            strategy.timeframe,
            enabled,
        )
        return True

    def unregister(self, strategy_id: str) -> bool:
        if strategy_id not in self._entries:
            return False
        self._entries.pop(strategy_id)
        log.info("[StrategyRegistry] unregistered: {}", strategy_id)
        return True

    def set_enabled(self, strategy_id: str, enabled: bool) -> bool:
        entry = self._entries.get(strategy_id)
        if entry is None:
            return False
        entry.enabled = enabled
        log.info("[StrategyRegistry] set_enabled: {} -> {}", strategy_id, enabled)
        return True

    def get(self, strategy_id: str) -> StrategyProtocol | None:
        entry = self._entries.get(strategy_id)
        return entry.strategy if entry else None

    def get_for_symbol(self, symbol: str, enabled_only: bool = True) -> list[StrategyProtocol]:
        result: list[StrategyProtocol] = []
        for entry in self._entries.values():
            if entry.strategy.symbol != symbol:
                continue
            if enabled_only and not entry.enabled:
                continue
            result.append(entry.strategy)
        return result

    def all(self, enabled_only: bool = False) -> list[StrategyProtocol]:
        if enabled_only:
            return [e.strategy for e in self._entries.values() if e.enabled]
        return [e.strategy for e in self._entries.values()]

    def health_snapshot(self) -> dict[str, dict]:
        return {
            sid: {
                "enabled": entry.enabled,
                **entry.strategy.health_snapshot(),
            }
            for sid, entry in self._entries.items()
        }

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterable[StrategyProtocol]:
        for entry in self._entries.values():
            yield entry.strategy
