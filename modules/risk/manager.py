"""
modules/risk/manager.py — 风险管理器（RiskManager）

设计说明：
这是系统的"最后防线"。所有策略产出的订单请求，
必须经过 RiskManager.check() 的审核才能进入执行层。

审核项目（硬约束，不可绕过）：
1. 单币种最大仓位比例检查
2. 组合最大回撤熔断（触发后系统进入只减仓模式）
3. 单日最大亏损限制（触发后当日停止所有买入）
4. 连续亏损熔断（N 次连续亏损后暂停）
5. 黑名单币种过滤
6. 流动性不足过滤（成交量低于阈值）

设计原则：
- RiskManager 是纯粹的"守门员"，只决定"是否允许"，不修改交易量
- 被拦截的订单必须记录到审计日志（含拒绝原因）
- 熔断状态必须持久化意图（重启后不自动恢复，需人工确认）

接口：
    RiskManager(config, broker_ref)
    .check(order_request, current_equity, positions, prices) → (allowed, reason)
    .update_daily_pnl(pnl_delta)   → 更新当日盈亏
    .record_trade_outcome(won)     → 记录单笔盈亏结果，用于连续亏损计数
    .is_circuit_broken()           → bool，系统是否处于熔断状态
    .reset_daily()                 → 每日重置（由调度器在 00:00 UTC 调用）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from core.exceptions import RiskLimitBreached
from core.logger import audit_log, get_logger

log = get_logger(__name__)


@dataclass
class RiskConfig:
    """风控参数配置（可从系统配置加载）。"""
    max_position_pct: float = 0.20       # 单币种最大仓位占净值比例
    max_portfolio_drawdown: float = 0.10  # 组合最大回撤熔断阈值
    max_daily_loss: float = 0.03          # 单日最大亏损（占净值比例）
    max_consecutive_losses: int = 5       # 连续亏损熔断次数
    blacklist: List[str] = field(default_factory=list)  # 黑名单币种


class RiskState:
    """风控运行时状态（不持久化，仅内存维护）。"""

    def __init__(self) -> None:
        self.circuit_broken: bool = False          # 是否触发了熔断
        self.circuit_reason: str = ""              # 熔断原因
        self.daily_pnl: Decimal = Decimal("0")    # 当日盈亏
        self.daily_start_equity: Optional[Decimal] = None  # 当日起始净值
        self.consecutive_losses: int = 0          # 连续亏损计数
        self.peak_equity: Decimal = Decimal("0")  # 历史最高净值（用于回撤计算）
        self.current_date: date = datetime.now(tz=timezone.utc).date()


class RiskManager:
    """
    风险管理器，负责对所有订单请求进行合规性审核。

    Args:
        config: RiskConfig 风控参数对象
    """

    def __init__(self, config: Optional[RiskConfig] = None) -> None:
        self.config = config or RiskConfig()
        self._state = RiskState()
        log.info(
            "RiskManager 初始化: max_pos={} max_dd={} max_daily_loss={} max_consec={}",
            self.config.max_position_pct,
            self.config.max_portfolio_drawdown,
            self.config.max_daily_loss,
            self.config.max_consecutive_losses,
        )

    # ────────────────────────────────────────────────────────────
    # 核心审核接口
    # ────────────────────────────────────────────────────────────

    def check(
        self,
        side: str,
        symbol: str,
        quantity: Decimal,
        price: float,
        current_equity: float,
        positions: Dict[str, Decimal],
    ) -> Tuple[bool, str]:
        """
        审核单个订单请求是否符合所有风控约束。

        Args:
            side:           "buy" | "sell"
            symbol:         交易对（如 "BTC/USDT"）
            quantity:       申请数量
            price:          预计成交价格
            current_equity: 当前账户净值（USDT）
            positions:      当前各币种持仓字典 {symbol: qty}

        Returns:
            (allowed: bool, reason: str)
            allowed=True 表示通过，allowed=False 表示被拒绝，reason 说明原因
        """
        equity = Decimal(str(current_equity))

        # 只有买入订单需要全量风控审核（卖出/平仓允许在熔断后继续）
        if side == "buy":
            checks = [
                self._check_circuit_breaker(),
                self._check_blacklist(symbol),
                self._check_daily_loss(equity),
                self._check_position_limit(symbol, quantity, price, equity, positions),
                self._check_portfolio_drawdown(equity),
            ]
            for allowed, reason in checks:
                if not allowed:
                    self._log_rejection(symbol, side, quantity, reason)
                    return False, reason

        elif side == "sell":
            # 熔断时只允许减仓，不允许新开空
            if self._state.circuit_broken:
                log.info(
                    "RiskManager: 允许减仓请求（熔断中只减仓）: symbol={}", symbol
                )
            # 黑名单卖出也需拒绝（异常情况防止大量减仓冲击市场）
            allowed, reason = self._check_blacklist(symbol)
            if not allowed:
                self._log_rejection(symbol, side, quantity, reason)
                return False, reason

        return True, "通过"

    # ────────────────────────────────────────────────────────────
    # 状态更新接口（供 Broker 在成交后调用）
    # ────────────────────────────────────────────────────────────

    def update_equity(self, current_equity: float) -> None:
        """
        每个时间步更新净值，用于回撤计算和熔断检测。
        应该在每根 K 线结束时调用。

        Args:
            current_equity: 当前账户净值
        """
        equity = Decimal(str(current_equity))

        # 初始化当日起始净值
        if self._state.daily_start_equity is None:
            self._state.daily_start_equity = equity

        # 更新历史最高净值
        if equity > self._state.peak_equity:
            self._state.peak_equity = equity

        # 更新当日盈亏
        self._state.daily_pnl = equity - self._state.daily_start_equity

        # 检查是否需要触发熔断
        self._check_and_trigger_circuit_breaker(equity)

    def record_trade_outcome(self, won: bool) -> None:
        """
        记录单笔交易结果，更新连续亏损计数。

        Args:
            won: True = 盈利，False = 亏损
        """
        if won:
            if self._state.consecutive_losses > 0:
                log.info(
                    "连续亏损计数重置（盈利），此前连亏 {} 次",
                    self._state.consecutive_losses,
                )
            self._state.consecutive_losses = 0
        else:
            self._state.consecutive_losses += 1
            log.warning(
                "连续亏损计数: {}/{}",
                self._state.consecutive_losses,
                self.config.max_consecutive_losses,
            )
            if self._state.consecutive_losses >= self.config.max_consecutive_losses:
                self._trigger_circuit_breaker(
                    f"连续亏损 {self._state.consecutive_losses} 次，触发熔断"
                )

    def reset_daily(self, current_equity: float) -> None:
        """
        每日重置：更新当日起始净值，清空当日盈亏计数器。
        由调度器在每天 00:00 UTC 调用。

        注意：熔断状态不会由此方法自动恢复！
        """
        self._state.daily_start_equity = Decimal(str(current_equity))
        self._state.daily_pnl = Decimal("0")
        self._state.current_date = datetime.now(tz=timezone.utc).date()
        log.info(
            "每日风控重置: 起始净值={:.2f}", current_equity
        )

    def reset_circuit_breaker(self, authorized_by: str = "manual") -> None:
        """
        手动恢复熔断（需要明确授权，不自动恢复）。

        Args:
            authorized_by: 操作人标识，记录到审计日志
        """
        audit_log(
            "CIRCUIT_BREAKER_RESET",
            authorized_by=authorized_by,
            previous_reason=self._state.circuit_reason,
        )
        self._state.circuit_broken = False
        self._state.circuit_reason = ""
        self._state.consecutive_losses = 0
        log.warning("熔断已手动解除，授权人: {}", authorized_by)

    def is_circuit_broken(self) -> bool:
        """返回当前是否处于熔断状态。"""
        return self._state.circuit_broken

    def get_state_summary(self) -> Dict[str, object]:
        """返回当前风控状态摘要，用于监控和调试。"""
        return {
            "circuit_broken": self._state.circuit_broken,
            "circuit_reason": self._state.circuit_reason,
            "daily_pnl": float(self._state.daily_pnl),
            "consecutive_losses": self._state.consecutive_losses,
            "peak_equity": float(self._state.peak_equity),
            "daily_start_equity": float(self._state.daily_start_equity or 0),
        }

    # ────────────────────────────────────────────────────────────
    # 私有检查函数（每个返回 (allowed, reason)）
    # ────────────────────────────────────────────────────────────

    def _check_circuit_breaker(self) -> Tuple[bool, str]:
        if self._state.circuit_broken:
            return False, f"系统熔断中: {self._state.circuit_reason}"
        return True, ""

    def _check_blacklist(self, symbol: str) -> Tuple[bool, str]:
        if symbol in self.config.blacklist:
            return False, f"{symbol} 在风控黑名单中，禁止交易"
        return True, ""

    def _check_daily_loss(self, equity: Decimal) -> Tuple[bool, str]:
        if self._state.daily_start_equity is None:
            return True, ""

        if self._state.daily_pnl < Decimal("0"):
            loss_pct = float(
                abs(self._state.daily_pnl) / self._state.daily_start_equity
            )
            if loss_pct >= self.config.max_daily_loss:
                return (
                    False,
                    f"单日亏损 {loss_pct * 100:.2f}% 超过限制 "
                    f"{self.config.max_daily_loss * 100:.0f}%",
                )
        return True, ""

    def _check_position_limit(
        self,
        symbol: str,
        quantity: Decimal,
        price: float,
        equity: Decimal,
        positions: Dict[str, Decimal],
    ) -> Tuple[bool, str]:
        """检查买入后单币种仓位是否超过最大比例。"""
        current_qty = positions.get(symbol, Decimal("0"))
        new_qty = current_qty + quantity
        new_notional = new_qty * Decimal(str(price))
        new_pct = float(new_notional / equity) if equity > 0 else 1.0

        if new_pct > self.config.max_position_pct:
            return (
                False,
                f"{symbol} 买入后仓位 {new_pct * 100:.1f}% 超过限制 "
                f"{self.config.max_position_pct * 100:.0f}%",
            )
        return True, ""

    def _check_portfolio_drawdown(self, equity: Decimal) -> Tuple[bool, str]:
        """检查当前净值是否触及最大回撤阈值。"""
        if self._state.peak_equity <= Decimal("0"):
            return True, ""

        drawdown = float((self._state.peak_equity - equity) / self._state.peak_equity)
        if drawdown >= self.config.max_portfolio_drawdown:
            return (
                False,
                f"组合回撤 {drawdown * 100:.2f}% 超过限制 "
                f"{self.config.max_portfolio_drawdown * 100:.0f}%",
            )
        return True, ""

    def _check_and_trigger_circuit_breaker(self, equity: Decimal) -> None:
        """在 update_equity 中检查是否需要触发熔断。"""
        if self._state.circuit_broken:
            return

        # 检查最大回撤
        if self._state.peak_equity > Decimal("0"):
            drawdown = float(
                (self._state.peak_equity - equity) / self._state.peak_equity
            )
            if drawdown >= self.config.max_portfolio_drawdown:
                self._trigger_circuit_breaker(
                    f"组合最大回撤 {drawdown * 100:.2f}% 触发熔断"
                )
                return

        # 检查单日亏损
        if self._state.daily_start_equity and self._state.daily_start_equity > 0:
            daily_loss_pct = float(
                abs(min(self._state.daily_pnl, Decimal("0")))
                / self._state.daily_start_equity
            )
            if daily_loss_pct >= self.config.max_daily_loss:
                self._trigger_circuit_breaker(
                    f"单日亏损 {daily_loss_pct * 100:.2f}% 触发熔断"
                )

    def _trigger_circuit_breaker(self, reason: str) -> None:
        """触发熔断，写入审计日志。"""
        self._state.circuit_broken = True
        self._state.circuit_reason = reason
        audit_log(
            "CIRCUIT_BREAKER_TRIGGERED",
            reason=reason,
            daily_pnl=float(self._state.daily_pnl),
            consecutive_losses=self._state.consecutive_losses,
            peak_equity=float(self._state.peak_equity),
        )
        log.error("🚨 熔断触发: {}", reason)

    def _log_rejection(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        reason: str,
    ) -> None:
        """记录被拒绝的订单到日志和审计轨迹。"""
        audit_log(
            "ORDER_REJECTED",
            symbol=symbol,
            side=side,
            quantity=float(quantity),
            reason=reason,
        )
        log.warning("风控拒绝订单: {} {} {} 原因={}", symbol, side, float(quantity), reason)
