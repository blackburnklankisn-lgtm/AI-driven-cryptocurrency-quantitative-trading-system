"""
modules/risk/kill_switch.py — 实时操作级停机开关

设计说明：
- 独立于 RiskManager（最后防线），在上游提供"实时操作级熔断"能力
- 监控以下信号：
    * 实时回撤超阈值
    * 日内亏损超阈值
    * 连续订单拒绝过多（来自 RiskManager 或 BudgetChecker）
    * 连续订单成交失败过多
    * 数据源 freshness 失效（stale 超时）
- 提供两类恢复机制：
    * 手动重置（manual_reset）—— 人工确认后解除
    * 自动冷却恢复（try_auto_recover）—— 冷却期满后自动解除
- 激活状态持久化到 StateStore，避免重启后隐式解锁
- 关键解耦要求：
    * 不依赖具体策略类，只消费运行状态、错误计数和风险指标
    * 不修改订单内容，只输出 is_active 布尔值让调用方判断

接口：
    KillSwitch(config, state_store)
    .evaluate(risk_snapshot) → bool    # 综合评估，必要时激活
    .record_order_rejection(reason)     # 记录订单拒绝
    .record_order_failure(reason)       # 记录订单成交失败
    .record_order_success()             # 记录成功成交（重置计数器）
    .record_data_health(source, fresh)  # 更新数据源健康状态
    .manual_activate(reason)            # 人工紧急激活
    .manual_reset(reason)               # 人工解除（完全清空状态）
    .try_auto_recover() → bool          # 自动冷却期恢复检查
    .is_active → bool                   # 当前是否已激活
    .health_snapshot() → dict           # 完整诊断快照

日志标签：[KillSwitch]
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from core.logger import get_logger
from modules.risk.snapshot import RiskSnapshot
from modules.risk.state_store import StateStore

log = get_logger(__name__)

_STORE_KEY = "kill_switch"


@dataclass
class KillSwitchConfig:
    """KillSwitch 触发条件与恢复策略配置。"""

    # ── 风险指标触发阈值 ─────────────────────────────────────────
    drawdown_trigger: float = 0.10          # 回撤超过此值激活 Kill Switch
    daily_loss_trigger: float = 0.03        # 日内亏损超过此值激活 Kill Switch

    # ── 操作计数器触发阈值 ───────────────────────────────────────
    max_consecutive_rejections: int = 5     # 连续拒绝次数超过此值激活
    max_consecutive_failures: int = 3       # 连续失败次数超过此值激活

    # ── 数据源 freshness 触发 ────────────────────────────────────
    stale_data_timeout_sec: int = 300       # 数据源超过此秒数未更新视为 stale
    stale_sources_trigger_count: int = 2    # 同时有多少个 source stale 时触发

    # ── 恢复策略 ─────────────────────────────────────────────────
    auto_recover_minutes: int = 120         # 非手动激活时的自动冷却恢复时长（分钟）
    manual_activate_requires_manual_reset: bool = True  # 手动激活只能手动恢复

    # ── 版本 ─────────────────────────────────────────────────────
    config_version: str = "v1.0"


class KillSwitchState:
    """
    KillSwitch 运行时状态（可序列化，支持 StateStore 持久化）。
    """

    __slots__ = (
        "active",
        "reason",
        "activated_at",
        "auto_recover_at",
        "manual_activated",
        "consecutive_rejections",
        "consecutive_failures",
        "stale_sources",         # dict: source_name -> last_ok_at (datetime)
    )

    def __init__(self) -> None:
        self.active: bool = False
        self.reason: str = ""
        self.activated_at: Optional[datetime] = None
        self.auto_recover_at: Optional[datetime] = None
        self.manual_activated: bool = False
        self.consecutive_rejections: int = 0
        self.consecutive_failures: int = 0
        self.stale_sources: Dict[str, Optional[datetime]] = {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "reason": self.reason,
            "activated_at": (
                self.activated_at.isoformat() if self.activated_at else None
            ),
            "auto_recover_at": (
                self.auto_recover_at.isoformat() if self.auto_recover_at else None
            ),
            "manual_activated": self.manual_activated,
            "consecutive_rejections": self.consecutive_rejections,
            "consecutive_failures": self.consecutive_failures,
            "stale_sources": {
                k: (v.isoformat() if v else None)
                for k, v in self.stale_sources.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KillSwitchState":
        state = cls()
        state.active = bool(data.get("active", False))
        state.reason = str(data.get("reason", ""))
        state.manual_activated = bool(data.get("manual_activated", False))
        state.consecutive_rejections = int(data.get("consecutive_rejections", 0))
        state.consecutive_failures = int(data.get("consecutive_failures", 0))

        def _parse_dt(val: Any) -> Optional[datetime]:
            if val is None:
                return None
            dt = datetime.fromisoformat(str(val))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        state.activated_at = _parse_dt(data.get("activated_at"))
        state.auto_recover_at = _parse_dt(data.get("auto_recover_at"))

        raw_stale = data.get("stale_sources", {})
        state.stale_sources = {k: _parse_dt(v) for k, v in raw_stale.items()}
        return state


class KillSwitch:
    """
    实时操作级停机开关。

    Args:
        config:      KillSwitchConfig 配置对象
        state_store: StateStore 持久化对象（可选）
    """

    def __init__(
        self,
        config: Optional[KillSwitchConfig] = None,
        state_store: Optional[StateStore] = None,
    ) -> None:
        self.config = config or KillSwitchConfig()
        self._store = state_store
        self._lock = threading.Lock()
        self._state = self._load_state()
        log.info(
            "[KillSwitch] 初始化完成: drawdown_trigger={:.0%} "
            "daily_loss_trigger={:.0%} max_rejections={} "
            "max_failures={} auto_recover={}min version={}",
            self.config.drawdown_trigger,
            self.config.daily_loss_trigger,
            self.config.max_consecutive_rejections,
            self.config.max_consecutive_failures,
            self.config.auto_recover_minutes,
            self.config.config_version,
        )
        if self._state.active:
            log.warning(
                "[KillSwitch] 从持久化状态恢复：Kill Switch 仍处于激活状态，"
                "原因: {} 激活时间: {}",
                self._state.reason,
                self._state.activated_at,
            )

    # ──────────────────────────────────────────────────────────────
    # 状态持久化
    # ──────────────────────────────────────────────────────────────

    def _load_state(self) -> KillSwitchState:
        """从 StateStore 恢复状态，如不存在则初始化为零值。"""
        if self._store is None:
            return KillSwitchState()
        data = self._store.load(_STORE_KEY)
        if data is None:
            return KillSwitchState()
        try:
            return KillSwitchState.from_dict(data)
        except Exception as exc:
            log.error("[KillSwitch] 恢复状态失败，使用空状态: {}", exc)
            return KillSwitchState()

    def _persist(self) -> None:
        """持久化当前状态（持有锁时调用）。"""
        if self._store is None:
            return
        try:
            self._store.save(_STORE_KEY, self._state.to_dict())
        except Exception as exc:
            log.error("[KillSwitch] 未能持久化风险状态: {}", exc)

    # ──────────────────────────────────────────────────────────────
    # 内部激活/解除辅助
    # ──────────────────────────────────────────────────────────────

    def _activate(self, reason: str, manual: bool = False) -> None:
        """激活 Kill Switch（不加锁，调用方负责）。"""
        now = datetime.now(tz=timezone.utc)
        self._state.active = True
        self._state.reason = reason
        self._state.activated_at = now
        self._state.manual_activated = manual
        if not manual:
            self._state.auto_recover_at = now + timedelta(
                minutes=self.config.auto_recover_minutes
            )
        else:
            self._state.auto_recover_at = None
        self._persist()
        log.info(
            "[KillSwitch] 已激活: reason={} manual={} auto_recover_at={}",
            reason,
            manual,
            self._state.auto_recover_at,
        )

    def _deactivate(self, reason: str) -> None:
        """解除 Kill Switch（不加锁，调用方负责）。"""
        self._state.active = False
        self._state.reason = ""
        self._state.activated_at = None
        self._state.auto_recover_at = None
        self._state.manual_activated = False
        self._state.consecutive_rejections = 0
        self._state.consecutive_failures = 0
        self._persist()
        log.info("[KillSwitch] 已解除: reason={}", reason)

    # ──────────────────────────────────────────────────────────────
    # 核心评估接口
    # ──────────────────────────────────────────────────────────────

    def evaluate(self, risk_snapshot: Optional[RiskSnapshot] = None) -> bool:
        """
        综合评估所有监控信号，必要时激活 Kill Switch。

        建议在每个交易循环开头调用，结合 `is_active` 属性阻断下单。

        Args:
            risk_snapshot: 来自 RiskManager 的结构化风险快照（可选）

        Returns:
            True 如果当前 Kill Switch 已激活（禁止下单）
        """
        # 已激活时先检查自动恢复
        if self._state.active:
            self.try_auto_recover()
            return self._state.active

        cfg = self.config
        with self._lock:
            if risk_snapshot is not None:
                # ── 1. 回撤阈值 ──────────────────────────────────
                if risk_snapshot.current_drawdown >= cfg.drawdown_trigger:
                    self._activate(
                        f"实时回撤 {risk_snapshot.current_drawdown:.2%} "
                        f">= 触发阈值 {cfg.drawdown_trigger:.2%}"
                    )
                    return True

                # ── 2. 日内亏损阈值 ───────────────────────────────
                if risk_snapshot.daily_loss_pct >= cfg.daily_loss_trigger:
                    self._activate(
                        f"日内亏损 {risk_snapshot.daily_loss_pct:.2%} "
                        f">= 触发阈值 {cfg.daily_loss_trigger:.2%}"
                    )
                    return True

            # ── 3. 连续订单拒绝 ───────────────────────────────────
            if self._state.consecutive_rejections >= cfg.max_consecutive_rejections:
                self._activate(
                    f"连续订单拒绝次数 {self._state.consecutive_rejections} "
                    f">= 上限 {cfg.max_consecutive_rejections}"
                )
                return True

            # ── 4. 连续订单失败 ───────────────────────────────────
            if self._state.consecutive_failures >= cfg.max_consecutive_failures:
                self._activate(
                    f"连续订单成交失败次数 {self._state.consecutive_failures} "
                    f">= 上限 {cfg.max_consecutive_failures}"
                )
                return True

            # ── 5. 数据源 stale 检查 ─────────────────────────────
            stale_count = self._count_stale_sources()
            if stale_count >= cfg.stale_sources_trigger_count:
                stale_names = self._stale_source_names()
                self._activate(
                    f"数据源 stale 数量 {stale_count} >= 触发阈值 "
                    f"{cfg.stale_sources_trigger_count}，"
                    f"stale sources: {stale_names}"
                )
                return True

        return False

    # ──────────────────────────────────────────────────────────────
    # 计数器更新接口
    # ──────────────────────────────────────────────────────────────

    def record_order_rejection(self, reason: str = "") -> None:
        """记录一次订单拒绝（来自 BudgetChecker 或 RiskManager）。"""
        with self._lock:
            self._state.consecutive_rejections += 1
            self._state.consecutive_failures = 0  # 拒绝不计入"失败"
        log.debug(
            "[KillSwitch] 记录订单拒绝: reason={} 连续拒绝次数={}",
            reason,
            self._state.consecutive_rejections,
        )
        if self._state.consecutive_rejections >= self.config.max_consecutive_rejections:
            log.warning(
                "[KillSwitch] 连续拒绝次数 {} >= 上限 {}，下次 evaluate() 将激活",
                self._state.consecutive_rejections,
                self.config.max_consecutive_rejections,
            )

    def record_order_failure(self, reason: str = "") -> None:
        """记录一次订单成交失败（交易所返回错误）。"""
        with self._lock:
            self._state.consecutive_failures += 1
        log.debug(
            "[KillSwitch] 记录订单失败: reason={} 连续失败次数={}",
            reason,
            self._state.consecutive_failures,
        )
        if self._state.consecutive_failures >= self.config.max_consecutive_failures:
            log.warning(
                "[KillSwitch] 连续失败次数 {} >= 上限 {}，下次 evaluate() 将激活",
                self._state.consecutive_failures,
                self.config.max_consecutive_failures,
            )

    def record_order_success(self) -> None:
        """记录一次成功成交，重置拒绝/失败计数器。"""
        with self._lock:
            self._state.consecutive_rejections = 0
            self._state.consecutive_failures = 0
        log.debug("[KillSwitch] 记录订单成功，计数器已重置")

    def record_data_health(self, source_name: str, is_fresh: bool) -> None:
        """
        更新指定数据源的健康状态。

        Args:
            source_name: 数据源名称（如 "technical", "onchain", "sentiment"）
            is_fresh:    当前时刻该数据源是否新鲜
        """
        now = datetime.now(tz=timezone.utc)
        with self._lock:
            if is_fresh:
                self._state.stale_sources[source_name] = now  # 更新最后健康时间
            else:
                # 仅在尚无记录时初始化为 None（表示"从未健康过"）
                if source_name not in self._state.stale_sources:
                    self._state.stale_sources[source_name] = None
        if not is_fresh:
            log.warning(
                "[KillSwitch] 数据源 {} freshness 失效，"
                "stale_sources 当前 stale 数量: {}",
                source_name,
                self._count_stale_sources(),
            )

    # ──────────────────────────────────────────────────────────────
    # 手动控制接口
    # ──────────────────────────────────────────────────────────────

    def manual_activate(self, reason: str) -> None:
        """
        人工紧急激活 Kill Switch。

        当 `manual_activate_requires_manual_reset=True` 时，
        只能通过 `manual_reset()` 解除，不会自动冷却恢复。
        """
        with self._lock:
            self._activate(reason, manual=True)
        log.warning("[KillSwitch] 人工激活: {}", reason)

    def manual_reset(self, reason: str = "人工解除") -> None:
        """
        人工解除 Kill Switch（完全清空激活状态）。

        Args:
            reason: 解除原因（记入日志）
        """
        with self._lock:
            self._deactivate(reason)
        log.info("[KillSwitch] 人工解除完成: {}", reason)

    # ──────────────────────────────────────────────────────────────
    # 自动恢复
    # ──────────────────────────────────────────────────────────────

    def try_auto_recover(self) -> bool:
        """
        检查自动冷却期是否已满，满足条件则自动解除。

        Returns:
            True 如果成功自动恢复（Kill Switch 已解除）
        """
        with self._lock:
            if not self._state.active:
                return False
            if self._state.manual_activated and (
                self.config.manual_activate_requires_manual_reset
            ):
                return False  # 手动激活只能手动恢复
            if self._state.auto_recover_at is None:
                return False
            now = datetime.now(tz=timezone.utc)
            if now >= self._state.auto_recover_at:
                self._deactivate(
                    f"自动冷却期满解除（冷却 {self.config.auto_recover_minutes} 分钟）"
                )
                return True
        return False

    # ──────────────────────────────────────────────────────────────
    # 内部辅助
    # ──────────────────────────────────────────────────────────────

    def _count_stale_sources(self) -> int:
        """统计当前 stale 的数据源数量（不加锁，调用方负责）。"""
        now = datetime.now(tz=timezone.utc)
        timeout_sec = self.config.stale_data_timeout_sec
        count = 0
        for last_ok in self._state.stale_sources.values():
            if last_ok is None:
                count += 1
            elif (now - last_ok).total_seconds() > timeout_sec:
                count += 1
        return count

    def _stale_source_names(self) -> list[str]:
        """返回当前 stale 的数据源名称列表（不加锁，调用方负责）。"""
        now = datetime.now(tz=timezone.utc)
        timeout_sec = self.config.stale_data_timeout_sec
        return [
            name
            for name, last_ok in self._state.stale_sources.items()
            if last_ok is None
            or (now - last_ok).total_seconds() > timeout_sec
        ]

    # ──────────────────────────────────────────────────────────────
    # 属性与诊断
    # ──────────────────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        """当前 Kill Switch 是否激活（True = 禁止下单）。"""
        return self._state.active

    def health_snapshot(self) -> dict[str, Any]:
        """返回完整诊断快照（用于监控面板、日志、RiskSnapshot 构建）。"""
        with self._lock:
            stale_count = self._count_stale_sources()
            return {
                "active": self._state.active,
                "reason": self._state.reason,
                "activated_at": (
                    self._state.activated_at.isoformat()
                    if self._state.activated_at
                    else None
                ),
                "auto_recover_at": (
                    self._state.auto_recover_at.isoformat()
                    if self._state.auto_recover_at
                    else None
                ),
                "manual_activated": self._state.manual_activated,
                "consecutive_rejections": self._state.consecutive_rejections,
                "consecutive_failures": self._state.consecutive_failures,
                "stale_source_count": stale_count,
                "stale_source_names": self._stale_source_names(),
                "config_version": self.config.config_version,
            }
