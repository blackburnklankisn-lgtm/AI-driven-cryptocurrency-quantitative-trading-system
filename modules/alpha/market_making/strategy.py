"""
modules/alpha/market_making/strategy.py — MarketMakingStrategy 顶层编排器

设计说明：
- 连接 W17-W18 所有子模块，按严格顺序执行：
    1. 风险拦截（RiskSnapshot.is_safe_to_trade + KillSwitch）
    2. 库存状态更新（InventoryManager.update_mid）
    3. Avellaneda 数学模型（AvellanedaModel.compute → QuoteIntent）
    4. 报价引擎（QuoteEngine.generate → QuoteDecision）
    5. 报价生命周期（QuoteLifecycle.evaluate → QuoteActions）
    6. Fill 仿真（paper 模式下 FillSimulator.batch_check）
    7. 持久化（QuoteStateStore.save）
- 支持 paper/replay/live 三种模式（live 下 fill 由真实回调注入，不走 FillSimulator）
- 所有 log 使用 [MarketMaking] 标签，内部子模块使用各自标签

日志标签：[MarketMaking]
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.monitoring.trace import generate_trace_id, get_recorder
from modules.alpha.contracts.mm_types import (
    ActiveQuote,
    FillRecord,
    QuoteAction,
    QuoteDecision,
    QuoteSide,
    QuoteState,
)
from modules.alpha.market_making.avellaneda_model import AvellanedaConfig, AvellanedaModel
from modules.alpha.market_making.fill_simulator import FillSimulator, FillSimulatorConfig
from modules.alpha.market_making.inventory_manager import InventoryConfig, InventoryManager
from modules.alpha.market_making.quote_engine import QuoteEngine, QuoteEngineConfig
from modules.alpha.market_making.quote_lifecycle import QuoteLifecycle, QuoteLifecycleConfig
from modules.alpha.market_making.quote_state_store import QuoteStateSnapshot, QuoteStateStore
from modules.data.realtime.orderbook_types import OrderBookSnapshot
from modules.risk.snapshot import RiskSnapshot

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、策略配置
# ══════════════════════════════════════════════════════════════

@dataclass
class MarketMakingStrategyConfig:
    """
    MarketMakingStrategy 总配置。

    Attributes:
        symbol:          交易对（如 "BTC/USDT"）
        exchange:        交易所名称（如 "binance"）
        paper_mode:      True = paper/replay 模式（填单走 FillSimulator）
        save_every_n:    每 N 次 tick 保存一次状态（0 = 禁用自动持久化）
        state_store_path: 状态文件路径（None = 默认 storage/quote_state_{symbol}.json）

        avellaneda:      AvellanedaModel 配置
        inventory:       InventoryManager 配置
        quote_engine:    QuoteEngine 配置
        lifecycle:       QuoteLifecycle 配置
        fill_sim:        FillSimulator 配置（paper 模式）
    """

    symbol: str = "BTC/USDT"
    exchange: str = "binance"
    paper_mode: bool = True
    save_every_n: int = 10
    state_store_path: Optional[str] = None

    avellaneda: AvellanedaConfig = field(default_factory=AvellanedaConfig)
    inventory: InventoryConfig = field(default_factory=InventoryConfig)
    quote_engine: QuoteEngineConfig = field(default_factory=QuoteEngineConfig)
    lifecycle: QuoteLifecycleConfig = field(default_factory=QuoteLifecycleConfig)
    fill_sim: FillSimulatorConfig = field(default_factory=FillSimulatorConfig)


# ══════════════════════════════════════════════════════════════
# 二、MarketMakingStrategy 主体
# ══════════════════════════════════════════════════════════════

class MarketMakingStrategy:
    """
    Avellaneda-Stoikov 做市策略编排器。

    线程安全：tick() 持 RLock，fill 回调也持 RLock。
    """

    def __init__(self, config: Optional[MarketMakingStrategyConfig] = None) -> None:
        self.config = config or MarketMakingStrategyConfig()
        self._lock = threading.RLock()
        self._tick_count: int = 0

        # 子模块
        self._model = AvellanedaModel(self.config.avellaneda)
        self._inventory = InventoryManager(self.config.inventory)
        self._quote_engine = QuoteEngine(self.config.quote_engine)
        self._lifecycle = QuoteLifecycle(self.config.symbol, self.config.lifecycle)
        self._fill_sim = FillSimulator(self.config.fill_sim)

        # 持久化
        store_path = (
            self.config.state_store_path
            or f"storage/quote_state_{self.config.symbol.replace('/', '_')}.json"
        )
        self._store = QuoteStateStore(store_path)
        self._last_trace_id: str = ""  # 最近一次 tick 的 trace_id（供 _halt_decision 使用）

        log.info(
            "[MarketMaking] 策略初始化: symbol={} exchange={} paper_mode={}",
            self.config.symbol,
            self.config.exchange,
            self.config.paper_mode,
        )

    # ──────────────────────────────────────────────────────────
    # 主驱动接口
    # ──────────────────────────────────────────────────────────

    def tick(
        self,
        snapshot: OrderBookSnapshot,
        risk_snapshot: RiskSnapshot,
        elapsed_sec: float = 0.0,
    ) -> QuoteDecision:
        """
        每个 tick 驱动一次完整的报价决策流程。

        Args:
            snapshot:       当前订单簿快照
            risk_snapshot:  当前风险状态
            elapsed_sec:    自策略启动经过的秒数（用于 Avellaneda 时间衰减）

        Returns:
            QuoteDecision（含 bid/ask 价格与数量，及 allow_post_bid/ask）
        """
        with self._lock:
            self._tick_count += 1
            _trace_id = generate_trace_id("mm", self.config.symbol)
            self._last_trace_id = _trace_id  # 供 halt 路径使用

            # 步骤 1：风险拦截
            if not risk_snapshot.is_safe_to_trade():
                return self._halt_decision("RISK_BLOCKED", risk_snapshot)

            if not snapshot.is_healthy():
                return self._halt_decision("SNAPSHOT_UNHEALTHY", risk_snapshot)

            mid = snapshot.mid_price

            # 步骤 2：更新库存
            self._inventory.update_mid(mid)
            inv_snap = self._inventory.snapshot(mid)

            # 检查库存硬停
            suggestions = self._inventory.suggest_quote_sides(inv_snap)
            if suggestions.get("halt", False):
                return self._halt_decision("INVENTORY_HALT", risk_snapshot)

            # 步骤 3：Avellaneda 数学模型
            intent = self._model.compute(
                symbol=self.config.symbol,
                mid_price=mid,
                sigma=snapshot.spread_bps / 10000 if snapshot.spread_bps > 0 else 0.001,
                inventory_qty=inv_snap.base_qty,
                inventory_max_qty=max(
                    abs(inv_snap.base_qty) * (1.0 / max(self.config.inventory.max_inventory_pct, 0.01)),
                    1e-8,
                ),
                elapsed_sec=elapsed_sec,
            )

            # 步骤 4：报价引擎
            decision = self._quote_engine.generate(intent, inv_snap)

            # 步骤 5：生命周期评估
            kill_switch = risk_snapshot.kill_switch_active
            book_gap = not snapshot.is_healthy()
            lifecycle_actions = self._lifecycle.evaluate(
                decision, mid, kill_switch_active=kill_switch, book_gap=book_gap
            )

            log.debug(
                "[MarketMaking] tick#{}: trace_id={} symbol={} mid={:.4f} "
                "reservation={:.4f} spread_bps={:.2f} bid={} ask={}",
                self._tick_count,
                _trace_id,
                self.config.symbol,
                mid,
                intent.reservation_price,
                decision.optimal_spread_bps,
                f"{decision.bid_price:.4f}" if decision.bid_price else "N/A",
                f"{decision.ask_price:.4f}" if decision.ask_price else "N/A",
            )

            # trace 记录决策结果
            get_recorder().record(_trace_id, "mm", "TICK_END", {
                "symbol": self.config.symbol,
                "tick": self._tick_count,
                "mid": mid,
                "reservation": intent.reservation_price,
                "bid": decision.bid_price,
                "ask": decision.ask_price,
                "allow_bid": decision.allow_post_bid,
                "allow_ask": decision.allow_post_ask,
                "spread_bps": decision.optimal_spread_bps,
            })

            # 步骤 6：paper 模式 fill 仿真
            if self.config.paper_mode:
                active_quotes = self._lifecycle.active_quotes()
                fills = self._fill_sim.batch_check(active_quotes, snapshot)
                for fill in fills:
                    self._process_fill(fill)

            # 步骤 7：定期持久化
            if self.config.save_every_n > 0 and self._tick_count % self.config.save_every_n == 0:
                self._save_state()

            return decision

    def on_fill(self, fill: FillRecord) -> None:
        """
        处理来自真实交易所的成交回报（live 模式使用）。

        Args:
            fill: FillRecord（由交易所 WebSocket 回报转换）
        """
        with self._lock:
            self._process_fill(fill)

    # ──────────────────────────────────────────────────────────
    # 状态查询
    # ──────────────────────────────────────────────────────────

    def restore(self) -> bool:
        """
        从持久化存储恢复上次状态。

        Returns:
            True = 恢复成功，False = 无存储或恢复失败
        """
        snap = self._store.load()
        if snap is None:
            return False
        with self._lock:
            # 恢复库存状态（仅基础数值，不恢复 FIFO 队列）
            self._inventory._base_qty = snap.base_qty
            self._inventory._quote_value = snap.quote_value
            self._inventory._realized_pnl = snap.realized_pnl
        log.info(
            "[MarketMaking] 已恢复库存状态: symbol={} base_qty={:.6f} realized_pnl={:.4f}",
            snap.symbol, snap.base_qty, snap.realized_pnl,
        )
        return True

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            inv_snap = self._inventory.snapshot()
            return {
                "symbol": self.config.symbol,
                "exchange": self.config.exchange,
                "paper_mode": self.config.paper_mode,
                "tick_count": self._tick_count,
                "inventory": {
                    "base_qty": inv_snap.base_qty,
                    "quote_value": inv_snap.quote_value,
                    "realized_pnl": inv_snap.realized_pnl,
                    "total_trades": inv_snap.total_trades,
                    "inventory_pct": inv_snap.inventory_pct,
                },
                "lifecycle": self._lifecycle.diagnostics(),
            }

    def reset(self) -> None:
        """重置策略状态（不清除持久化文件）。"""
        with self._lock:
            self._inventory.reset()
            self._lifecycle.reset()
            self._tick_count = 0
        log.info("[MarketMaking] 策略已重置: symbol={}", self.config.symbol)

    # ──────────────────────────────────────────────────────────
    # 内部辅助
    # ──────────────────────────────────────────────────────────

    def _process_fill(self, fill: FillRecord) -> None:
        """处理成交（更新库存 + 生命周期）。"""
        self._inventory.on_fill(fill)
        self._lifecycle.on_fill(fill)
        log.info(
            "[MarketMaking] 成交处理: symbol={} side={} price={:.4f} qty={:.6f} "
            "is_partial={} pnl_so_far={:.4f}",
            fill.symbol, fill.side.value, fill.fill_price, fill.fill_qty,
            fill.is_partial, self._inventory.snapshot().realized_pnl,
        )

    def _halt_decision(
        self, reason: str, risk_snapshot: RiskSnapshot
    ) -> QuoteDecision:
        """生成一个 HALT（全面禁止）的空 QuoteDecision，并写入 trace 事件。"""
        log.warning(
            "[MarketMaking] HALT: symbol={} reason={} circuit_broken={} kill_switch={}",
            self.config.symbol,
            reason,
            risk_snapshot.circuit_broken,
            risk_snapshot.kill_switch_active,
        )
        # trace 注入（halt 路径也记录，以保证可观测性完整）
        if self._last_trace_id:
            get_recorder().record(self._last_trace_id, "mm", "TICK_HALT", {
                "symbol": self.config.symbol,
                "tick": self._tick_count,
                "reason": reason,
            })
        return QuoteDecision(
            symbol=self.config.symbol,
            bid_price=None,
            ask_price=None,
            bid_size=0.0,
            ask_size=0.0,
            reservation_price=0.0,
            optimal_spread_bps=0.0,
            skew_bps=0.0,
            allow_post_bid=False,
            allow_post_ask=False,
            reason_codes=[reason],
        )

    def _save_state(self) -> None:
        """将当前库存状态原子写入持久化文件。"""
        try:
            inv_snap = self._inventory.snapshot()
            state_snap = QuoteStateSnapshot(
                symbol=self.config.symbol,
                base_qty=inv_snap.base_qty,
                quote_value=inv_snap.quote_value,
                realized_pnl=inv_snap.realized_pnl,
                total_trades=inv_snap.total_trades,
                metadata={
                    "tick_count": self._tick_count,
                    "paper_mode": self.config.paper_mode,
                },
            )
            self._store.save(state_snap)
        except Exception:
            log.exception("[MarketMaking] 状态持久化失败: symbol={}", self.config.symbol)
