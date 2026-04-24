"""
modules/risk/cooldown.py — W9 冷却期管理器

设计说明：
- 对特定 symbol 设置冷却期，冷却期内禁止新开仓
- 支持自动过期（基于时间 TTL），也支持手动解除
- 线程安全（单线程事件循环下 dict 操作无锁安全）
- 记录冷却历史（最近 N 条），用于诊断和追溯

日志标签：[Cooldown]
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class CooldownRecord:
    """单次冷却记录（用于诊断与历史追溯）。"""
    symbol: str
    reason: str
    start_at: datetime
    expires_at: datetime
    resolved_at: Optional[datetime] = None


class CooldownManager:
    """
    冷却期管理器。

    使用示例：
        cooldown = CooldownManager()
        cooldown.set(symbol="BTC/USDT", minutes=30, reason="止损触发")

        if cooldown.is_cooling(symbol="BTC/USDT"):
            # 跳过信号处理
            ...

        remaining = cooldown.remaining_minutes("BTC/USDT")

    Args:
        history_maxlen: 每个 symbol 保留的最大历史记录条数
    """

    def __init__(self, history_maxlen: int = 50) -> None:
        self._maxlen = history_maxlen
        # symbol -> 过期时间
        self._active: dict[str, datetime] = {}
        # symbol -> deque[CooldownRecord]
        self._history: dict[str, deque] = {}
        log.info("[Cooldown] CooldownManager 初始化: history_maxlen={}", history_maxlen)

    def set(self, symbol: str, minutes: int, reason: str = "") -> None:
        """
        为指定 symbol 设置冷却期。

        如果该 symbol 已在冷却中，新旧冷却期取更长者。

        Args:
            symbol:  交易对符号
            minutes: 冷却期时长（分钟）
            reason:  触发原因（日志/诊断用）
        """
        if minutes <= 0:
            return

        now = datetime.now(tz=timezone.utc)
        new_expiry = now + timedelta(minutes=minutes)

        # 取更长的冷却期（避免新冷却意外缩短旧冷却）
        if symbol in self._active and self._active[symbol] > new_expiry:
            new_expiry = self._active[symbol]

        self._active[symbol] = new_expiry

        record = CooldownRecord(
            symbol=symbol,
            reason=reason,
            start_at=now,
            expires_at=new_expiry,
        )
        if symbol not in self._history:
            self._history[symbol] = deque(maxlen=self._maxlen)
        self._history[symbol].append(record)

        log.info(
            "[Cooldown] 设置冷却: symbol={} minutes={} reason={} expires_at={}",
            symbol, minutes, reason, new_expiry.isoformat(),
        )

    def is_cooling(self, symbol: str) -> bool:
        """判断指定 symbol 是否仍在冷却期内。"""
        if symbol not in self._active:
            return False
        if datetime.now(tz=timezone.utc) >= self._active[symbol]:
            # 已过期，自动清理
            self._expire(symbol)
            return False
        return True

    def remaining_minutes(self, symbol: str) -> float:
        """返回剩余冷却分钟数（已过期或未设置时返回 0.0）。"""
        if not self.is_cooling(symbol):
            return 0.0
        delta = self._active[symbol] - datetime.now(tz=timezone.utc)
        return max(0.0, delta.total_seconds() / 60.0)

    def release(self, symbol: str) -> bool:
        """
        手动解除指定 symbol 的冷却期。

        Returns:
            True 如果该 symbol 确实处于冷却中并已解除，否则 False
        """
        if symbol not in self._active:
            return False
        now = datetime.now(tz=timezone.utc)
        # 标记最新历史记录的解除时间
        if symbol in self._history and self._history[symbol]:
            self._history[symbol][-1].resolved_at = now
        del self._active[symbol]
        log.info("[Cooldown] 手动解除冷却: symbol={}", symbol)
        return True

    def release_all(self) -> int:
        """
        解除所有 symbol 的冷却期（紧急恢复使用）。

        Returns:
            被解除的 symbol 数量
        """
        count = len(self._active)
        now = datetime.now(tz=timezone.utc)
        for symbol in list(self._active.keys()):
            if symbol in self._history and self._history[symbol]:
                self._history[symbol][-1].resolved_at = now
        self._active.clear()
        log.info("[Cooldown] 已解除全部 {} 个冷却", count)
        return count

    def active_symbols(self) -> dict[str, datetime]:
        """
        返回当前所有仍在冷却期内的 symbol 及其过期时间（自动清理过期项）。
        """
        now = datetime.now(tz=timezone.utc)
        expired = [s for s, t in self._active.items() if t <= now]
        for s in expired:
            self._expire(s)
        return dict(self._active)

    def _expire(self, symbol: str) -> None:
        """清理已过期的 symbol 冷却记录。"""
        if symbol in self._active:
            del self._active[symbol]
        log.debug("[Cooldown] 冷却自动过期: symbol={}", symbol)

    def diagnostics(self) -> dict:
        """诊断快照（用于 health_snapshot / 日志输出）。"""
        active = self.active_symbols()
        return {
            "active_count": len(active),
            "active_symbols": {
                s: {
                    "expires_at": t.isoformat(),
                    "remaining_min": round(self.remaining_minutes(s), 2),
                }
                for s, t in active.items()
            },
            "history_symbols": list(self._history.keys()),
        }
