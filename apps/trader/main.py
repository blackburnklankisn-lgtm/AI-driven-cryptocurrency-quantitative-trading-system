"""
apps/trader/main.py — 实盘/模拟盘主控程序

设计说明：
- 这是系统的"顶层入口"，整合所有六层组件
- 支持三种运行模式（由 TRADING_MODE 环境变量控制）：
    paper:    模拟盘（与实盘相同代码路径，不真实下单）
    live:     实盘（调用交易所 API 真实发单）
    backtest: 不使用此入口，走 BacktestEngine

主控循环设计（同步版本，简单可靠）：
1. 初始化所有组件（配置 → 日志 → 指标 → 网关 → 风控 → 策略）
2. 启动 Prometheus HTTP 服务
3. 进入主循环：
    a. 通过 CCXT REST 轮询最新 K 线
    b. 发布 KlineEvent 到事件总线
    c. 策略处理，产出 OrderRequestEvent（若有信号）
    d. RiskManager 审核
    e. OrderManager 提交发单
    f. 轮询成交回报，更新持仓状态
    g. 更新 Prometheus 指标
    h. 睡眠到下一个轮询间隔

安全措施：
- 捕获 KeyboardInterrupt 优雅退出
- 任何未捕获异常都触发邮件/Slack 告警（可扩展）
- 主循环内任何单次异常不崩溃整个进程（记录并继续）

运行方式：
    TRADING_MODE=paper python -m apps.trader.main
"""

from __future__ import annotations

import os
import signal
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional
import threading
import uvicorn
import asyncio
from uuid import uuid4

import pandas as pd
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.config import load_config
from core.event import EventBus, EventType, KlineEvent, OrderRequestEvent
from core.logger import audit_log, get_logger, setup_logging, trade_log
from modules.alpha.contracts import RegimeState
from modules.alpha.ml.data_kitchen import DataKitchen
from modules.alpha.orchestration.strategy_orchestrator import (
    OrchestrationInput,
    StrategyOrchestrator,
)
from modules.alpha.runtime import AlphaRuntime, StrategyRegistry, BaseAlphaAdapter
from modules.alpha.regime.detector import MarketRegimeDetector
from modules.data.realtime.depth_cache import DepthCacheConfig, DepthCacheRegistry
from modules.data.realtime.feature_builder import MicroFeatureBuilder
from modules.data.realtime.orderbook_types import OrderBookSnapshot
from modules.data.realtime.subscription_manager import SubscriptionManager, SubscriptionManagerConfig
from modules.data.realtime.trade_cache import TradeCacheConfig, TradeCacheRegistry
from modules.data.realtime.ws_client import (
    ExchangeWsClient,
    MockWsClient,
    WsClientConfig,
    create_exchange_ws_client,
)
from modules.execution.gateway import CCXTGateway
from modules.execution.order_manager import OrderManager
from modules.monitoring.metrics import SystemMetrics
from modules.risk.adaptive_matrix import AdaptiveRiskMatrix
from modules.risk.budget_checker import BudgetChecker, BudgetConfig
from modules.risk.kill_switch import KillSwitch, KillSwitchConfig
from modules.risk.manager import RiskConfig, RiskManager
from modules.risk.position_sizer import PositionSizer
from modules.risk.snapshot import RiskSnapshot
from modules.risk.state_store import StateStore
from modules.portfolio.allocator import AllocationMethod, PortfolioAllocator
from modules.portfolio.rebalancer import PortfolioRebalancer, RebalanceOrder
from modules.portfolio.performance_attribution import PerformanceAttributor
from apps.api.server import app as fast_app, set_trader_instance, WebsocketLogSink

log = get_logger(__name__)

# 将 WebsocketLogSink 挂载到 loguru（跨线程安全版本）
from loguru import logger
_ws_sink = WebsocketLogSink()
logger.add(_ws_sink, format="{time:HH:mm:ss} | {level} | {message}", level="INFO")

