"""
modules/alpha/market_making/quote_lifecycle.py — 报价生命周期状态机

设计说明：
- 维护单个交易对的双边（BID + ASK）活跃报价状态
- 状态机：IDLE → PENDING → ACTIVE → PARTIALLY_FILLED → FILLED / CANCELLED / EXPIRED
- 核心职责：
    * 接收 QuoteDecision → 决定 QuoteAction（POST / REFRESH / CANCEL / SKIP / HALT）
    * 处理 FillRecord → 更新 ActiveQuote 状态
    * 检测超时过期（quote age > max_age_sec）
    * 检测 mid 大幅移动（>stale_mid_bps）时触发 REFRESH
- 不直接下单，只输出 QuoteAction 和 reason_codes（MarketMakingStrategy 负责执行）
- 线程安全（threading.RLock）

日志标签：[QuoteLifecycle]
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.alpha.contracts.mm_types import (
    ActiveQuote,
    FillRecord,
    QuoteAction,
    QuoteDecision,
    QuoteSide,
    QuoteState,
)

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class QuoteLifecycleConfig:
    """
    报价生命周期配置。

    Attributes:
        max_quote_age_sec:     报价最大存活时间（秒），超过后触发 REFRESH/CANCEL
        stale_mid_bps:         mid price 移动超过此 bps 时触发 REFRESH
        min_refresh_interval_sec: 最小 REFRESH 间隔（防止过于频繁改单）
        cancel_on_gap:         订单簿 gap 时是否立即撤单
        halt_on_kill_switch:   Kill Switch 激活时是否立即撤单
    """

    max_quote_age_sec: float = 10.0
    stale_mid_bps: float = 5.0
    min_refresh_interval_sec: float = 1.0
    cancel_on_gap: bool = True
    halt_on_kill_switch: bool = True


# ══════════════════════════════════════════════════════════════
# 二、QuoteLifecycle 主体
# ══════════════════════════════════════════════════════════════

class QuoteLifecycle:
    """
    单交易对报价生命周期管理器。

    维护 bid_quote 和 ask_quote 两个活跃报价状态机。

    线程安全：所有写操作持 RLock
    """

    def __init__(
        self,
        symbol: str,
        config: Optional[QuoteLifecycleConfig] = None,
    ) -> None:
        self.symbol = symbol
        self.config = config or QuoteLifecycleConfig()
        self._lock = threading.RLock()

        self._bid_quote: Optional[ActiveQuote] = None
        self._ask_quote: Optional[ActiveQuote] = None
        self._last_mid: float = 0.0
        self._last_refresh_at: Optional[datetime] = None
        self._total_posts: int = 0
        self._total_cancels: int = 0
        self._total_fills: int = 0
        self._total_refreshes: int = 0

        log.info(
            "[QuoteLifecycle] 初始化: symbol={} max_age={}s stale_mid={}bps",
            symbol,
            self.config.max_quote_age_sec,
            self.config.stale_mid_bps,
        )

    # ──────────────────────────────────────────────────────────
    # 决策接口
    # ──────────────────────────────────────────────────────────

    def evaluate(
        self,
        decision: QuoteDecision,
        current_mid: float,
        kill_switch_active: bool = False,
        book_gap: bool = False,
    ) -> dict[QuoteSide, tuple[QuoteAction, Optional[ActiveQuote], list[str]]]:
        """
        评估当前报价状态，决定每侧的 QuoteAction。

        Args:
            decision:            QuoteEngine 产出的最新 QuoteDecision
            current_mid:         当前 mid price
            kill_switch_active:  是否已激活 Kill Switch
            book_gap:            订单簿是否处于 gap 状态

        Returns:
            dict[QuoteSide → (QuoteAction, ActiveQuote or None, reason_codes)]
            ActiveQuote 为新建报价（POST/REFRESH）或 None（CANCEL/SKIP/HALT）
        """
        with self._lock:
            now = datetime.now(tz=timezone.utc)
            result: dict[QuoteSide, tuple[QuoteAction, Optional[ActiveQuote], list[str]]] = {}

            for side in (QuoteSide.BID, QuoteSide.ASK):
                action, new_quote, reasons = self._evaluate_side(
                    side, decision, current_mid, now,
                    kill_switch_active, book_gap,
                )
                result[side] = (action, new_quote, reasons)

            self._last_mid = current_mid
            return result

    def on_fill(self, fill: FillRecord) -> None:
        """
        处理 fill 回报，更新对应侧报价状态。

        Args:
            fill: FillRecord（来自 FillSimulator 或真实成交回报）
        """
        with self._lock:
            quote = self._get_quote(fill.side)
            if quote is None or quote.quote_id != fill.quote_id:
                log.debug(
                    "[QuoteLifecycle] Fill 忽略（quote 不匹配）: "
                    "fill_quote_id={} current_quote_id={}",
                    fill.quote_id,
                    quote.quote_id if quote else "None",
                )
                return

            quote.fills.append(fill)
            quote.remaining_size = max(0.0, quote.remaining_size - fill.fill_qty)
            quote.last_updated_at = fill.filled_at
            self._total_fills += 1

            if quote.remaining_size <= 1e-10:
                quote.state = QuoteState.FILLED
                log.info(
                    "[QuoteLifecycle] 报价全额成交: symbol={} side={} "
                    "quote_id={} fill_price={:.4f} fill_qty={:.6f}",
                    self.symbol, fill.side.value, fill.quote_id,
                    fill.fill_price, fill.fill_qty,
                )
                self._set_quote(fill.side, None)  # 清除报价
            else:
                quote.state = QuoteState.PARTIALLY_FILLED
                log.debug(
                    "[QuoteLifecycle] 报价部分成交: symbol={} side={} "
                    "quote_id={} remaining={:.6f} filled_pct={:.2%}",
                    self.symbol, fill.side.value, fill.quote_id,
                    quote.remaining_size, quote.filled_pct(),
                )

    def on_posted(self, side: QuoteSide, quote_id: str) -> None:
        """报价已提交到交易所（从 PENDING → ACTIVE）。"""
        with self._lock:
            quote = self._get_quote(side)
            if quote and quote.quote_id == quote_id and quote.state == QuoteState.PENDING:
                quote.state = QuoteState.ACTIVE
                quote.last_updated_at = datetime.now(tz=timezone.utc)
                log.debug(
                    "[QuoteLifecycle] 报价已激活: symbol={} side={} quote_id={}",
                    self.symbol, side.value, quote_id,
                )

    def on_cancelled(self, side: QuoteSide, quote_id: str) -> None:
        """报价已被撤销（更新状态并清除）。"""
        with self._lock:
            quote = self._get_quote(side)
            if quote and quote.quote_id == quote_id:
                quote.state = QuoteState.CANCELLED
                quote.last_updated_at = datetime.now(tz=timezone.utc)
                log.debug(
                    "[QuoteLifecycle] 报价已撤销: symbol={} side={} quote_id={}",
                    self.symbol, side.value, quote_id,
                )
                self._set_quote(side, None)

    # ──────────────────────────────────────────────────────────
    # 状态查询
    # ──────────────────────────────────────────────────────────

    def active_quotes(self) -> list[ActiveQuote]:
        """返回当前所有存活的报价。"""
        with self._lock:
            result = []
            for q in (self._bid_quote, self._ask_quote):
                if q is not None and q.is_alive():
                    result.append(q)
            return result

    def get_quote(self, side: QuoteSide) -> Optional[ActiveQuote]:
        """获取指定侧的活跃报价（None = 无活跃报价）。"""
        with self._lock:
            return self._get_quote(side)

    def reset(self) -> None:
        """清空所有活跃报价状态（重连或策略重启时调用）。"""
        with self._lock:
            self._bid_quote = None
            self._ask_quote = None
            self._last_refresh_at = None
            self._last_mid = 0.0
        log.info("[QuoteLifecycle] 已重置: symbol={}", self.symbol)

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "symbol": self.symbol,
                "bid_quote_id": self._bid_quote.quote_id if self._bid_quote else None,
                "ask_quote_id": self._ask_quote.quote_id if self._ask_quote else None,
                "bid_state": self._bid_quote.state.value if self._bid_quote else "none",
                "ask_state": self._ask_quote.state.value if self._ask_quote else "none",
                "total_posts": self._total_posts,
                "total_cancels": self._total_cancels,
                "total_fills": self._total_fills,
                "total_refreshes": self._total_refreshes,
                "last_mid": self._last_mid,
            }

    # ──────────────────────────────────────────────────────────
    # 内部评估逻辑
    # ──────────────────────────────────────────────────────────

    def _evaluate_side(
        self,
        side: QuoteSide,
        decision: QuoteDecision,
        current_mid: float,
        now: datetime,
        kill_switch_active: bool,
        book_gap: bool,
    ) -> tuple[QuoteAction, Optional[ActiveQuote], list[str]]:
        """评估单侧报价的 QuoteAction。"""
        reasons: list[str] = []
        current_quote = self._get_quote(side)

        # ── Kill Switch 优先
        if kill_switch_active and self.config.halt_on_kill_switch:
            if current_quote and current_quote.is_alive():
                reasons.append("KILL_SWITCH_HALT")
                log.warning(
                    "[QuoteLifecycle] Kill Switch 激活，撤销 {} 报价: symbol={} quote_id={}",
                    side.value, self.symbol, current_quote.quote_id,
                )
                self._total_cancels += 1
                return QuoteAction.HALT, None, reasons
            reasons.append("KILL_SWITCH_SKIP")
            return QuoteAction.SKIP, None, reasons

        # ── 订单簿 gap 优先
        if book_gap and self.config.cancel_on_gap:
            if current_quote and current_quote.is_alive():
                reasons.append("BOOK_GAP_CANCEL")
                log.warning(
                    "[QuoteLifecycle] 订单簿 gap，撤销 {} 报价: symbol={} quote_id={}",
                    side.value, self.symbol, current_quote.quote_id,
                )
                self._total_cancels += 1
                return QuoteAction.CANCEL, None, reasons
            return QuoteAction.SKIP, None, reasons

        # ── 本轮不允许挂此侧
        allow = decision.allow_post_bid if side == QuoteSide.BID else decision.allow_post_ask
        if not allow:
            if current_quote and current_quote.is_alive():
                # 有活跃报价但本轮不允许 → 撤单
                reasons.append("SIDE_DISABLED_CANCEL")
                self._total_cancels += 1
                return QuoteAction.CANCEL, None, reasons
            reasons.append("SIDE_DISABLED_SKIP")
            return QuoteAction.SKIP, None, reasons

        target_price = decision.bid_price if side == QuoteSide.BID else decision.ask_price
        target_size = decision.bid_size if side == QuoteSide.BID else decision.ask_size

        if target_price is None or target_size is None:
            reasons.append("NO_QUOTE_PRICE_SKIP")
            return QuoteAction.SKIP, None, reasons

        # ── 检查 min_refresh_interval
        if self._last_refresh_at is not None:
            elapsed = (now - self._last_refresh_at).total_seconds()
            if elapsed < self.config.min_refresh_interval_sec:
                reasons.append("MIN_REFRESH_INTERVAL")
                return QuoteAction.SKIP, None, reasons

        # ── 检查现有报价是否需要 REFRESH
        if current_quote is not None and current_quote.is_alive():
            # 超时检查
            age = current_quote.age_sec(now)
            if age > self.config.max_quote_age_sec:
                reasons.append(f"QUOTE_EXPIRED_AGE={age:.1f}s")
                return self._do_refresh(side, target_price, target_size, now, reasons)

            # mid 大幅移动检查
            if self._last_mid > 0 and current_mid > 0:
                mid_move_bps = abs(current_mid - self._last_mid) / self._last_mid * 10000
                if mid_move_bps > self.config.stale_mid_bps:
                    reasons.append(f"STALE_MID_BPS={mid_move_bps:.1f}")
                    return self._do_refresh(side, target_price, target_size, now, reasons)

            # 价格漂移检查（新 target 与当前报价差距超阈值）
            price_diff_bps = abs(target_price - current_quote.price) / max(current_quote.price, 1e-10) * 10000
            if price_diff_bps > self.config.stale_mid_bps:
                reasons.append(f"PRICE_DRIFT_BPS={price_diff_bps:.1f}")
                return self._do_refresh(side, target_price, target_size, now, reasons)

            # 当前报价有效，跳过
            reasons.append("QUOTE_VALID_SKIP")
            return QuoteAction.SKIP, None, reasons

        # ── 无活跃报价 → 挂新单
        new_quote = self._create_quote(side, target_price, target_size, now)
        reasons.append("NEW_QUOTE_POST")
        self._total_posts += 1
        log.debug(
            "[QuoteLifecycle] 新报价: symbol={} side={} price={} size={} quote_id={}",
            self.symbol, side.value, target_price, target_size, new_quote.quote_id,
        )
        return QuoteAction.POST, new_quote, reasons

    def _do_refresh(
        self,
        side: QuoteSide,
        target_price: float,
        target_size: float,
        now: datetime,
        reasons: list[str],
    ) -> tuple[QuoteAction, Optional[ActiveQuote], list[str]]:
        """执行 REFRESH：撤旧报价 + 挂新报价。"""
        self._total_refreshes += 1
        self._last_refresh_at = now
        new_quote = self._create_quote(side, target_price, target_size, now)
        reasons.append("REFRESH")
        log.debug(
            "[QuoteLifecycle] REFRESH: symbol={} side={} price={} size={} "
            "new_quote_id={} reasons={}",
            self.symbol, side.value, target_price, target_size,
            new_quote.quote_id, reasons,
        )
        return QuoteAction.REFRESH, new_quote, reasons

    def _create_quote(
        self,
        side: QuoteSide,
        price: float,
        size: float,
        now: datetime,
    ) -> ActiveQuote:
        """创建新 ActiveQuote 并注册为当前侧报价。"""
        quote_id = f"{self.symbol}-{side.value}-{uuid.uuid4().hex[:8]}"
        quote = ActiveQuote(
            quote_id=quote_id,
            symbol=self.symbol,
            side=side,
            price=price,
            original_size=size,
            remaining_size=size,
            state=QuoteState.PENDING,
            posted_at=now,
        )
        self._set_quote(side, quote)
        return quote

    def _get_quote(self, side: QuoteSide) -> Optional[ActiveQuote]:
        return self._bid_quote if side == QuoteSide.BID else self._ask_quote

    def _set_quote(self, side: QuoteSide, quote: Optional[ActiveQuote]) -> None:
        if side == QuoteSide.BID:
            self._bid_quote = quote
        else:
            self._ask_quote = quote
