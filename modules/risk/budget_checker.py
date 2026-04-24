"""
modules/risk/budget_checker.py — 下单前预算预检层

设计说明：
- 在 RiskManager.check() 之前运行，负责"预算是否足够"的独立判断
- 职责边界：
    * BudgetChecker 只决定"预算是否足够"，不决定交易方向
    * 不替代 RiskManager 的最后防线角色
    * 不计算仓位大小（那是 PositionSizer + AdaptiveRiskMatrix 的职责）
- 跟踪：
    * 当前账户已部署预算占比（deployed_pct）
    * DCA 专用预留预算（dca_reserved_pct）
    * 每日已用预算比例（intraday_used_pct）
- 调用方在成功下单后应调用 record_order() 更新内部状态

接口：
    BudgetChecker(config, state_store)
    .check(order_value_pct, is_dca=False) → (allowed, reason, usage_after_pct)
    .record_order(order_value_pct, is_dca=False)   # 下单成功后更新状态
    .record_close(release_value_pct, is_dca=False) # 平仓后释放预算
    .reset_daily()                                 # 每日重置（调度器在 00:00 UTC 调用）
    .snapshot() → dict                             # 当前预算状态快照

日志标签：[Budget] [RiskGuard]
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional, Tuple

from core.logger import get_logger
from modules.risk.state_store import StateStore

log = get_logger(__name__)

_STORE_KEY = "budget"


@dataclass
class BudgetConfig:
    """BudgetChecker 全局配置（所有参数均可通过系统配置覆盖）。"""

    # ── 预算上限 ─────────────────────────────────────────────────
    max_budget_usage_pct: float = 0.90      # 总部署预算上限（净值占比，避免 100% 部署）
    max_single_order_budget_pct: float = 0.25  # 单笔订单最大预算占比

    # ── 手续费 / 滑点预留 ────────────────────────────────────────
    fee_reserve_pct: float = 0.003          # 手续费预留（单边，0.3%）
    slippage_reserve_pct: float = 0.001     # 滑点预留（0.1%）

    # ── DCA 专用预算 ─────────────────────────────────────────────
    dca_budget_cap_pct: float = 0.40        # DCA 仓位最大占总预算比例（封顶保护）

    # ── 最小订单规模 ─────────────────────────────────────────────
    min_order_budget_pct: float = 0.005     # 最小有效订单（占净值比），小于此值拒绝

    # ── 日内重置策略 ─────────────────────────────────────────────
    intraday_budget_cap_pct: float = 1.0    # 日内累计预算上限（默认不约束，设为 <1 可限制）

    # ── 版本（用于诊断）─────────────────────────────────────────
    config_version: str = "v1.0"


class BudgetState:
    """
    预算运行时状态。

    注意：这是可变状态，由 BudgetChecker 内部持有，通过锁保护。
    通过 StateStore 持久化，以便重启后恢复。
    """

    __slots__ = (
        "deployed_pct",
        "dca_deployed_pct",
        "intraday_used_pct",
        "current_date",
    )

    def __init__(self) -> None:
        self.deployed_pct: float = 0.0         # 当前已部署预算（占净值比）
        self.dca_deployed_pct: float = 0.0     # DCA 仓位占总预算比
        self.intraday_used_pct: float = 0.0    # 日内累计下单预算
        self.current_date: date = datetime.now(tz=timezone.utc).date()

    def to_dict(self) -> dict[str, Any]:
        return {
            "deployed_pct": self.deployed_pct,
            "dca_deployed_pct": self.dca_deployed_pct,
            "intraday_used_pct": self.intraday_used_pct,
            "current_date": self.current_date.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BudgetState":
        state = cls()
        state.deployed_pct = float(data.get("deployed_pct", 0.0))
        state.dca_deployed_pct = float(data.get("dca_deployed_pct", 0.0))
        state.intraday_used_pct = float(data.get("intraday_used_pct", 0.0))
        date_str = data.get("current_date")
        if date_str:
            state.current_date = date.fromisoformat(str(date_str))
        return state


class BudgetChecker:
    """
    下单前预算预检层。

    在 RiskManager.check() 之前调用，用于验证：
    1. 本次订单是否超过总预算上限
    2. 单笔订单是否超过单笔上限
    3. DCA 订单是否超过 DCA 专用预算封顶
    4. 日内累计是否超过日内预算上限
    5. 手续费 + 滑点预留后是否有效

    Args:
        config:      BudgetConfig 配置对象
        state_store: StateStore 持久化对象（可选，不提供则不持久化）
    """

    def __init__(
        self,
        config: Optional[BudgetConfig] = None,
        state_store: Optional[StateStore] = None,
    ) -> None:
        self.config = config or BudgetConfig()
        self._store = state_store
        self._lock = threading.Lock()
        self._state = self._load_state()
        log.info(
            "[Budget] BudgetChecker 初始化完成: max_usage={:.0%} "
            "max_single={:.0%} dca_cap={:.0%} version={}",
            self.config.max_budget_usage_pct,
            self.config.max_single_order_budget_pct,
            self.config.dca_budget_cap_pct,
            self.config.config_version,
        )

    # ──────────────────────────────────────────────────────────────
    # 状态持久化
    # ──────────────────────────────────────────────────────────────

    def _load_state(self) -> BudgetState:
        """从 StateStore 恢复状态，如不存在则初始化为零值。"""
        if self._store is None:
            return BudgetState()
        data = self._store.load(_STORE_KEY)
        if data is None:
            return BudgetState()
        try:
            state = BudgetState.from_dict(data)
            # 如果存储日期 != 今日，触发日内重置
            today = datetime.now(tz=timezone.utc).date()
            if state.current_date != today:
                log.info("[Budget] 检测到跨日，触发自动日内状态重置")
                state.intraday_used_pct = 0.0
                state.current_date = today
            log.info(
                "[Budget] 从持久化存储恢复状态: deployed={:.1%} dca={:.1%}",
                state.deployed_pct,
                state.dca_deployed_pct,
            )
            return state
        except Exception as exc:
            log.error("[Budget] 恢复状态失败，使用空状态: {}", exc)
            return BudgetState()

    def _persist(self) -> None:
        """将当前状态写入 StateStore（持有锁时调用）。"""
        if self._store is None:
            return
        try:
            self._store.save(_STORE_KEY, self._state.to_dict())
        except Exception as exc:
            log.error("[Budget] 持久化失败: {}", exc)

    # ──────────────────────────────────────────────────────────────
    # 核心检查接口
    # ──────────────────────────────────────────────────────────────

    def check(
        self,
        order_value_pct: float,
        is_dca: bool = False,
    ) -> Tuple[bool, str, float]:
        """
        检查本次订单是否满足预算约束。

        Args:
            order_value_pct: 本次订单金额占账户净值的比例（0~1）
            is_dca:          是否为 DCA 加仓订单

        Returns:
            (allowed, reason, projected_deployed_pct)
            - allowed: True 表示预算充足，可下单
            - reason:  不允许时的拒绝原因；允许时为 "OK"
            - projected_deployed_pct: 下单后的预估已部署比例
        """
        cfg = self.config

        # ── 1. 最小订单规模过滤 ──────────────────────────────────
        if order_value_pct < cfg.min_order_budget_pct:
            reason = (
                f"订单规模 {order_value_pct:.2%} 低于最小阈值 "
                f"{cfg.min_order_budget_pct:.2%}"
            )
            log.debug("[RiskGuard] 预算检查拒绝（规模过小）: {}", reason)
            return False, reason, self._state.deployed_pct

        # ── 2. 单笔上限 ──────────────────────────────────────────
        if order_value_pct > cfg.max_single_order_budget_pct:
            reason = (
                f"单笔订单 {order_value_pct:.2%} 超过单笔上限 "
                f"{cfg.max_single_order_budget_pct:.2%}"
            )
            log.debug("[RiskGuard] 预算检查拒绝（单笔超限）: {}", reason)
            return False, reason, self._state.deployed_pct

        with self._lock:
            state = self._state

            # ── 3. 手续费 + 滑点预留后的有效订单规模 ─────────────
            effective_cost = order_value_pct * (
                1 + cfg.fee_reserve_pct + cfg.slippage_reserve_pct
            )

            # ── 4. 总预算上限 ────────────────────────────────────
            projected_total = state.deployed_pct + effective_cost
            if projected_total > cfg.max_budget_usage_pct:
                reason = (
                    f"预算不足：当前已部署 {state.deployed_pct:.2%}，"
                    f"本次预计占用 {effective_cost:.2%}（含手续费/滑点），"
                    f"将达到 {projected_total:.2%}，"
                    f"超过上限 {cfg.max_budget_usage_pct:.2%}"
                )
                log.info("[Budget] 预算检查拒绝（总量超限）: {}", reason)
                return False, reason, state.deployed_pct

            # ── 5. DCA 专用预算上限 ──────────────────────────────
            if is_dca:
                projected_dca = state.dca_deployed_pct + effective_cost
                if projected_dca > cfg.dca_budget_cap_pct:
                    reason = (
                        f"DCA 预算超限：当前 DCA 已部署 {state.dca_deployed_pct:.2%}，"
                        f"本次加仓后将达 {projected_dca:.2%}，"
                        f"超过 DCA 上限 {cfg.dca_budget_cap_pct:.2%}"
                    )
                    log.info("[Budget] 预算检查拒绝（DCA 超限）: {}", reason)
                    return False, reason, state.deployed_pct

            # ── 6. 日内累计上限 ──────────────────────────────────
            projected_intraday = state.intraday_used_pct + effective_cost
            if projected_intraday > cfg.intraday_budget_cap_pct:
                reason = (
                    f"日内预算超限：日内已用 {state.intraday_used_pct:.2%}，"
                    f"本次累计后将达 {projected_intraday:.2%}，"
                    f"超过日内上限 {cfg.intraday_budget_cap_pct:.2%}"
                )
                log.info("[Budget] 预算检查拒绝（日内超限）: {}", reason)
                return False, reason, state.deployed_pct

        log.debug(
            "[Budget] 预算检查通过: order={:.2%} effective={:.2%} "
            "deployed_after={:.2%} is_dca={}",
            order_value_pct,
            effective_cost,
            projected_total,
            is_dca,
        )
        return True, "OK", projected_total

    # ──────────────────────────────────────────────────────────────
    # 状态更新接口
    # ──────────────────────────────────────────────────────────────

    def record_order(
        self,
        order_value_pct: float,
        is_dca: bool = False,
    ) -> None:
        """
        下单成功后调用，更新已部署预算状态。

        Args:
            order_value_pct: 实际下单金额占净值比
            is_dca:          是否为 DCA 仓
        """
        cfg = self.config
        effective_cost = order_value_pct * (
            1 + cfg.fee_reserve_pct + cfg.slippage_reserve_pct
        )
        with self._lock:
            self._state.deployed_pct = min(
                self._state.deployed_pct + effective_cost,
                1.0,
            )
            if is_dca:
                self._state.dca_deployed_pct = min(
                    self._state.dca_deployed_pct + effective_cost,
                    1.0,
                )
            self._state.intraday_used_pct = min(
                self._state.intraday_used_pct + effective_cost,
                1.0,
            )
            self._persist()
        log.info(
            "[Budget] 记录下单: order={:.2%} is_dca={} → deployed={:.2%} dca={:.2%}",
            order_value_pct,
            is_dca,
            self._state.deployed_pct,
            self._state.dca_deployed_pct,
        )

    def record_close(
        self,
        release_value_pct: float,
        is_dca: bool = False,
    ) -> None:
        """
        平仓/止损后调用，释放已部署预算。

        Args:
            release_value_pct: 释放的预算占净值比
            is_dca:            是否为 DCA 仓释放
        """
        cfg = self.config
        effective_release = release_value_pct * (
            1 + cfg.fee_reserve_pct + cfg.slippage_reserve_pct
        )
        with self._lock:
            self._state.deployed_pct = max(
                self._state.deployed_pct - effective_release,
                0.0,
            )
            if is_dca:
                self._state.dca_deployed_pct = max(
                    self._state.dca_deployed_pct - effective_release,
                    0.0,
                )
            self._persist()
        log.info(
            "[Budget] 记录平仓: release={:.2%} is_dca={} → deployed={:.2%}",
            release_value_pct,
            is_dca,
            self._state.deployed_pct,
        )

    def reset_daily(self) -> None:
        """
        每日重置（调度器在 00:00 UTC 调用）。

        仅重置日内累计计数，不重置持仓部署量（仓位仍然存在）。
        """
        with self._lock:
            old_intraday = self._state.intraday_used_pct
            self._state.intraday_used_pct = 0.0
            self._state.current_date = datetime.now(tz=timezone.utc).date()
            self._persist()
        log.info(
            "[Budget] 日内预算重置: 旧值={:.2%} → 0.00%",
            old_intraday,
        )

    def reset_all(self) -> None:
        """
        完全重置（仅测试或手动恢复使用）。
        """
        with self._lock:
            self._state = BudgetState()
            self._persist()
        log.warning("[Budget] 预算状态已完全重置（reset_all 调用）")

    # ──────────────────────────────────────────────────────────────
    # 诊断接口
    # ──────────────────────────────────────────────────────────────

    @property
    def remaining_budget_pct(self) -> float:
        """剩余可用预算比例（考虑总上限）。"""
        with self._lock:
            return max(
                self.config.max_budget_usage_pct - self._state.deployed_pct,
                0.0,
            )

    def snapshot(self) -> dict[str, Any]:
        """返回当前预算状态快照（用于日志 / RiskSnapshot 构建）。"""
        with self._lock:
            remaining = max(
                self.config.max_budget_usage_pct - self._state.deployed_pct,
                0.0,
            )
            return {
                "deployed_pct": self._state.deployed_pct,
                "dca_deployed_pct": self._state.dca_deployed_pct,
                "intraday_used_pct": self._state.intraday_used_pct,
                "remaining_budget_pct": remaining,
                "max_budget_usage_pct": self.config.max_budget_usage_pct,
                "dca_budget_cap_pct": self.config.dca_budget_cap_pct,
                "config_version": self.config.config_version,
                "current_date": self._state.current_date.isoformat(),
            }