class LiveTrader:
    """
    实盘/模拟盘主控类。

    负责协调所有组件的初始化和主循环驱动。
    策略以插件方式注册，与实盘代码完全解耦。

    Args:
        config_path: 系统配置文件路径
    """

    def __init__(self, config_path: str = "configs/system.yaml") -> None:
        # ── 1. 加载系统配置 ──────────────────────────────────────
        self.sys_config = load_config(config_path)
        self.mode = self.sys_config.trading_mode

        if self.mode == "backtest":
            raise RuntimeError("LiveTrader 不支持 backtest 模式，请使用 BacktestEngine")

        # ── 2. 初始化日志 ────────────────────────────────────────
        setup_logging(
            log_dir=self.sys_config.logging.log_dir,
            log_level=self.sys_config.logging.log_level,
        )

        # ── 3. 系统组件 ──────────────────────────────────────────
        self.bus = EventBus()
        self.metrics = SystemMetrics(
            exchange_id=self.sys_config.exchange.exchange_id,
            mode=self.mode,
        )

        # ── 4. 交易所网关 ────────────────────────────────────────
        exc_cfg = self.sys_config.exchange
        api_key, secret, passphrase = exc_cfg.get_credentials()
        self.gateway = CCXTGateway(
            exchange_id=exc_cfg.exchange_id,
            mode=self.mode,
            api_key=api_key,
            secret=secret,
            passphrase=passphrase,
            timeout_ms=exc_cfg.request_timeout_ms,
        )
        self.order_manager = OrderManager(self.gateway, fill_timeout_s=300)


        # ── 5. 风控管理器 ────────────────────────────────────────
        risk_cfg = self.sys_config.risk
        self.risk_manager = RiskManager(RiskConfig(
            max_position_pct=float(risk_cfg.max_position_pct),
            max_portfolio_drawdown=float(risk_cfg.max_portfolio_drawdown),
            max_daily_loss=float(risk_cfg.max_daily_loss),
            max_consecutive_losses=risk_cfg.max_consecutive_losses,
            circuit_breaker_cooldown_minutes=getattr(risk_cfg, 'circuit_breaker_cooldown_minutes', 60),
        ))

        # ── 5b. 动态仓位管理器 ───────────────────────────────────
        self.position_sizer = PositionSizer(
            max_position_pct=float(risk_cfg.max_position_pct),
        )

        # ── 5c. Phase 2 风险守卫链 ─────────────────────────────
        self._phase2_state_store = StateStore(
            self._state_file_path.with_name("risk_runtime_state.json")
        )
        self._budget_checker = BudgetChecker(
            BudgetConfig(
                max_single_order_budget_pct=float(risk_cfg.max_position_pct),
            ),
            state_store=self._phase2_state_store,
        )
        self._kill_switch = KillSwitch(
            KillSwitchConfig(
                drawdown_trigger=float(risk_cfg.max_portfolio_drawdown),
                daily_loss_trigger=float(risk_cfg.max_daily_loss),
            ),
            state_store=self._phase2_state_store,
        )
        self._adaptive_risk = AdaptiveRiskMatrix()

        # ── 6. 策略列表（外部注入） ───────────────────────────────
        self._strategies = []
        
        # ── 6b. Phase 1 AlphaRuntime 及 StrategyRegistry ────────
        self._strategy_registry = StrategyRegistry()
        self._alpha_runtime = AlphaRuntime(
            registry=self._strategy_registry,
            debug_enabled=bool(os.getenv("DEBUG_ALPHA_RUNTIME", False)),
        )
        # 默认 regime（初始状态）
        self._current_regime = RegimeState(
            bull_prob=0.33, bear_prob=0.33, sideways_prob=0.34,
            high_vol_prob=0.0, confidence=0.5, dominant_regime="sideways"
        )
        self._phase1_orchestrator = StrategyOrchestrator()
        self._phase1_data_kitchens: Dict[str, DataKitchen] = {}
        self._phase1_regime_detectors: Dict[str, MarketRegimeDetector] = {}
        self._phase1_feature_views: Dict[str, Dict[str, pd.DataFrame]] = {}
        self._symbol_regimes: Dict[str, RegimeState] = {}
        self._last_trace_ids: Dict[str, str] = {}
        self._symbol_risk_plans: Dict[str, object] = {}

        # 运行状态
        self._positions: Dict[str, Decimal] = {}
        self._entry_prices: Dict[str, float] = {}   # 入场均价追踪
        self._stop_loss_pending: set = set()         # 已发出止损单的品种，防重复触发
        self._latest_prices: Dict[str, float] = {}
        self._current_equity: float = 0.0
        self._running: bool = False
        
        # K 线存储库 (最近 100 根，供前端绘图使用)
        # 格式: { symbol: [ {time, open, high, low, close, volume}, ... ] }
        self._kline_store: Dict[str, List[Dict[str, Any]]] = {}

        # ── 交易所连接状态追踪 ────────────────────────────────
        # loadMarkets 是否成功（VPN 不通时会失败）
        self._markets_loaded: bool = False
        # 历史 K 线是否已成功预加载
        self._preload_done: bool = False

        # Gemini 配置
        self._gemini_api_key = os.getenv("GOOGLE_API_KEY")
        self._last_ai_analysis = "Waiting for AI analysis..."

        # 轮询间隔（根据 timeframe 动态调整，1h K 线每 60s 轮询一次）
        self._poll_interval_s: float = 60.0

        # ── 7. 收益率跟踪（Allocator/Optimizer 数据源）────────────
        self._prev_closes: Dict[str, float] = {}

        # ── 8. 组合管理层（可通过配置 enabled 开关）──────────────
        pf_cfg = self.sys_config.portfolio
        self._portfolio_enabled = pf_cfg.enabled
        if self._portfolio_enabled:
            method_map = {
                "equal_weight": AllocationMethod.EQUAL_WEIGHT,
                "risk_parity": AllocationMethod.RISK_PARITY,
                "momentum": AllocationMethod.MOMENTUM_WEIGHTED,
                "minimum_variance": AllocationMethod.MINIMUM_VARIANCE,
            }
            self.allocator = PortfolioAllocator(
                method=method_map.get(pf_cfg.allocation_method, AllocationMethod.RISK_PARITY),
                lookback_bars=pf_cfg.lookback_bars,
                weight_cap=pf_cfg.weight_cap,
                min_weight=pf_cfg.min_weight,
            )
            self.rebalancer = PortfolioRebalancer(
                allocator=self.allocator,
                rebalance_every_n=pf_cfg.rebalance_every_n,
                drift_threshold=pf_cfg.drift_threshold,
                min_trade_notional=pf_cfg.min_trade_notional,
                cash_buffer_pct=pf_cfg.cash_buffer_pct,
            )
            log.info(
                "组合管理层已启用: method={} rebalance_every={} drift={}",
                pf_cfg.allocation_method, pf_cfg.rebalance_every_n, pf_cfg.drift_threshold,
            )
        else:
            self.allocator = None
            self.rebalancer = None
            log.info("组合管理层已禁用 (portfolio.enabled=false)")

        # ── 9. 绩效归因器 ────────────────────────────────────────
        self.attributor = PerformanceAttributor()

        # ── 10. ContinuousLearner 实例表 {strategy_id: learner} ──
        self._continuous_learners: Dict[str, object] = {}

        # ── 11. Phase 3 组件（shadow-only 模式）──────────────────────
        # shadow-only：接收真实行情数据、产出决策、记录 trace，但不提交实际订单
        # 只有当 phase3.enabled=true 时初始化，第一个失败不阻断其他组件
        self._phase3_enabled: bool = False
        self._phase3_mm: Optional[object] = None       # MarketMakingStrategy (shadow)
        self._phase3_ppo: Optional[object] = None      # PPOAgent (shadow)
        self._phase3_evolution: Optional[object] = None  # SelfEvolutionEngine
        self._phase3_obs_builder: Optional[object] = None
        self._phase3_action_adapter: Optional[object] = None
        self._phase3_rl_policy_mode: str = "shadow"
        self._phase3_realtime_enabled: bool = False
        self._phase3_ws_client: Optional[ExchangeWsClient] = None
        self._phase3_subscription_manager: Optional[SubscriptionManager] = None
        self._phase3_depth_registry: Optional[DepthCacheRegistry] = None
        self._phase3_trade_registry: Optional[TradeCacheRegistry] = None
        self._phase3_micro_builder: Optional[MicroFeatureBuilder] = None

        p3_cfg = getattr(self.sys_config, "phase3", None)
        if p3_cfg is not None and getattr(p3_cfg, "enabled", False):
            self._init_phase3_components(p3_cfg)

        log.info(
            "LiveTrader 初始化完成: exchange={} mode={} symbols={}",
            exc_cfg.exchange_id,
            self.mode,
            self.sys_config.data.default_symbols,
        )

    # ────────────────────────────────────────────────────────────
    # Phase 3 生命周期
    # ────────────────────────────────────────────────────────────

    def _init_phase3_components(self, p3_cfg: object) -> None:
        """
        初始化 Phase 3 组件。

        每个组件独立初始化，失败不阻断其他组件。
        初始化完成后设置 self._phase3_enabled = True。
        """
        from modules.monitoring.trace import init_recorder

        # 初始化全局 Phase3TraceRecorder
        init_recorder()

        rl_cfg = getattr(p3_cfg, "rl", None)
        mm_cfg_raw = getattr(p3_cfg, "market_making", None)
        self._phase3_rl_policy_mode = (
            "paper"
            if self.mode == "paper"
            else getattr(rl_cfg, "policy_mode", "shadow")
        )
        self._init_phase3_realtime_runtime(p3_cfg)

        # ── 做市策略（paper 优先，live 保持 shadow） ─────────────
        try:
            from modules.alpha.market_making.strategy import (
                MarketMakingStrategy,
                MarketMakingStrategyConfig,
            )
            from modules.alpha.market_making.avellaneda_model import AvellanedaConfig
            from modules.alpha.market_making.inventory_manager import InventoryConfig

            _symbol = (
                self.sys_config.data.default_symbols[0]
                if self.sys_config.data.default_symbols
                else "BTC/USDT"
            )
            mm_cfg = MarketMakingStrategyConfig(
                symbol=_symbol,
                exchange=self.sys_config.exchange.exchange_id,
                paper_mode=(self.mode == "paper"),
                save_every_n=10 if self.mode == "paper" else 0,
                avellaneda=AvellanedaConfig(
                    gamma=float(getattr(mm_cfg_raw, "risk_aversion_gamma", 0.12)),
                ),
                inventory=InventoryConfig(
                    max_inventory_pct=float(
                        getattr(mm_cfg_raw, "max_inventory_pct", 0.20)
                    ),
                ),
            )
            self._phase3_mm = MarketMakingStrategy(mm_cfg)
            log.info(
                "[Phase3] MarketMakingStrategy 已加载: symbol={} exchange={} mode={}",
                _symbol,
                self.sys_config.exchange.exchange_id,
                "paper" if self.mode == "paper" else "shadow",
            )
        except Exception as _exc:
            log.warning("[Phase3] MarketMakingStrategy 初始化失败（已忽略）: {}", _exc)

        # ── RL Agent (paper/shadow) ───────────────────────────
        try:
            from modules.alpha.rl.action_adapter import (
                ActionAdapter,
                ActionAdapterConfig,
            )
            from modules.alpha.rl.observation_builder import ObservationBuilder
            from modules.alpha.rl.ppo_agent import PPOAgent, PPOConfig

            obs_dim = 24   # ObservationBuilder.OBS_DIM
            n_actions = 8  # ActionAdapter 的离散动作数
            ppo_cfg = PPOConfig(obs_dim=obs_dim, n_actions=n_actions)
            self._phase3_ppo = PPOAgent(ppo_cfg)
            self._phase3_obs_builder = ObservationBuilder()
            self._phase3_action_adapter = ActionAdapter(
                ActionAdapterConfig(
                    confidence_floor=float(
                        getattr(rl_cfg, "action_confidence_floor", 0.55)
                    )
                )
            )
            log.info(
                "[Phase3] PPOAgent 已加载: obs_dim={} n_actions={} policy={}",
                obs_dim, n_actions,
                self._phase3_rl_policy_mode,
            )
        except Exception as _exc:
            log.warning("[Phase3] PPOAgent 初始化失败（已忽略）: {}", _exc)

        # ── 自进化引擎 ──────────────────────────────
        try:
            from modules.evolution.self_evolution_engine import (
                SelfEvolutionEngine,
                SelfEvolutionConfig,
            )

            ev_cfg = SelfEvolutionConfig(
                state_dir="storage/phase3_evolution",
                auto_run=False,  # 手动调用 run_cycle()
            )
            self._phase3_evolution = SelfEvolutionEngine(ev_cfg)
            log.info(
                "[Phase3] SelfEvolutionEngine 已加载: state_dir=storage/phase3_evolution"
            )
        except Exception as _exc:
            log.warning("[Phase3] SelfEvolutionEngine 初始化失败（已忽略）: {}", _exc)

        self._phase3_enabled = any(
            component is not None
            for component in (
                self._phase3_mm,
                self._phase3_ppo,
                self._phase3_evolution,
            )
        )
        log.info(
            "[Phase3] 运行模式已启动: runtime_mode={} mm={} ppo={} evolution={}",
            "paper" if self.mode == "paper" else "shadow",
            self._phase3_mm is not None,
            self._phase3_ppo is not None,
            self._phase3_evolution is not None,
        )

    def _init_phase3_realtime_runtime(self, p3_cfg: object) -> None:
        """初始化 Phase 3 realtime paper runtime。"""
        try:
            rt_cfg = getattr(p3_cfg, "realtime_feed", None)
            self._phase3_depth_registry = DepthCacheRegistry(
                DepthCacheConfig(
                    max_depth=int(getattr(rt_cfg, "orderbook_depth_levels", 20)),
                )
            )
            self._phase3_trade_registry = TradeCacheRegistry(TradeCacheConfig())
            self._phase3_micro_builder = MicroFeatureBuilder()
        except Exception as exc:  # noqa: BLE001
            log.warning("[Phase3] realtime registry 初始化失败（已忽略）: {}", exc)
            self._phase3_depth_registry = None
            self._phase3_trade_registry = None
            self._phase3_micro_builder = None
            return

        self._phase3_realtime_enabled = bool(
            getattr(p3_cfg, "realtime_feed_enabled", False) and self.mode == "paper"
        )
        if not self._phase3_realtime_enabled:
            log.info(
                "[Phase3] realtime paper runtime 已准备但未启动: enabled={} mode={}",
                getattr(p3_cfg, "realtime_feed_enabled", False),
                self.mode,
            )
            return

        try:
            heartbeat_timeout = max(
                float(getattr(rt_cfg, "heartbeat_timeout_sec", 15.0)),
                self._poll_interval_s * 2,
            )
            ws_cfg = WsClientConfig(
                exchange=self.sys_config.exchange.exchange_id,
                reconnect_delay_sec=float(
                    getattr(rt_cfg, "reconnect_backoff_sec", 2.0)
                ),
                heartbeat_timeout_sec=heartbeat_timeout,
                depth_levels=int(getattr(rt_cfg, "orderbook_depth_levels", 20)),
                ws_url=getattr(rt_cfg, "ws_url", None),
            )
            self._phase3_ws_client = self._create_phase3_ws_client(rt_cfg, ws_cfg)
            self._phase3_subscription_manager = SubscriptionManager(
                self._phase3_ws_client,
                self._phase3_depth_registry,
                self._phase3_trade_registry,
                SubscriptionManagerConfig(
                    health_check_interval_sec=max(self._poll_interval_s / 2, 5.0),
                    reconnect_backoff_base_sec=float(
                        getattr(rt_cfg, "reconnect_backoff_sec", 2.0)
                    ),
                    heartbeat_timeout_sec=heartbeat_timeout,
                ),
            )
            self._phase3_subscription_manager.set_health_callback(
                self._on_phase3_feed_health_change
            )
            self._phase3_subscription_manager.start(
                list(self.sys_config.data.default_symbols)
            )
            log.info(
                "[Phase3] realtime paper feed 已启动: symbols={} exchange={}",
                self.sys_config.data.default_symbols,
                self.sys_config.exchange.exchange_id,
            )
        except Exception as exc:  # noqa: BLE001
            self._phase3_realtime_enabled = False
            self._phase3_ws_client = None
            self._phase3_subscription_manager = None
            log.warning("[Phase3] realtime paper feed 启动失败（回退到合成快照）: {}", exc)

    def _create_phase3_ws_client(
        self,
        rt_cfg: object,
        ws_cfg: WsClientConfig,
    ) -> ExchangeWsClient:
        """按配置构建 Phase 3 realtime ws client。"""
        return create_exchange_ws_client(
            str(getattr(rt_cfg, "provider", "mock") or "mock"),
            ws_cfg,
            mock_price_drift=0.0008,
            mock_seed=42,
        )
    def _on_phase3_feed_health_change(self, snapshot: object) -> None:
        """同步 Phase 3 realtime feed 健康状态到 KillSwitch。"""
        is_healthy = getattr(getattr(snapshot, "health", None), "value", "") == "healthy"
        if hasattr(self, "_kill_switch"):
            self._kill_switch.record_data_health("phase3_realtime", is_healthy)

    def _pump_phase3_realtime_feed(self, symbol: str) -> None:
        """驱动一次 mock realtime feed，产出真实 DepthCache / TradeCache 数据。"""
        client = getattr(self, "_phase3_ws_client", None)
        if client is None or not self._phase3_realtime_enabled:
            return

        if not isinstance(client, MockWsClient):
            return

        try:
            depth_delta = client.generate_depth_update(symbol)
            if depth_delta is not None:
                client.push_depth(depth_delta)
                if hasattr(self, "_kill_switch"):
                    self._kill_switch.record_data_health(f"phase3_depth:{symbol}", True)

            trade_tick = client.generate_trade(symbol)
            if trade_tick is not None:
                client.push_trade(trade_tick)
                if hasattr(self, "_kill_switch"):
                    self._kill_switch.record_data_health(f"phase3_trade:{symbol}", True)
        except Exception as exc:  # noqa: BLE001
            if hasattr(self, "_kill_switch"):
                self._kill_switch.record_data_health(f"phase3_depth:{symbol}", False)
                self._kill_switch.record_data_health(f"phase3_trade:{symbol}", False)
            log.debug("[Phase3] realtime feed pump 异常（已忽略）: symbol={} error={}", symbol, exc)

    def _get_phase3_orderbook_snapshot(
        self,
        symbol: str,
        mid_price: float,
        seq: int,
    ) -> OrderBookSnapshot:
        """优先读取 realtime registry 中的订单簿快照，失败时回退到合成快照。"""
        if self._phase3_realtime_enabled and self._phase3_depth_registry is not None:
            self._pump_phase3_realtime_feed(symbol)
            cache = self._phase3_depth_registry.get(
                symbol,
                self.sys_config.exchange.exchange_id,
            )
            if cache is not None:
                snapshot = cache.get_snapshot()
                if snapshot is not None:
                    return snapshot

        return OrderBookSnapshot.create_mock(
            symbol=symbol,
            exchange=self.sys_config.exchange.exchange_id,
            mid_price=mid_price,
            spread_bps=5.0,
            sequence_id=seq,
        )

    def _get_or_create_data_kitchen(self, symbol: str) -> DataKitchen:
        kitchen = self._phase1_data_kitchens.get(symbol)
        if kitchen is None:
            kitchen = DataKitchen()
            self._phase1_data_kitchens[symbol] = kitchen
        return kitchen

    def _get_or_create_regime_detector(self, symbol: str) -> MarketRegimeDetector:
        detector = self._phase1_regime_detectors.get(symbol)
        if detector is None:
            detector = MarketRegimeDetector()
            self._phase1_regime_detectors[symbol] = detector
        return detector

    def _build_symbol_ohlcv_frame(self, symbol: str) -> Optional[pd.DataFrame]:
        klines = self._kline_store.get(symbol, [])
        if not klines:
            return None

        frame = pd.DataFrame(klines[-600:]).copy()
        frame["timestamp"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        return frame[["timestamp", "open", "high", "low", "close", "volume"]]

    def _get_phase1_feature_views(
        self,
        symbol: str,
    ) -> Optional[Dict[str, pd.DataFrame]]:
        ohlcv_df = self._build_symbol_ohlcv_frame(symbol)
        if ohlcv_df is None or len(ohlcv_df) < 30:
            return self._phase1_feature_views.get(symbol)

        kitchen = self._get_or_create_data_kitchen(symbol)
        try:
            if kitchen.contract is None:
                views, _ = kitchen.fit(ohlcv_df)
            else:
                views = kitchen.transform(ohlcv_df, validate_contract=False)
            self._phase1_feature_views[symbol] = views
            return views
        except Exception as exc:  # noqa: BLE001
            log.debug("[Phase1] DataKitchen 处理失败: symbol={} error={}", symbol, exc)
            return self._phase1_feature_views.get(symbol)

    def _extract_feature_value(
        self,
        feature_views: Optional[Dict[str, pd.DataFrame]],
        *columns: str,
        default: Optional[float] = None,
    ) -> Optional[float]:
        if not feature_views:
            return default

        frames = [
            feature_views.get("alpha_features"),
            feature_views.get("regime_features"),
            feature_views.get("diagnostic_features"),
        ]
        for col in columns:
            for frame in frames:
                if frame is None or frame.empty or col not in frame.columns:
                    continue
                value = frame.iloc[-1][col]
                if pd.notna(value):
                    return float(value)
        return default

    def _estimate_atr_pct(
        self,
        symbol: str,
        feature_views: Optional[Dict[str, pd.DataFrame]] = None,
    ) -> Optional[float]:
        atr_pct = self._extract_feature_value(feature_views, "atr_pct_14")
        if atr_pct is not None and atr_pct > 0:
            return atr_pct

        klines = self._kline_store.get(symbol, [])
        if len(klines) < 15:
            return None

        try:
            from modules.alpha.features import FeatureEngine

            atr_series = FeatureEngine.atr_pct(pd.DataFrame(klines[-50:]), window=14)
            if not atr_series.empty:
                value = atr_series.iloc[-1]
                if pd.notna(value) and float(value) > 0:
                    return float(value)
        except Exception as exc:  # noqa: BLE001
            log.debug("[Phase2] ATR% 计算失败: symbol={} error={}", symbol, exc)
        return None

    def _extract_cooldown_symbols(self) -> dict[str, datetime]:
        cooldowns: dict[str, datetime] = {}
        try:
            active = (
                self._adaptive_risk.health_snapshot()
                .get("cooldown", {})
                .get("active_symbols", {})
            )
            for symbol, payload in active.items():
                expires_at = payload.get("expires_at")
                if not expires_at:
                    continue
                cooldowns[symbol] = datetime.fromisoformat(expires_at)
        except Exception as exc:  # noqa: BLE001
            log.debug("[Phase2] 冷却期快照提取失败: {}", exc)
        return cooldowns

    def _build_risk_snapshot(self) -> RiskSnapshot:
        if not hasattr(self, "risk_manager"):
            return RiskSnapshot.make_default()

        risk_summary = self.risk_manager.get_state_summary()
        budget_snapshot = (
            self._budget_checker.snapshot()
            if hasattr(self, "_budget_checker")
            else {"remaining_budget_pct": 1.0, "deployed_pct": 0.0}
        )

        current_equity = float(getattr(self, "_current_equity", 0.0) or 0.0)
        peak_equity = float(risk_summary.get("peak_equity") or 0.0)
        daily_start_equity = float(risk_summary.get("daily_start_equity") or 0.0)
        daily_pnl = float(risk_summary.get("daily_pnl") or 0.0)

        current_drawdown = (
            max(0.0, (peak_equity - current_equity) / peak_equity)
            if peak_equity > 0 and current_equity >= 0
            else 0.0
        )
        daily_loss_pct = (
            max(0.0, -daily_pnl / daily_start_equity)
            if daily_start_equity > 0 and daily_pnl < 0
            else 0.0
        )

        if hasattr(self, "_kill_switch"):
            self._kill_switch.try_auto_recover()

        snapshot = RiskSnapshot(
            current_drawdown=current_drawdown,
            daily_loss_pct=daily_loss_pct,
            consecutive_losses=int(risk_summary.get("consecutive_losses") or 0),
            circuit_broken=bool(risk_summary.get("circuit_broken", False)),
            kill_switch_active=bool(
                self._kill_switch.is_active if hasattr(self, "_kill_switch") else False
            ),
            budget_remaining_pct=float(
                budget_snapshot.get("remaining_budget_pct", 1.0)
            ),
            cooldown_symbols=self._extract_cooldown_symbols(),
            metadata={
                "budget_deployed_pct": float(budget_snapshot.get("deployed_pct", 0.0)),
                "kill_switch_reason": (
                    self._kill_switch.health_snapshot().get("reason", "")
                    if hasattr(self, "_kill_switch")
                    else ""
                ),
                "circuit_reason": str(risk_summary.get("circuit_reason") or ""),
            },
        )

        if hasattr(self, "_kill_switch"):
            kill_active = self._kill_switch.evaluate(snapshot)
            if kill_active != snapshot.kill_switch_active:
                snapshot = RiskSnapshot(
                    current_drawdown=snapshot.current_drawdown,
                    daily_loss_pct=snapshot.daily_loss_pct,
                    consecutive_losses=snapshot.consecutive_losses,
                    circuit_broken=snapshot.circuit_broken,
                    kill_switch_active=kill_active,
                    budget_remaining_pct=snapshot.budget_remaining_pct,
                    cooldown_symbols=snapshot.cooldown_symbols,
                    metadata=snapshot.metadata,
                )

        return snapshot

    def _record_order_rejection(
        self,
        req: OrderRequestEvent,
        quantity: Decimal,
        reason: str,
        stage: str,
    ) -> None:
        log.warning(
            "[RiskBlock:{}] strategy={} {} {} qty={} 被拒绝: {}",
            stage,
            req.strategy_id,
            req.symbol,
            req.side,
            quantity,
            reason,
        )
        self.metrics.record_order_rejected(req.symbol, reason)
        trade_log(
            event_type="REJECTED",
            strategy=req.strategy_id,
            symbol=req.symbol,
            side=req.side,
            quantity=f"{quantity}",
            reason=reason,
        )

    def _build_phase3_observation(
        self,
        symbol: str,
        seq: int,
        snapshot: OrderBookSnapshot,
        risk_snapshot: RiskSnapshot,
    ) -> Optional[object]:
        obs_builder = getattr(self, "_phase3_obs_builder", None)
        if obs_builder is None:
            return None

        feature_views = getattr(self, "_phase1_feature_views", {}).get(symbol)
        regime = getattr(self, "_symbol_regimes", {}).get(symbol, self._current_regime)

        volume_ratio = self._extract_feature_value(feature_views, "volume_ratio", default=1.0)
        technical = {
            "regime": regime.dominant_regime,
            "ma_cross": self._extract_feature_value(
                feature_views,
                "sma_10_20_spread",
                default=0.0,
            )
            or 0.0,
            "rsi": self._extract_feature_value(feature_views, "rsi_14", default=50.0)
            or 50.0,
            "macd_norm": self._extract_feature_value(
                feature_views,
                "macd_histogram",
                default=0.0,
            )
            or 0.0,
            "vol_norm": max(0.0, min((volume_ratio or 1.0) / 2.0, 1.0)),
            "price_mom_5": self._extract_feature_value(
                feature_views,
                "ret_roll_mean_5",
                "close_return_lag5",
                default=0.0,
            )
            or 0.0,
        }

        bid_qty = sum(level.size for level in snapshot.bids[:5])
        ask_qty = sum(level.size for level in snapshot.asks[:5])
        total_qty = bid_qty + ask_qty
        bid_pressure = bid_qty / total_qty if total_qty > 0 else 0.5
        ask_pressure = ask_qty / total_qty if total_qty > 0 else 0.5
        micro_builder = getattr(self, "_phase3_micro_builder", None)
        trade_registry = getattr(self, "_phase3_trade_registry", None)
        micro_frame = None
        if micro_builder is not None:
            try:
                trade_stats = (
                    trade_registry.get_stats(symbol, self.sys_config.exchange.exchange_id)
                    if trade_registry is not None
                    else None
                )
                micro_frame = micro_builder.build(snapshot, trade_stats)
                if pd.notna(micro_frame.mb_book_pressure_ratio):
                    bid_pressure = max(
                        0.0,
                        min((micro_frame.mb_book_pressure_ratio + 1.0) / 2.0, 1.0),
                    )
                    ask_pressure = 1.0 - bid_pressure
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "[Phase3] micro feature 构建失败，回退原始订单簿特征: symbol={} error={}",
                    symbol,
                    exc,
                )

        microstructure = {
            "mb_spread_bps": (
                micro_frame.mb_spread_bps
                if micro_frame is not None and pd.notna(micro_frame.mb_spread_bps)
                else snapshot.spread_bps
            ),
            "mb_imbalance": (
                micro_frame.mb_order_imbalance
                if micro_frame is not None and pd.notna(micro_frame.mb_order_imbalance)
                else snapshot.imbalance
            ),
            "mb_micro_price_dev": (
                micro_frame.mb_micro_price
                if micro_frame is not None and pd.notna(micro_frame.mb_micro_price)
                else 0.0
            ),
            "mb_bid_pressure": bid_pressure,
            "mb_ask_pressure": ask_pressure,
        }

        inventory_pct = 0.5
        mm = getattr(self, "_phase3_mm", None)
        if mm is not None:
            try:
                diagnostics = mm.diagnostics()
                if isinstance(diagnostics, dict):
                    inventory_pct = float(
                        diagnostics.get("inventory", {}).get("inventory_pct", inventory_pct)
                    )
            except Exception:  # noqa: BLE001
                pass

        position_pct = 0.0
        price = snapshot.mid_price
        equity = float(getattr(self, "_current_equity", 0.0) or 0.0)
        positions = getattr(self, "_positions", {})
        current_qty = float(positions.get(symbol, Decimal("0")))
        if equity > 0 and price > 0:
            position_pct = max(-1.0, min((current_qty * price) / equity, 1.0))

        trace_id = getattr(self, "_last_trace_ids", {}).get(
            symbol,
            f"phase3:{symbol}:{seq}",
        )
        return obs_builder.build(
            symbol=symbol,
            trace_id=trace_id,
            risk_snapshot=risk_snapshot,
            technical=technical,
            microstructure=microstructure,
            inventory_pct=inventory_pct,
            position_pct=position_pct,
            episode_step=seq,
        )

    def _build_phase3_order_request(
        self,
        symbol: str,
        policy_decision: object,
        seq: int,
    ) -> Optional[OrderRequestEvent]:
        from modules.alpha.contracts.rl_types import ActionType

        action = policy_decision.effective_action()
        if action == ActionType.HOLD:
            return None

        if action in {
            ActionType.WIDEN_QUOTE,
            ActionType.NARROW_QUOTE,
            ActionType.BIAS_BID,
            ActionType.BIAS_ASK,
        }:
            log.debug(
                "[Phase3/RL] 非方向性动作保留在 paper 观测层: symbol={} action={}",
                symbol,
                action.value,
            )
            return None

        side = "buy"
        quantity = Decimal("0")
        if action in {ActionType.SELL, ActionType.REDUCE}:
            current_qty = getattr(self, "_positions", {}).get(symbol, Decimal("0"))
            if current_qty <= 0:
                return None
            side = "sell"
            scale = 1.0 if action == ActionType.SELL else max(
                0.25,
                float(policy_decision.action.action_value),
            )
            quantity = current_qty * Decimal(str(min(scale, 1.0)))
            if quantity <= 0:
                return None

        return OrderRequestEvent(
            event_type=EventType.ORDER_REQUESTED,
            timestamp=datetime.now(tz=timezone.utc),
            source="phase3_rl",
            symbol=symbol,
            side=side,
            order_type="market",
            quantity=quantity,
            price=None,
            strategy_id=f"phase3_rl_{policy_decision.policy_version}",
            request_id=f"phase3-{seq}-{uuid4().hex[:10]}",
        )

    def _step_phase3_shadow(self, symbol: str, mid_price: float, seq: int) -> None:
        """
        Phase 3 单步驱动：
        做市进入 paper 仿真路径，RL 在 paper 模式下可进入真实的 paper 下单链路；
        live 模式仍保持 shadow-only。

        Args:
            symbol:    当前处理的交易对
            mid_price: K线收盘价（作为 mid 价格代理）
            seq:       主循环心跳序号（用于日志关联性）
        """
        if not self._phase3_enabled:
            return

        risk_snapshot = self._build_risk_snapshot()
        snapshot = self._get_phase3_orderbook_snapshot(symbol, mid_price, seq)

        # ── 做市 Tick（paper/live-shadow）────────────────────
        if (
            self._phase3_mm is not None
            and getattr(self._phase3_mm.config, "symbol", None) == symbol
        ):
            try:
                _decision = self._phase3_mm.tick(
                    snapshot,
                    risk_snapshot,
                    elapsed_sec=float(seq),
                )
                log.debug(
                    "[Phase3/MM] tick: symbol={} mode={} mid={:.4f} "
                    "bid={} ask={} allow_bid={} allow_ask={}",
                    symbol,
                    "paper" if self.mode == "paper" else "shadow",
                    snapshot.mid_price,
                    f"{_decision.bid_price:.4f}" if _decision.bid_price else "N/A",
                    f"{_decision.ask_price:.4f}" if _decision.ask_price else "N/A",
                    _decision.allow_post_bid,
                    _decision.allow_post_ask,
                )
            except Exception:  # noqa: BLE001
                log.debug("[Phase3/MM] shadow tick 异常（已忽略）: symbol={}", symbol)

        # ── RL Predict / Paper Execute ─────────────────────
        if self._phase3_ppo is not None and self._phase3_action_adapter is not None:
            try:
                _obs = self._build_phase3_observation(
                    symbol=symbol,
                    seq=seq,
                    snapshot=snapshot,
                    risk_snapshot=risk_snapshot,
                )
                if _obs is None:
                    return
                _action_idx, _action_val, _conf, _lp = self._phase3_ppo.predict(
                    _obs.feature_vector,
                    deterministic=True,
                )
                _action = self._phase3_action_adapter.index_to_action(
                    _action_idx,
                    action_value=_action_val,
                    confidence=_conf,
                )
                _decision = self._phase3_action_adapter.apply_safety(
                    _action,
                    risk_snapshot,
                    obs=_obs,
                    policy_id="phase3_rl",
                    policy_version=getattr(self._phase3_ppo, "_version", "v1"),
                )
                log.debug(
                    "[Phase3/RL] predict: symbol={} mode={} action={} confidence={:.3f} override={}",
                    symbol,
                    self._phase3_rl_policy_mode,
                    _decision.effective_action().value,
                    _conf,
                    _decision.safety_override,
                )

                if (
                    self.mode == "paper"
                    and self._phase3_rl_policy_mode in {"paper", "active"}
                    and hasattr(self, "order_manager")
                ):
                    order_req = self._build_phase3_order_request(symbol, _decision, seq)
                    if order_req is not None:
                        self._process_order_request(
                            order_req,
                            self._current_equity,
                            risk_snapshot=risk_snapshot,
                            regime=getattr(self, "_symbol_regimes", {}).get(
                                symbol,
                                self._current_regime,
                            ),
                            signal_confidence=_decision.action.confidence,
                            strategy_weight=max(_decision.action.action_value, 0.25),
                            phase_source="phase3_rl",
                        )
            except Exception:  # noqa: BLE001
                log.debug("[Phase3/RL] predict 异常（已忽略）: symbol={}", symbol)

        # ── 演进周期检查（由调度器决定是否到期）────────────
        if self._phase3_evolution is not None:
            try:
                _report = self._phase3_evolution.run_cycle(force=False)
                if _report is not None:
                    log.info(
                        "[Phase3/EV] shadow 演进周期完成: {}",
                        _report.summary(),
                    )
            except Exception:  # noqa: BLE001
                log.debug("[Phase3/EV] 演进周期检查异常（已忽略）")

    # ────────────────────────────────────────────────────────────
    # 策略注册接口
    # ────────────────────────────────────────────────────────────

    def add_strategy(self, strategy_obj) -> None:
        """
        注册策略对象（需实现 on_kline(event) → List[OrderRequestEvent]）。
        
        新增（Phase 1）：策略会自动包装为 BaseAlphaAdapter 并注册到 AlphaRuntime，
        同时仍保留在 self._strategies 列表中以保持向后兼容性。
        """
        # 向后兼容：保留在旧列表中
        self._strategies.append(strategy_obj)
        
        # Phase 1 新增：包装为 StrategyProtocol 并注册到 AlphaRuntime
        strategy_id = getattr(strategy_obj, "strategy_id", type(strategy_obj).__name__)
        wrapped_strategy = BaseAlphaAdapter(strategy=strategy_obj)
        
        # 获取策略适用的交易品种
        target_symbols = getattr(strategy_obj, "symbols", [])
        if not target_symbols:
            target_symbols = self.sys_config.data.default_symbols
        
        # 注册到 AlphaRuntime
        self._strategy_registry.register(wrapped_strategy)
        
        log.info(
            "注册实盘策略: strategy_id={} symbols={} (已包装为 StrategyProtocol)",
            strategy_id, target_symbols
        )

    # ────────────────────────────────────────────────────────────
    # 主控流程
    # ────────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        启动主控程序。现在 Uvicorn 占用主线程，交易逻辑在守护线程运行。
        """
        set_trader_instance(self)
        
        # 将原主循环丢入子线程
        trader_thread = threading.Thread(target=self._run_loop, daemon=True)
        trader_thread.start()
        
        # 主线程阻塞运行 API
        log.info("启动本地通信层 API: http://127.0.0.1:8000 (Dual Stack Support)")
        # 使用 host="::" 以支持 IPv4 和 IPv6 客户端
        uvicorn.run(fast_app, host="::", port=8000, log_level="info")

    def _run_loop(self) -> None:
        """原有的交易主引擎循环"""
        # 启动 Prometheus 指标服务
        SystemMetrics.start_http_server(port=8001)  # 改为 8001 避免与 FastAPI 冲突

        audit_log("SYSTEM_STARTUP", mode=self.mode)
        log.info("=" * 50)
        log.info("系统启动: mode={} exchange={}", self.mode, self.gateway.exchange_id)
        log.info("用户数据目录: {}", self._state_file_path.parent)
        log.info("=" * 50)

        # ── 恢复持久化状态（如有）──────────────────────────────
        self._load_state()

        # ── 确保 equity 在预加载/主循环前已正确计算 ──────────────
        # 避免 _current_equity=0 导致风控注入假值
        self._update_account_snapshot()
        # 首次启动(无状态文件)时 peak_equity 可能为 0，用真实 equity 初始化
        if self._current_equity > 0 and self.risk_manager._state.peak_equity <= 0:
            self.risk_manager.reset_baseline(self._current_equity)
            log.info(
                "首次启动: 以当前权益 {:.2f} 初始化风控基线",
                self._current_equity,
            )

        # ── 预加载历史 K 线，喂给策略暖机 ─────────────────────
        self._preload_history()

        self._running = True
        heartbeat_seq = 0

        log.info(
            "[System] 主循环就绪启动: 策略={} 组合管理={} CL实例={} 轮询间隔={}s",
            len(self._strategies),
            self._portfolio_enabled,
            len(self._continuous_learners),
            self._poll_interval_s,
        )

        while self._running:
            loop_start = time.monotonic()
            heartbeat_seq += 1

            try:
                self._main_loop_step(heartbeat_seq)
            except KeyboardInterrupt:
                break
            except Exception:  # noqa: BLE001
                log.exception("主循环异常（已记录，继续运行）: seq={}", heartbeat_seq)

            # 精确等待到下一个轮询间隔
            elapsed = time.monotonic() - loop_start
            sleep_time = max(0, self._poll_interval_s - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

        self._shutdown()

    # ────────────────────────────────────────────────────────────
    # 单次循环步骤
    # ────────────────────────────────────────────────────────────

    def _main_loop_step(self, seq: int) -> None:
        """
        一次完整的主控循环，包含以下步骤：
        1. 心跳记录
        2. 轮询最新 K 线行情
        3. 发布 KlineEvent → 策略处理
        4. 风控审核 → 发单
        5. 轮询成交回报 + 超时撤单
        6. 更新账户快照 + 指标上报
        7. 硬止损检查
        8. 每日重置检查
        """
        now = datetime.now(tz=timezone.utc)
        self.metrics.record_heartbeat()
        log.debug("主循环心跳: seq={} ts={}", seq, now.isoformat())

        # 每 10 次循环输出一次 INFO 级状态摘要（约每 10 分钟），监控各模块健康状态
        if seq % 10 == 1:
            _cl_summary = {
                sid: f"{len(l._ohlcv_buffer)}/{l.config.min_bars_for_retrain}"
                for sid, l in self._continuous_learners.items()
            }
            _alloc_warm = {
                s: self.allocator.is_warm(s)
                for s in self.sys_config.data.default_symbols
            } if (self._portfolio_enabled and self.allocator) else {}
            log.info(
                "[Loop#{}] 状态: equity={:.2f} positions={} CL_buf={} alloc_warm={}",
                seq, self._current_equity,
                {s: float(q) for s, q in self._positions.items() if q > 0},
                _cl_summary,
                _alloc_warm,
            )

        symbols = self.sys_config.data.default_symbols

        # Step 1: 拉取最新行情，构建 KlineEvent
        kline_events = self._fetch_latest_klines(symbols)
        log.debug("[Loop#{0}] 拉取K线: {1}个事件", seq, len(kline_events))

        # ── 延迟预加载检测：网络恢复后自动补充历史 K 线 ──────────
        # 若启动时预加载失败（VPN 未连、网络不通等），当主循环首次从交易所
        # 成功拿到真实行情数据时，说明网络已恢复，立即执行延迟预加载。
        if not self._preload_done and kline_events:
            has_live = any(e.source == "live_feed" for e in kline_events)
            if has_live:
                log.info(
                    "[DeferredPreload] 检测到交易所连接恢复，开始延迟预加载历史 K 线…"
                )
                try:
                    self._preload_history()
                    if self._preload_done:
                        log.info(
                            "[DeferredPreload] ✅ 历史 K 线延迟预加载成功"
                        )
                except Exception:  # noqa: BLE001
                    log.exception("[DeferredPreload] 延迟预加载异常")

        # Step 2: 驱动策略并缓存 K 线 + 收益率跟踪
        rebalance_triggered = False
        for event in kline_events:
            close_f = float(event.close)
            prev_price = self._latest_prices.get(event.symbol, 0)
            self._latest_prices[event.symbol] = close_f
            self._cache_kline_event(event)

            # ── 收益率计算（Allocator 数据源）────────────────────
            prev_close = self._prev_closes.get(event.symbol, close_f)
            if prev_close > 0:
                period_return = (close_f - prev_close) / prev_close
            else:
                period_return = 0.0
            self._prev_closes[event.symbol] = close_f

            # 喂入 Allocator
            if self._portfolio_enabled and self.allocator:
                self.allocator.update_return(event.symbol, period_return)
                log.debug(
                    "[Allocator] update_return: {} ret={:.5f} warm={}",
                    event.symbol, period_return, self.allocator.is_warm(event.symbol),
                )

            # 喂入 PerformanceAttributor (价格记录)
            self.attributor.record_price(event.symbol, close_f, now)

            # 喂入 ContinuousLearner (OHLCV)
            _sym_safe = event.symbol.replace("/", "_")
            for sid, learner in self._continuous_learners.items():
                if _sym_safe in sid:
                    ohlcv_row = {
                        "timestamp": event.timestamp,
                        "open": float(event.open),
                        "high": float(event.high),
                        "low": float(event.low),
                        "close": close_f,
                        "volume": float(event.volume),
                    }
                    new_model = learner.on_new_bar(ohlcv_row)
                    buf_len = len(learner._ohlcv_buffer)
                    # 每100根输出一次 INFO 里程碑日志，方便观察进度
                    if buf_len % 100 == 0 or buf_len == learner.config.min_bars_for_retrain:
                        log.info(
                            "[CL] {} buf={}/{} since_retrain={} 触发阈值={}",
                            sid, buf_len,
                            learner.config.max_buffer_size,
                            learner._bars_since_retrain,
                            learner.config.min_bars_for_retrain,
                        )
                    else:
                        log.debug(
                            "[CL] on_new_bar: {} buf={}/{} since_retrain={} new_model={}",
                            sid, buf_len,
                            learner.config.min_bars_for_retrain,
                            learner._bars_since_retrain,
                            new_model is not None,
                        )
                    if new_model is not None:
                        self._on_model_updated(sid, learner, new_model)

            log.debug(
                "[Loop#{0}] 处理K线: {1} close={2:.4f} (prev={3:.4f}) ret={4:.5f}",
                seq, event.symbol, close_f, prev_price, period_return,
            )
            self._process_kline_event(event)

        # Step 3: 组合再平衡检查（再平衡优先于策略信号冲突）
        if self._portfolio_enabled and self.rebalancer and kline_events:
            weights = self.allocator.compute_weights(symbols) if self.allocator else {}
            log.debug(
                "[Rebalancer] 当前目标权重: {}",
                {s: f"{w:.3f}" for s, w in weights.items()},
            )
            rebal_orders = self.rebalancer.on_bar_close(
                equity=self._current_equity or 5000.0,
                positions=dict(self._positions),
                prices=self._latest_prices,
                symbols=symbols,
            )
            if rebal_orders:
                rebalance_triggered = True
                log.info(
                    "[Rebalancer] 触发再平衡: {} 笔订单 (bar={})",
                    len(rebal_orders), self.rebalancer._bar_count,
                )
                self._process_rebalance_orders(rebal_orders)
            else:
                log.debug(
                    "[Rebalancer] bar={} bars_since_last={} 未触发再平衡",
                    self.rebalancer._bar_count,
                    self.rebalancer._bar_count - self.rebalancer._last_rebalance_bar,
                )

        # Step 4: Phase 3 运行时驱动（paper 模式进入真实 paper 集成）
        if self._phase3_enabled and kline_events:
            for _ev in kline_events:
                _price = float(_ev.close)
                self._step_phase3_shadow(_ev.symbol, _price, seq)

        # Step 5: 周期性 AI 深度分析 (如每小时一次)
        if now.minute == 0 and now.second < 10:
             self._run_ai_analysis()

        # Step 6: 轮询成交回报
        fills = self.order_manager.poll_fills()
        log.debug("[Loop#{0}] 成交回报: {1}笔", seq, len(fills))
        for fill in fills:
            self._on_fill(fill)

        # Step 6b: 撤销超时订单
        cancelled = self.order_manager.cancel_timed_out_orders()
        if cancelled > 0:
            log.warning("超时撤单 {} 笔", cancelled)

        # Step 7: 更新账户快照与指标
        self._update_account_snapshot()
        log.debug(
            "[Loop#{0}] 账户快照: equity={1:.2f} positions={2} entry_prices={3}",
            seq, self._current_equity,
            {s: float(q) for s, q in self._positions.items() if q > 0},
            {s: f"{p:.4f}" for s, p in self._entry_prices.items()},
        )

        # Step 8: 硬止损检查（熔断时强制平仓）
        self._check_stop_loss()

        # Step 9: 每日重置检测（UTC 00:00 后的第一次循环）
        self._check_daily_reset(now)


    def _fetch_latest_klines(self, symbols: List[str]) -> List[KlineEvent]:
        """
        通过 CCXT fetch_ohlcv 获取最新已收线 K 线。

        Live 模式：API 异常时跳过本次循环（不使用假数据）。
        Paper 模式：API 异常时使用 Mock 数据保持 UI 可用。
        """
        events = []
        tf = self.sys_config.data.default_timeframe
        for symbol in symbols:
            try:
                fetch_start = time.monotonic()
                candles = self.gateway.fetch_ohlcv(symbol, timeframe=tf, limit=2)
                latency_ms = (time.monotonic() - fetch_start) * 1000
                self.metrics.record_data_latency(latency_ms)

                if not candles or len(candles) < 2:
                    continue

                # 倒数第二根是最新的已收线 K 线（最后一根可能未收线）
                ts_ms, o, h, l, c, v = candles[-2]
                event = KlineEvent(
                    event_type=EventType.KLINE_UPDATED,
                    timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                    source="live_feed",
                    symbol=symbol,
                    timeframe=tf,
                    open=Decimal(str(o)),
                    high=Decimal(str(h)),
                    low=Decimal(str(l)),
                    close=Decimal(str(c)),
                    volume=Decimal(str(v)),
                    is_closed=True,  # 倒数第二根确保是已收线
                )
                events.append(event)
                if hasattr(self, "_kill_switch"):
                    self._kill_switch.record_data_health(f"kline:{symbol}", True)

                # 同时更新最新价格（用最后一根的收盘价）
                _, _, _, _, last_c, _ = candles[-1]
                self._latest_prices[symbol] = float(last_c)
                # Paper 模式：同步行情到网关（用于模拟成交价计算）
                if self.mode == "paper":
                    self.gateway.update_paper_price(symbol, float(last_c))

            except Exception as exc:  # noqa: BLE001
                err_str = str(exc)[:200]
                if hasattr(self, "_kill_switch"):
                    self._kill_switch.record_data_health(f"kline:{symbol}", False)
                if self.mode == "live":
                    # Live 模式下绝不使用假数据，跳过本次
                    log.error("获取行情失败(Live模式跳过): symbol={} error={}", symbol, err_str)
                    continue
                else:
                    # Paper 模式下使用 Mock 数据保持 UI 可用
                    log.warning("获取行情(走Mock): symbol={} error={}", symbol, err_str)
                    import random
                    mock_prices = {"BTC": 67000, "ETH": 3500, "SOL": 150}
                    base = next((v for k, v in mock_prices.items() if k in symbol), 1000)
                    mock_price = base * random.uniform(0.99, 1.01)
                    # 使用上一小时的整点时间戳（与真实 K 线对齐），
                    # 避免 datetime.now() 产生的非整点时间戳导致
                    # developing bar 追加时出现时间倒序。
                    now_utc = datetime.now(tz=timezone.utc)
                    mock_ts = now_utc.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
                    events.append(KlineEvent(
                        event_type=EventType.KLINE_UPDATED,
                        timestamp=mock_ts,
                        source="mock_feed",
                        symbol=symbol,
                        timeframe=tf,
                        open=Decimal(str(round(mock_price * 0.999, 2))),
                        high=Decimal(str(round(mock_price * 1.002, 2))),
                        low=Decimal(str(round(mock_price * 0.997, 2))),
                        close=Decimal(str(round(mock_price, 2))),
                        volume=Decimal("10.5"),
                        is_closed=True,
                    ))

        return events


    def _process_kline_event(self, event: KlineEvent) -> None:
        """
        处理 KlineEvent：通知策略 → 风控审核 → 提交订单。
        
        新增（Phase 1）：使用 AlphaRuntime 处理所有策略，而不是直接循环。
        """
        # 发布到事件总线（其他订阅者可扩展）
        self.bus.publish(event)
        if hasattr(self, "_kill_switch"):
            self._kill_switch.record_data_health(f"kline:{event.symbol}", True)

        # 更新风控状态（仅当 equity 已被正确计算时才更新，避免注入假值）
        if self._current_equity > 0:
            self.risk_manager.update_equity(self._current_equity)

        # Phase 1: 驱动所有策略通过 AlphaRuntime
        try:
            # 准备上下文和最新价格
            latest_prices = dict(self._latest_prices)
            latest_prices[event.symbol] = float(event.close)
            feature_views = self._get_phase1_feature_views(event.symbol)
            detector = self._get_or_create_regime_detector(event.symbol)
            bar_seq = self._alpha_runtime.loop_seq + 1
            regime = self._symbol_regimes.get(event.symbol, self._current_regime)
            if feature_views and not feature_views["regime_features"].empty:
                regime = detector.update_from_frame(
                    feature_views["regime_features"],
                    bar_seq=bar_seq,
                )
            else:
                ohlcv_df = self._build_symbol_ohlcv_frame(event.symbol)
                if ohlcv_df is not None and len(ohlcv_df) >= 30:
                    regime = detector.update(
                        ohlcv_df[["open", "high", "low", "close", "volume"]],
                        bar_seq=bar_seq,
                    )
            self._current_regime = regime
            self._symbol_regimes[event.symbol] = regime
            risk_snapshot = self._build_risk_snapshot()
            
            # 通过 AlphaRuntime 处理 K 线事件
            context, strategy_results = self._alpha_runtime.process_bar(
                event=event,
                latest_prices=latest_prices,
                feature_frame=(
                    feature_views["alpha_features"]
                    if feature_views is not None
                    else None
                ),
                regime=regime,
                portfolio_snapshot={
                    "positions": dict(self._positions),
                    "equity": self._current_equity,
                    "entry_prices": dict(self._entry_prices),
                },
                risk_snapshot=risk_snapshot,
            )
            self._last_trace_ids[event.symbol] = context.trace_id
            
            decision = self._phase1_orchestrator.orchestrate(
                OrchestrationInput(
                    regime=regime,
                    strategy_results=strategy_results,
                    equity=self._current_equity,
                    current_drawdown=risk_snapshot.current_drawdown,
                    is_regime_stable=detector.is_stable,
                    bar_seq=context.loop_seq,
                )
            )
            
            for result in decision.selected_results:
                weight = decision.weights.get(result.strategy_id, 1.0)
                for req in result.order_requests:
                    self._process_order_request(
                        req,
                        self._current_equity,
                        regime=regime,
                        risk_snapshot=risk_snapshot,
                        signal_confidence=result.confidence,
                        strategy_weight=weight,
                        phase_source="phase1",
                    )
                
            log.debug(
                "[AlphaRuntime] symbol={} trace_id={} strategies={} selected={} blocked={} ",
                event.symbol,
                context.trace_id,
                len(strategy_results),
                len(decision.selected_results),
                len(decision.block_reasons),
            )
            
        except Exception:  # noqa: BLE001
            log.exception("AlphaRuntime 处理异常: symbol={}", event.symbol)
            
            # 回退：直接调用策略（向后兼容）
            risk_snapshot = self._build_risk_snapshot()
            for strategy in self._strategies:
                try:
                    order_requests = strategy.on_kline(event)
                    for req in (order_requests or []):
                        self._process_order_request(
                            req,
                            self._current_equity,
                            regime=self._symbol_regimes.get(
                                event.symbol,
                                self._current_regime,
                            ),
                            risk_snapshot=risk_snapshot,
                            phase_source="legacy_fallback",
                        )
                except Exception:  # noqa: BLE001
                    log.exception("策略异常（回退模式）: strategy={}", getattr(strategy, "strategy_id", "unknown"))

    def _process_order_request(
        self,
        req: OrderRequestEvent,
        equity: float,
        *,
        regime: Optional[RegimeState] = None,
        risk_snapshot: Optional[RiskSnapshot] = None,
        signal_confidence: float = 0.5,
        strategy_weight: float = 1.0,
        phase_source: str = "legacy",
    ) -> None:
        """
        处理单个订单请求：动态仓位 → 风控审核 → 提交到 OrderManager。
        """
        risk_snapshot = risk_snapshot or self._build_risk_snapshot()

        # 记录信号指标
        self.metrics.record_signal(req.strategy_id, req.side)
        log.debug(
            "[OrderReq:{}] strategy={} symbol={} side={} qty={} price={} weight={:.3f} conf={:.3f}",
            phase_source,
            req.strategy_id,
            req.symbol,
            req.side,
            req.quantity,
            req.price,
            strategy_weight,
            signal_confidence,
        )

        # 动态仓位：买单使用波动率目标法替代固定 qty
        quantity = req.quantity
        price = float(req.price or self._latest_prices.get(req.symbol, 0))
        risk_plan = None
        if req.side == "buy":
            if hasattr(self, "_kill_switch") and self._kill_switch.evaluate(risk_snapshot):
                reason = self._kill_switch.health_snapshot().get("reason") or "Kill Switch 已激活"
                self._kill_switch.record_order_rejection(reason)
                self._record_order_rejection(req, quantity, reason, "kill-switch")
                return

            risk_plan = self._adaptive_risk.evaluate(
                symbol=req.symbol,
                risk_snapshot=risk_snapshot,
                regime=regime,
                signal_confidence=signal_confidence,
                atr_pct=self._estimate_atr_pct(
                    req.symbol,
                    self._phase1_feature_views.get(req.symbol),
                ),
            )
            if risk_plan.is_blocked:
                reason = "; ".join(risk_plan.block_reasons) or "AdaptiveRiskMatrix 阻断"
                if hasattr(self, "_kill_switch"):
                    self._kill_switch.record_order_rejection(reason)
                self._record_order_rejection(req, quantity, reason, "adaptive-risk")
                return

            if price > 0 and equity > 0:
                atr_pct = self._estimate_atr_pct(
                    req.symbol,
                    self._phase1_feature_views.get(req.symbol),
                )
                if atr_pct is not None and atr_pct > 0:
                    dynamic_qty = self.position_sizer.volatility_target(
                        equity=equity,
                        atr_pct=float(atr_pct),
                        target_vol=0.02,
                        price=price,
                    )
                    if dynamic_qty > 0:
                        log.info(
                            "[DynPos] {} 策略建议qty={} → 波动率目标qty={} "
                            "(equity={:.2f} price={:.4f} atr%={:.4f} target_vol=2%)",
                            req.symbol,
                            req.quantity,
                            dynamic_qty,
                            equity,
                            price,
                            atr_pct,
                        )
                        quantity = dynamic_qty
                    else:
                        log.debug(
                            "[DynPos] {} volatility_target 返回 0，使用策略建议 qty={}",
                            req.symbol,
                            req.quantity,
                        )
                else:
                    log.debug(
                        "[DynPos] {} ATR% 无法计算，回退到策略建议 qty={}",
                        req.symbol,
                        req.quantity,
                    )
            else:
                log.debug(
                    "[DynPos] {} price={} equity={:.2f} 无效，回退到策略建议 qty={}",
                    req.symbol, price, equity, req.quantity,
                )

            scale = max(strategy_weight, 0.0)
            if risk_plan is not None:
                scale *= max(risk_plan.position_scalar, 0.0)
            if scale <= 0:
                self._record_order_rejection(
                    req,
                    quantity,
                    "策略权重或风险乘数收缩为 0",
                    "position-scaling",
                )
                return

            quantity = self.position_sizer._round_qty(
                quantity * Decimal(str(min(scale, 1.0)))
            )
            if quantity <= 0:
                self._record_order_rejection(
                    req,
                    quantity,
                    "缩放后的下单数量低于最小下单量",
                    "position-scaling",
                )
                return

            order_value_pct = (
                float(quantity) * price / equity
                if equity > 0 and price > 0
                else 0.0
            )
            budget_allowed, budget_reason, _ = self._budget_checker.check(order_value_pct)
            if not budget_allowed:
                if hasattr(self, "_kill_switch"):
                    self._kill_switch.record_order_rejection(budget_reason)
                self._record_order_rejection(req, quantity, budget_reason, "budget")
                return

        # 风控审核
        allowed, reason = self.risk_manager.check(
            side=req.side,
            symbol=req.symbol,
            quantity=quantity,
            price=price,
            current_equity=equity,
            positions=dict(self._positions),
        )

        if not allowed:
            if hasattr(self, "_kill_switch"):
                self._kill_switch.record_order_rejection(reason)
            self._record_order_rejection(req, quantity, reason, "risk-manager")
            return

        log.info(
            "[OrderSubmit:{}] strategy={} {} {} qty={} 通过风控，提交下单",
            phase_source,
            req.strategy_id,
            req.symbol,
            req.side,
            quantity,
        )
        # 通过风控，提交订单
        try:
            self.metrics.record_order_submitted(req.symbol, req.side, req.order_type)
            submit_req = replace(req, quantity=quantity)
            self.order_manager.submit(
                symbol=submit_req.symbol,
                side=submit_req.side,
                order_type=submit_req.order_type,
                quantity=quantity,
                price=submit_req.price,
                strategy_id=submit_req.strategy_id,
                request_id=submit_req.request_id,
            )
            if req.side == "buy" and risk_plan is not None:
                self._symbol_risk_plans[req.symbol] = risk_plan
        except Exception as exc:
            if hasattr(self, "_kill_switch"):
                self._kill_switch.record_order_failure(str(exc))
            log.error("订单提交失败: {} {} 原因={}", req.symbol, req.side, exc)

    def _process_rebalance_orders(self, rebal_orders: List[RebalanceOrder]) -> None:
        """
        将 RebalanceOrder 转换为风控→发单管道执行。

        设计说明：
        - 再平衡订单不经过 PositionSizer（qty 已由 Allocator 精确计算）
        - 再平衡订单不受 max_position_pct 限制（Allocator 的 weight_cap 负责权重约束）
        - 仅受系统熔断器保护：熔断时取消全部再平衡

        RiskManager.max_position_pct 设计用于单策略单次买入上限，
        而 Allocator 管理的是整个组合权重，两者职责分离。
        """
        # 熔断检查：若已熔断，取消本次再平衡
        if self.risk_manager.is_circuit_broken():
            log.warning("[Rebalance] 系统已熔断，取消本次再平衡 (orders={})", len(rebal_orders))
            # 通知 rebalancer 本次 drift 未被执行，用于抑制空仓下的日志风暴
            if self.rebalancer:
                self.rebalancer._consecutive_drift_noop += 1
            return

        equity = self._current_equity or 5000.0
        submitted = 0
        rejected = 0
        for order in rebal_orders:
            log.info(
                "[Rebalance] {} {} qty={:.6f} notional={:.2f} reason={} weight:{:.3f}→{:.3f}",
                order.symbol, order.side, float(order.quantity), order.notional,
                order.reason, order.current_weight, order.target_weight,
            )

            # 简单安全边界：单笔名义金额不超过总净值的 50%（防止异常权重）
            if order.notional > equity * 0.5:
                log.warning(
                    "[Rebalance] {} notional={:.2f} > equity*50%={:.2f}，跳过异常订单",
                    order.symbol, order.notional, equity * 0.5,
                )
                rejected += 1
                continue

            try:
                self.order_manager.submit(
                    symbol=order.symbol,
                    side=order.side,
                    order_type="market",
                    quantity=order.quantity,
                    price=None,
                    strategy_id="portfolio_rebalancer",
                )
                submitted += 1
            except Exception as exc:
                log.error("[Rebalance] 订单提交失败: {} error={}", order.symbol, exc)
                rejected += 1

        log.info(
            "[Rebalance] 完成: submitted={} rejected={} total={}",
            submitted, rejected, len(rebal_orders),
        )
        # 有订单成功提交时，重置 drift noop 计数器
        if submitted > 0 and self.rebalancer:
            self.rebalancer._consecutive_drift_noop = 0

    def _on_model_updated(self, strategy_id: str, learner, new_model) -> None:
        """ContinuousLearner 产出新模型时的热替换回调。"""
        log.info("[CL] 新模型产出，开始热替换: strategy_id={}", strategy_id)
        replaced = False
        for s in self._strategies:
            sid = getattr(s, 'strategy_id', '')
            if sid == strategy_id:
                # 热替换模型
                if hasattr(s, 'model'):
                    old_type = getattr(s.model, 'model_type', '?')
                    s.model = new_model
                    log.info(
                        "[CL] 模型热替换成功: {} old={} → new={}",
                        sid, old_type, getattr(new_model, 'model_type', '?'),
                    )
                    replaced = True
                # 注入自适应阈值
                if hasattr(s, 'set_thresholds') and hasattr(learner, 'get_optimal_thresholds'):
                    buy_t, sell_t = learner.get_optimal_thresholds()
                    s.set_thresholds(buy_t, sell_t)
                    log.info("[CL] 自适应阈值注入: {} buy={:.3f} sell={:.3f}", sid, buy_t, sell_t)
                break
        if not replaced:
            log.warning("[CL] 未找到对应策略进行热替换: {}", strategy_id)

    def _on_fill(self, fill) -> None:
        """处理成交回报：更新持仓和入场均价，记录指标，写入交易日志。"""
        rec = fill.order_record
        fill_price = float(fill.avg_price)
        fill_qty = float(fill.new_filled_qty)
        notional = fill_qty * fill_price
        fee = notional * 0.001

        log.info(
            "[Fill] strategy={} {} {} qty={} avg_price={} order_id={}",
            rec.strategy_id, rec.symbol, rec.side,
            fill_qty, fill_price, rec.exchange_id,
        )

        pnl = 0.0
        if rec.side == "buy":
            old_qty = float(self._positions.get(rec.symbol, Decimal("0")))
            old_entry = self._entry_prices.get(rec.symbol, 0.0)
            new_qty = fill_qty
            new_price = fill_price
            # 加权平均入场价
            total_qty = old_qty + new_qty
            if total_qty > 0:
                self._entry_prices[rec.symbol] = (
                    old_entry * old_qty + new_price * new_qty
                ) / total_qty
            self._positions[rec.symbol] = (
                self._positions.get(rec.symbol, Decimal("0")) + fill.new_filled_qty
            )
            log.info(
                "[Fill] 入场价更新: {} entry={:.4f} (old_qty={} new_qty={} total_qty={})",
                rec.symbol, self._entry_prices[rec.symbol], old_qty, new_qty, total_qty,
            )
            if self._current_equity > 0:
                self._budget_checker.record_order(notional / self._current_equity)
            if rec.symbol in self._symbol_risk_plans:
                self._adaptive_risk.record_entry(
                    rec.symbol,
                    self._symbol_risk_plans[rec.symbol].cooldown_minutes,
                )
        elif rec.side == "sell":
            current = self._positions.get(rec.symbol, Decimal("0"))
            self._positions[rec.symbol] = max(
                Decimal("0"), current - fill.new_filled_qty
            )
            remaining = float(self._positions[rec.symbol])
            # 计算本笔盈亏
            entry_price = self._entry_prices.get(rec.symbol, fill_price)
            pnl = (fill_price - entry_price) * fill_qty - fee
            log.info(
                "[Fill] 卖出成交: {} remaining_qty={:.6f} entry={:.4f} pnl={:.4f}",
                rec.symbol, remaining, entry_price, pnl,
            )
            # 记录盈亏结果到风控（用于连续亏损熔断）
            self.risk_manager.record_trade_outcome(won=(pnl > 0))
            if self._current_equity > 0:
                self._budget_checker.record_close(notional / self._current_equity)
            # 清仓时移除入场价，并清除止损待处理标记
            if self._positions[rec.symbol] <= 0:
                self._entry_prices.pop(rec.symbol, None)
                self._stop_loss_pending.discard(rec.symbol)
                self._symbol_risk_plans.pop(rec.symbol, None)
                log.info("[Fill] {} 已清仓，移除入场价和止损待处理标记", rec.symbol)

        if hasattr(self, "_kill_switch"):
            self._kill_switch.record_order_success()

        self.metrics.record_order_filled(
            rec.symbol, rec.side, fill_qty, notional, fee=fee
        )

        # ── 写入专用交易日志（每日轮转） ─────────────────────
        cash_after = self.gateway.paper_cash if self.mode == "paper" else 0
        trade_log(
            event_type="FILL",
            strategy=rec.strategy_id,
            symbol=rec.symbol,
            side=rec.side,
            quantity=f"{fill_qty:.6f}",
            price=f"{fill_price:.4f}",
            notional=f"{notional:.2f}",
            fee=f"{fee:.4f}",
            pnl=f"{pnl:.4f}" if rec.side == "sell" else "N/A",
            entry_price=f"{self._entry_prices.get(rec.symbol, 0):.4f}" if rec.side == "buy" else f"{self._entry_prices.get(rec.symbol, fill_price):.4f}",
            positions={s: f"{float(q):.6f}" for s, q in self._positions.items() if q > 0},
            equity=f"{self._current_equity:.2f}",
            cash=f"{cash_after:.2f}",
        )

        # ── PerformanceAttributor 记录成交 ─────────────────────
        try:
            self.attributor.record_trade(
                symbol=rec.symbol,
                strategy_id=rec.strategy_id,
                side=rec.side,
                quantity=float(fill.new_filled_qty),
                price=float(fill.avg_price),
                timestamp=datetime.now(tz=timezone.utc),
            )
            log.debug(
                "[Attributor] 记录成交: {} {} {} qty={:.6f} price={:.4f}",
                rec.strategy_id, rec.symbol, rec.side,
                float(fill.new_filled_qty), float(fill.avg_price),
            )
        except Exception:
            log.debug("[Attributor] record_trade 失败（非关键）")

    def _update_account_snapshot(self) -> None:
        """查询账户余额，更新净值和持仓指标。"""
        try:
            balance = self.gateway.fetch_balance()
            usdt_free = balance.get("USDT", {}).get("free", 0) if isinstance(balance.get("USDT"), dict) else 0
            positions_value = sum(
                float(qty) * self._latest_prices.get(sym, 0)
                for sym, qty in self._positions.items()
            )
            self._current_equity = usdt_free + positions_value
            self.metrics.update_equity(self._current_equity)

            for sym, qty in self._positions.items():
                notional = float(qty) * self._latest_prices.get(sym, 0)
                self.metrics.update_position(sym, float(qty), notional)

        except Exception as exc:  # noqa: BLE001
            err_str = str(exc)[:200]
            if self.mode == "live":
                log.error("更新账户快照失败(Live模式保留旧值): {}", err_str)
                # Live 模式保留上一次的 equity，不注入假数据
            else:
                log.warning("更新账户快照(Paper保留旧值): {}", err_str)
                # Paper 模式也保留旧值，不再注入默认 5000

        # 每次更新后持久化状态
        self._save_state()


    def _check_daily_reset(self, now: datetime) -> None:
        """检查是否需要执行每日重置（UTC 日期变更时执行，有防重复机制）。"""
        today = now.date()
        if not hasattr(self, '_last_daily_reset_date'):
            self._last_daily_reset_date = None
        if self._last_daily_reset_date != today and now.hour == 0 and now.minute < 5:
            self.risk_manager.reset_daily(self._current_equity)
            self._budget_checker.reset_daily()
            self._last_daily_reset_date = today
            log.info("每日风控重置完成")

    # ────────────────────────────────────────────────────────────
    # 历史 K 线预加载（解决策略冷启动问题）
    # ────────────────────────────────────────────────────────────

    def _ensure_markets_loaded(self) -> bool:
        """尝试加载 CCXT 市场元数据（带重试）。返回是否成功。

        在 VPN/网络不通时可能失败，此时返回 False，后续主循环中
        检测到交易所可用后会自动重试。
        """
        if self._markets_loaded:
            return True

        for attempt in range(1, 4):
            try:
                self.gateway._exchange.load_markets()
                self._markets_loaded = True
                log.info("CCXT loadMarkets 成功 (attempt={})", attempt)
                return True
            except Exception as exc:
                log.warning(
                    "CCXT loadMarkets 失败 (attempt={}/3): {}",
                    attempt, str(exc)[:120],
                )
                if attempt < 3:
                    time.sleep(3 * attempt)  # 渐进退避 3s, 6s

        log.warning(
            "CCXT loadMarkets 3 次重试均失败，等待网络恢复后自动重试"
        )
        return False

    def _preload_history(self) -> None:
        """启动时预加载历史 K 线，让策略完成暖机（预热期）。

        若网络不通（VPN 未连接等），loadMarkets 和 fetch_ohlcv 都会失败，
        此时标记 _preload_done=False。主循环会在检测到交易所恢复后
        自动调用本方法完成延迟加载。
        """
        symbols = self.sys_config.data.default_symbols
        tf = self.sys_config.data.default_timeframe
        preload_bars = 500  # 加载 500 根历史 K 线（满足 ML min_buffer_size=300 + Allocator lookback=60）

        log.info("开始预加载历史 K 线: bars={} timeframe={}", preload_bars, tf)

        # ── 显式预加载 CCXT 市场元数据（带重试） ──
        if not self._ensure_markets_loaded():
            log.warning("预加载中止: 无法加载市场元数据，等待网络恢复")
            return

        preload_success_count = 0
        for symbol in symbols:
            candles = None
            # ── 每个品种最多重试 3 次 ──
            for attempt in range(1, 4):
                try:
                    candles = self.gateway.fetch_ohlcv(
                        symbol, timeframe=tf, limit=preload_bars,
                    )
                    if candles:
                        break
                    log.warning(
                        "预加载: {} 返回空数据 (attempt={}/3)", symbol, attempt,
                    )
                except Exception as exc:
                    log.warning(
                        "预加载: {} attempt={}/3 失败: {}",
                        symbol, attempt, str(exc)[:120],
                    )
                if attempt < 3:
                    time.sleep(2 * attempt)  # 渐进退避 2s, 4s

            try:
                if not candles:
                    log.warning("预加载: {} 所有重试失败，跳过", symbol)
                    continue

                # 除最后一根（可能未收线）外，全部喂给策略
                closed_candles = candles[:-1] if len(candles) > 1 else candles
                prev_close = None
                for ts_ms, o, h, l, c, v in closed_candles:
                    event = KlineEvent(
                        event_type=EventType.KLINE_UPDATED,
                        timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                        source="history_preload",
                        symbol=symbol,
                        timeframe=tf,
                        open=Decimal(str(o)),
                        high=Decimal(str(h)),
                        low=Decimal(str(l)),
                        close=Decimal(str(c)),
                        volume=Decimal(str(v)),
                        is_closed=True,
                    )
                    self._cache_kline_event(event) # 同时也放入前端缓存

                    # 喂给 Allocator 收益率
                    if self._portfolio_enabled and self.allocator:
                        if prev_close and prev_close > 0:
                            period_return = (float(c) - prev_close) / prev_close
                        else:
                            period_return = 0.0
                        self.allocator.update_return(symbol, period_return)
                    prev_close = float(c)

                    # 喂给 ContinuousLearner（预加载期直接追加数据，不触发重训）
                    _sym_safe_pre = symbol.replace("/", "_")
                    for sid, learner in self._continuous_learners.items():
                        if _sym_safe_pre in sid:
                            ohlcv_row = {
                                "timestamp": event.timestamp,
                                "open": float(o), "high": float(h),
                                "low": float(l), "close": float(c),
                                "volume": float(v),
                            }
                            # 预加载期直接追加缓冲区，跳过重训触发检查
                            learner._ohlcv_buffer.append(ohlcv_row)
                            learner._bar_count += 1
                            learner._bars_since_retrain += 1

                    # 只驱动策略内部状态更新，不触发下单
                    # Phase 1: 使用 AlphaRuntime 驱动（但预加载期不处理订单）
                    try:
                        _, _ = self._alpha_runtime.process_bar(
                            event=event,
                            latest_prices=dict(self._latest_prices),
                            regime=self._current_regime,
                            portfolio_snapshot={
                                "positions": dict(self._positions),
                                "equity": self._current_equity,
                                "entry_prices": dict(self._entry_prices),
                            },
                        )
                    except Exception:  # noqa: BLE001
                        # 回退：直接调用策略进行预加载状态初始化
                        for strategy in self._strategies:
                            try:
                                strategy.on_kline(event)
                            except Exception:  # noqa: BLE001
                                pass

                # 更新最新价格和 prev_closes
                _, _, _, _, last_c, _ = candles[-1]
                self._latest_prices[symbol] = float(last_c)
                self._prev_closes[symbol] = float(closed_candles[-1][4]) if closed_candles else float(last_c)
                # Paper 模式：同步行情到网关
                if self.mode == "paper":
                    self.gateway.update_paper_price(symbol, float(last_c))

                # 预加载后的状态摘要
                _sym_safe_sum = symbol.replace("/", "_")
                cl_buf = next(
                    (len(l._ohlcv_buffer) for sid, l in self._continuous_learners.items() if _sym_safe_sum in sid),
                    0,
                )
                alloc_warm = (
                    self.allocator.is_warm(symbol)
                    if (self._portfolio_enabled and self.allocator) else False
                )
                log.info(
                    "预加载完成: {} bars={} alloc_warm={} CL_buf={}",
                    symbol, len(closed_candles), alloc_warm, cl_buf,
                )
                preload_success_count += 1

            except Exception as exc:  # noqa: BLE001
                log.warning("预加载失败: {} error={}", symbol, str(exc)[:200])

        # 预加载整体摘要
        if self._portfolio_enabled and self.allocator:
            warm_symbols = [s for s in symbols if self.allocator.is_warm(s)]
            log.info("[Preload] Allocator 预热完成: {}/{} 品种已达 lookback_bars/2",
                     len(warm_symbols), len(symbols))
        if self._continuous_learners:
            for sid, learner in self._continuous_learners.items():
                log.info("[Preload] ContinuousLearner {}: buf={}/{}",
                         sid, len(learner._ohlcv_buffer), learner.config.min_bars_for_retrain)

        # ── 标记预加载是否成功（至少有一个品种加载到数据）──
        if preload_success_count > 0:
            self._preload_done = True
            log.info(
                "AUDIT: K-line history cache is now ready and populated. "
                "({}/{} 品种预加载成功)",
                preload_success_count, len(symbols),
            )
        else:
            self._preload_done = False
            log.warning(
                "AUDIT: K-line 历史预加载全部失败 ({} 品种均未成功)，"
                "等待网络恢复后主循环将自动重试",
                len(symbols),
            )

    def _cache_kline_event(self, event: KlineEvent) -> None:
        """将其缓存到内存中，供前端通过 API 请求历史轨迹。"""
        if event.symbol not in self._kline_store:
            self._kline_store[event.symbol] = []
        
        # 转换为前端友好格式
        kline = {
            "time": int(event.timestamp.timestamp()),
            "open": float(event.open),
            "high": float(event.high),
            "low": float(event.low),
            "close": float(event.close),
            "volume": float(event.volume)
        }
        
        # 避免重复（通过时间戳），并确保时间严格递增
        store = self._kline_store[event.symbol]
        if store and store[-1]["time"] == kline["time"]:
            store[-1] = kline
        elif store and kline["time"] < store[-1]["time"]:
            # 新 bar 时间早于 store 末尾（mock 数据或乱序场景）→ 跳过
            log.debug(
                "cache_kline: {} 跳过乱序 bar time={} < last={}",
                event.symbol, kline["time"], store[-1]["time"],
            )
        else:
            store.append(kline)
            # 限制长度，防止内存无限增长
            if len(store) > 600:
                self._kline_store[event.symbol] = store[-600:]

    def _run_ai_analysis(self) -> None:
        """驱动 Gemini AI 对当前盘面进行解读（如果已配置 Key）。"""
        if not self._gemini_api_key:
            return
            
        try:
            import google.generativeai as genai
            genai.configure(api_key=self._gemini_api_key)
            model = genai.GenerativeModel('gemini-1.5-flash')
            
            # 构建 Prompt: 选取最近 10 根价格
            symbol = "BTC/USDT"
            klines = self._kline_store.get(symbol, [])[-10:]
            prices = [k["close"] for k in klines]
            
            prompt = f"你是一个专业的加密货币交易员。当前 {symbol} 最近的价格序列是: {prices}。请用一段简短的话分析当前市场情绪并给出风险提示。"
            response = model.generate_content(prompt)
            self._last_ai_analysis = response.text
            log.info("Gemini AI 分析完成: {}", self._last_ai_analysis[:50] + "...")
        except Exception as e:
            log.error("Gemini AI 分析失败: {}", e)

    # ────────────────────────────────────────────────────────────
    # 状态持久化（防止重启丢失持仓）
    # ────────────────────────────────────────────────────────────

    # 旧版状态文件路径（相对于 cwd，安装目录下）
    _STATE_FILE_LEGACY = "storage/trader_state.json"

    @property
    def _state_file_path(self) -> Path:
        """
        获取状态文件的绝对路径。

        优先级：
        1. 环境变量 USER_DATA_DIR（由 Electron 设置为 %APPDATA%/AI Quant Trader/）
        2. 回退到 cwd 下的 storage/trader_state.json（开发模式）

        将状态文件存入用户数据目录（%APPDATA%），确保软件升级/卸载/重装
        均不会丢失 paper 模式下的资金和仓位数据。
        """
        user_data_dir = os.environ.get("USER_DATA_DIR")
        if user_data_dir:
            return Path(user_data_dir) / "trader_state.json"
        return Path(self._STATE_FILE_LEGACY)

    def _migrate_legacy_state(self) -> None:
        """
        将旧版安装目录下的状态文件迁移到用户数据目录（%APPDATA%）。

        仅在以下条件全部满足时执行：
        - 存在 USER_DATA_DIR 环境变量
        - 新位置尚无状态文件
        - 旧位置存在状态文件
        """
        user_data_dir = os.environ.get("USER_DATA_DIR")
        if not user_data_dir:
            return

        new_path = Path(user_data_dir) / "trader_state.json"
        old_path = Path(self._STATE_FILE_LEGACY)

        if new_path.exists() or not old_path.exists():
            return

        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(str(old_path), str(new_path))
            log.info(
                "状态文件已迁移: {} → {}",
                old_path, new_path,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("状态文件迁移失败（将从旧位置读取）: {}", exc)

    def _save_state(self) -> None:
        """将关键运行状态持久化到 JSON 文件。"""
        import json
        risk_summary = self.risk_manager.get_state_summary()
        state = {
            "positions": {sym: str(qty) for sym, qty in self._positions.items()},
            "entry_prices": self._entry_prices,
            "current_equity": self._current_equity,
            "latest_prices": self._latest_prices,
            "paper_cash": self.gateway.paper_cash if self.mode == "paper" else 0,
            "risk_peak_equity": risk_summary.get("peak_equity", 0),
            "risk_daily_start_equity": risk_summary.get("daily_start_equity", 0),
            "risk_consecutive_losses": risk_summary.get("consecutive_losses", 0),
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
        try:
            state_path = self._state_file_path
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("状态持久化失败: {}", exc)

    def _load_state(self) -> None:
        """从 JSON 文件恢复运行状态。"""
        import json

        # ── 旧版状态文件自动迁移（安装目录 → %APPDATA%）──
        self._migrate_legacy_state()

        state_path = self._state_file_path
        if not state_path.exists():
            log.info("无历史状态文件，首次启动")
            return

        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self._positions = {
                sym: Decimal(qty) for sym, qty in state.get("positions", {}).items()
            }
            self._entry_prices = state.get("entry_prices", {})
            self._current_equity = state.get("current_equity", 0.0)
            self._latest_prices = state.get("latest_prices", {})

            # 恢复 Paper 模式网关状态（现金 + 持仓 + 行情价格）
            if self.mode == "paper":
                paper_cash = state.get("paper_cash", 5000.0)
                self.gateway.set_paper_cash(paper_cash)
                self.gateway.set_paper_positions(self._positions)
                # 恢复行情价格（用于首个交易周期的成交价计算）
                for sym, price in self._latest_prices.items():
                    self.gateway.update_paper_price(sym, price)

            # 恢复风控状态（peak_equity / daily_start_equity / consecutive_losses）
            risk_peak = state.get("risk_peak_equity")
            risk_daily_start = state.get("risk_daily_start_equity")
            risk_consec = state.get("risk_consecutive_losses", 0)
            if risk_peak is not None and self._current_equity > 0:
                # 检查恢复的 peak_equity 是否会立即触发熔断
                # 若 drawdown 已超阈值，说明 peak_equity 是过时的历史高点，
                # 在新会话中应重置为当前值，避免启动即熔断
                drawdown_threshold = float(self.sys_config.risk.max_portfolio_drawdown)
                if risk_peak > 0:
                    pending_drawdown = (risk_peak - self._current_equity) / risk_peak
                else:
                    pending_drawdown = 0.0
                if pending_drawdown >= drawdown_threshold:
                    log.warning(
                        "状态恢复: peak={:.2f} equity={:.2f} drawdown={:.1f}% >= 阈值{:.0f}%，"
                        "重置 peak_equity 为当前值以避免启动即熔断",
                        risk_peak, self._current_equity,
                        pending_drawdown * 100, drawdown_threshold * 100,
                    )
                    self.risk_manager.reset_baseline(self._current_equity)
                else:
                    self.risk_manager.restore_state(
                        peak_equity=risk_peak,
                        daily_start_equity=risk_daily_start or self._current_equity,
                        consecutive_losses=risk_consec,
                    )
            elif self._current_equity > 0:
                # 旧版状态文件无风控字段 → 回退到 reset_baseline
                self.risk_manager.reset_baseline(self._current_equity)

            log.info(
                "已恢复历史状态: equity={:.2f} positions={} paper_cash={:.2f} peak={:.2f}",
                self._current_equity,
                dict(self._positions),
                state.get("paper_cash", 5000.0),
                risk_peak or self._current_equity,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("状态恢复失败（使用默认值启动）: {}", exc)

    # ────────────────────────────────────────────────────────────
    # 硬止损检查（保护资金底线）
    # ────────────────────────────────────────────────────────────

    _STOP_LOSS_PCT = 0.05  # 回退止损；优先使用 AdaptiveRiskMatrix 产出的 stop_loss_pct

    def _check_stop_loss(self) -> None:
        """
        检查所有持仓是否触及硬止损，触发则强制平仓。

        两层止损：
        1. 入场价止损：当前价格跌破入场价 × (1 - STOP_LOSS_PCT) → 单品种平仓
        2. 熔断清仓：熔断器触发时 → 全部持仓强制平仓

        防重复：_stop_loss_pending 集合记录已发出止损单的品种，fill 后移除。
        """
        active_positions = {sym: qty for sym, qty in self._positions.items() if qty > 0}
        if not active_positions:
            return

        circuit = self.risk_manager.is_circuit_broken()
        log.debug(
            "[StopLoss] 检查 {} 个持仓，熔断={} 待处理止损={}",
            len(active_positions), circuit, self._stop_loss_pending,
        )

        for sym, qty in active_positions.items():
            # 已发出止损单，等待成交，跳过重复触发
            if sym in self._stop_loss_pending:
                log.debug("[StopLoss] {} 止损单已挂出，等待成交，跳过", sym)
                continue

            current_price = self._latest_prices.get(sym)
            if not current_price or current_price <= 0:
                log.debug("[StopLoss] {} 无法获取当前价格，跳过", sym)
                continue

            should_stop = False
            reason = ""

            # ① 入场价止损
            entry_price = self._entry_prices.get(sym)
            if entry_price and entry_price > 0:
                risk_plan = self._symbol_risk_plans.get(sym)
                stop_loss_pct = (
                    risk_plan.stop_loss_pct
                    if risk_plan is not None and risk_plan.stop_loss_pct is not None
                    else self._STOP_LOSS_PCT
                )
                stop_price = entry_price * (1 - stop_loss_pct)
                loss_pct = (entry_price - current_price) / entry_price * 100
                log.debug(
                    "[StopLoss] {} entry={:.4f} stop={:.4f} current={:.4f} loss={:.2f}% stop_pct={:.2%}",
                    sym,
                    entry_price,
                    stop_price,
                    current_price,
                    loss_pct,
                    stop_loss_pct,
                )
                if current_price <= stop_price:
                    reason = (
                        f"入场价止损: entry={entry_price:.4f} "
                        f"stop={stop_price:.4f} current={current_price:.4f} "
                        f"loss={loss_pct:.2f}%"
                    )
                    should_stop = True
                    self._adaptive_risk.record_stop_loss(sym)
            else:
                log.debug("[StopLoss] {} 无入场价记录，跳过价格止损检查", sym)

            # ② 熔断清仓
            if not should_stop and circuit:
                reason = "系统已熔断，强制平仓"
                should_stop = True

            if should_stop:
                log.warning("[StopLoss] 触发平仓: {} {} qty={}", sym, reason, float(qty))
                trade_log(
                    event_type="STOP_LOSS",
                    symbol=sym,
                    side="sell",
                    quantity=f"{float(qty):.6f}",
                    current_price=f"{current_price:.4f}",
                    entry_price=f"{entry_price:.4f}" if entry_price else "N/A",
                    reason=reason,
                )
                try:
                    self.order_manager.submit(
                        symbol=sym,
                        side="sell",
                        order_type="market",
                        quantity=qty,
                        price=None,
                        strategy_id="stop_loss_system",
                    )
                    self._stop_loss_pending.add(sym)  # 标记已发出，防重复
                    audit_log(
                        "STOP_LOSS_TRIGGERED",
                        symbol=sym, quantity=float(qty), reason=reason,
                    )
                    # 同步策略层持仓状态
                    for s in self._strategies:
                        if getattr(s, 'symbol', '') == sym:
                            # v2 MLPredictor 同步接口
                            if hasattr(s, 'sync_position'):
                                s.sync_position(0.0)
                                log.debug("[StopLoss] 同步策略 {} sync_position(0)", s.strategy_id)
                            elif hasattr(s, '_in_position'):
                                s._in_position = False
                                log.debug("[StopLoss] 同步策略 {} _in_position=False", s.strategy_id)
                except Exception as exc:
                    log.error("[StopLoss] 止损平仓失败: {} error={}", sym, exc)

    # ────────────────────────────────────────────────────────────
    # 优雅退出
    # ────────────────────────────────────────────────────────────

    def _signal_handler(self, signum, frame) -> None:
        log.warning("收到信号 {}，准备优雅退出...", signum)
        self._running = False

    def _shutdown(self) -> None:
        """优雅关机：保存状态，取消所有挂单，记录审计日志，关闭连接。"""
        log.info("系统关机中...")

        # 保存状态
        self._save_state()

        # 取消所有未完成订单
        open_orders = self.order_manager.get_open_orders()
        for order in open_orders:
            try:
                self.gateway.cancel_order(order.exchange_id, order.symbol)
                log.info("关机撤单: {}", order.exchange_id)
            except Exception as exc:
                log.error("关机撤单失败: {} error={}", order.exchange_id, exc)

        self.gateway.close()

        audit_log(
            "SYSTEM_SHUTDOWN",
            mode=self.mode,
            open_orders_cancelled=len(open_orders),
            final_equity=self._current_equity,
        )
        log.info("系统已安全退出。最终净值: {:.2f} USDT", self._current_equity)

        # Phase 3 shadow 组件清理
        if self._phase3_enabled:
            if self._phase3_subscription_manager is not None:
                try:
                    self._phase3_subscription_manager.stop()
                except Exception:  # noqa: BLE001
                    log.debug("[Phase3] realtime subscription manager 停止异常（已忽略）")
            log.info("[Phase3] shadow 组件已随主进程退出")
            self._phase3_mm = None
            self._phase3_ppo = None
            self._phase3_evolution = None
            self._phase3_ws_client = None
            self._phase3_subscription_manager = None
            self._phase3_depth_registry = None
            self._phase3_trade_registry = None
            self._phase3_micro_builder = None
            self._phase3_realtime_enabled = False
            self._phase3_enabled = False




def main() -> None:
    """程序入口（通过 pyproject.toml scripts 注册）。"""
    config_path = os.environ.get("CONFIG_PATH", "configs/system.yaml")
    trader = LiveTrader(config_path=config_path)

    # 注册策略（动态导入，按需配置）
    from modules.alpha.strategies.ma_cross import MACrossStrategy
    from modules.alpha.strategies.momentum import MomentumStrategy

    # 下单量配置（基于 $5000 初始资金，单笔控制在 ~$200-300 左右）
    # 注意：买单实际数量由 PositionSizer 波动率目标法动态调整
    order_qty_map = {
        "BTC/USDT": 0.003,   # ~$200 @ BTC=$67000
        "ETH/USDT": 0.06,    # ~$210 @ ETH=$3500
        "SOL/USDT": 1.5,     # ~$225 @ SOL=$150
    }
    # Momentum 策略使用较小基础仓位（动态仓位会覆盖）
    momentum_qty_map = {
        "BTC/USDT": 0.002,
        "ETH/USDT": 0.04,
        "SOL/USDT": 1.0,
    }

    symbols = trader.sys_config.data.default_symbols
    for symbol in symbols:
        # 策略 1: MA Cross (EMA + ADX 过滤)
        ma_strategy = MACrossStrategy(
            symbol=symbol,
            fast_window=10,
            slow_window=30,
            order_qty=order_qty_map.get(symbol, 0.001),
            use_ema=True,
            adx_filter=True,
            volume_filter=True,
            timeframe=trader.sys_config.data.default_timeframe,
        )
        trader.add_strategy(ma_strategy)

        # 策略 2: Momentum (ROC + RSI)
        mom_strategy = MomentumStrategy(
            symbol=symbol,
            roc_window=10,
            roc_entry_pct=2.0,
            rsi_window=14,
            rsi_upper=70.0,
            rsi_lower=30.0,
            order_qty=momentum_qty_map.get(symbol, 0.001),
            timeframe=trader.sys_config.data.default_timeframe,
        )
        trader.add_strategy(mom_strategy)

    # ── 策略 3: ML Predictor（条件注册：需要已训练模型文件）─────────
    _register_ml_strategies(trader, symbols)

    log.info(
        "策略注册完成: {} 个策略 — {}",
        len(trader._strategies),
        [getattr(s, 'strategy_id', type(s).__name__) for s in trader._strategies],
    )
    trader.run()


def _register_ml_strategies(trader, symbols: list) -> None:
    """
    条件注册 ML 策略：如果 models/ 下存在已训练模型则注册，否则跳过。

    查找规则：
    - models/{symbol_safe}_rf_model.pkl  （如 btcusdt_rf_model.pkl）
    - models/{symbol_safe}_lgbm_model.pkl
    - 按优先级 lgbm > rf 选取
    """
    from modules.alpha.ml.predictor_v2 import MLPredictor as MLPredictorV2, PredictorConfig
    from modules.alpha.ml.model import SignalModel
    from modules.alpha.ml.feature_builder import MLFeatureBuilder, FeatureConfig
    from modules.alpha.ml.continuous_learner import ContinuousLearner, ContinuousLearnerConfig
    from modules.alpha.ml.trainer import WalkForwardTrainer
    from modules.alpha.ml.labeler import ReturnLabeler

    model_dir = Path("./models")
    if not model_dir.exists():
        log.info("[MLReg] models/ 目录不存在，跳过 ML 策略注册")
        return

    # ML 专用下单量（较小，由 PositionSizer 动态调整）
    ml_qty_map = {
        "BTC/USDT": 0.002,
        "ETH/USDT": 0.03,
        "SOL/USDT": 0.8,
    }

    registered = 0
    for symbol in symbols:
        symbol_safe = symbol.replace("/", "").lower()

        # 按优先级查找模型文件
        model_path = None
        for suffix in ["lgbm_model.pkl", "rf_model.pkl"]:
            candidate = model_dir / f"{symbol_safe}_{suffix}"
            if candidate.exists():
                model_path = candidate
                break

        if model_path is None:
            log.debug("[MLReg] {} 无已训练模型，跳过", symbol)
            continue

        try:
            log.info("[MLReg] 加载 ML 模型: {} → {}", symbol, model_path)
            model = SignalModel.load(model_path)

            # 使用较保守的配置（实际阈值在首次 ContinuousLearner 重训后自动更新）
            config = PredictorConfig(
                buy_threshold=0.60,
                sell_threshold=0.40,
                order_qty=ml_qty_map.get(symbol, 0.001),
                cooling_bars=5,
                min_buffer_size=300,
                enable_feature_cache=True,
            )

            predictor = MLPredictorV2(
                model=model,
                symbol=symbol,
                config=config,
                timeframe=trader.sys_config.data.default_timeframe,
            )
            trader.add_strategy(predictor)
            registered += 1
            log.info(
                "[MLReg] ML 策略注册成功: {} model={} thresh={}/{}",
                predictor.strategy_id, model.model_type,
                config.buy_threshold, config.sell_threshold,
            )

            # ── ContinuousLearner 接入（如已启用）─────────────────
            cl_cfg = trader.sys_config.continuous_learning
            if cl_cfg.enabled:
                try:
                    feature_builder = MLFeatureBuilder()
                    labeler = ReturnLabeler()
                    trainer = WalkForwardTrainer(
                        feature_builder=feature_builder,
                        labeler=labeler,
                        calibrate=True,
                        feature_selection=True,
                    )
                    learner_config = ContinuousLearnerConfig(
                        retrain_every_n_bars=cl_cfg.retrain_every_n_bars,
                        min_accuracy_threshold=cl_cfg.min_accuracy_threshold,
                        drift_significance=cl_cfg.drift_significance,
                        drift_check_window=cl_cfg.drift_check_window,
                        max_buffer_size=cl_cfg.max_buffer_size,
                        max_saved_versions=cl_cfg.max_saved_versions,
                        ab_test_window=cl_cfg.ab_test_window,
                        min_bars_for_retrain=cl_cfg.min_bars_for_retrain,
                    )
                    learner = ContinuousLearner(
                        trainer=trainer,
                        feature_builder=feature_builder,
                        labeler=labeler,
                        config=learner_config,
                    )
                    # 注入初始活跃模型
                    from modules.alpha.ml.continuous_learner import ModelVersion
                    init_version = ModelVersion(
                        version_id="init_loaded",
                        model=model,
                        trained_at=datetime.now(tz=timezone.utc),
                        train_bars=0,
                        oos_accuracy=0.0,
                        oos_f1=0.0,
                        is_active=True,
                        model_path=str(model_path),
                    )
                    learner._active_version = init_version
                    learner._versions.append(init_version)

                    trader._continuous_learners[predictor.strategy_id] = learner
                    log.info(
                        "[MLReg] ContinuousLearner 已绑定: {} retrain_every={}",
                        predictor.strategy_id, cl_cfg.retrain_every_n_bars,
                    )
                except Exception as cl_exc:
                    log.warning("[MLReg] ContinuousLearner 创建失败: {} error={}", symbol, cl_exc)
        except Exception as exc:
            log.warning("[MLReg] ML 策略注册失败: {} error={}", symbol, exc)

    if registered > 0:
        log.info("[MLReg] ML 策略注册完成: {} 个品种", registered)
    else:
        log.info("[MLReg] 无可用 ML 模型文件，ML 策略未注册（可通过 train_ml_model.py 训练）")



if __name__ == "__main__":
    main()
