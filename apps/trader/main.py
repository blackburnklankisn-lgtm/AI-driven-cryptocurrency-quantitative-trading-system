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

import hashlib
import json
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

import numpy as np
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
from modules.data.fusion.alignment import AlignmentConfig, SourceAligner
from modules.data.fusion.freshness import FreshnessConfig
from modules.data.onchain.cache import OnChainCache
from modules.data.onchain.collector import CollectorConfig, OnChainCollector
from modules.data.onchain.feature_builder import FeatureBuilderConfig, OnChainFeatureBuilder
from modules.data.onchain.providers import (
    CryptoQuantProvider,
    GlassnodeProvider,
    MockOnChainProvider,
    PublicOnChainProvider,
)
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
from modules.data.sentiment.cache import SentimentCache
from modules.data.sentiment.collector import SentimentCollector, SentimentCollectorConfig
from modules.data.sentiment.feature_builder import (
    SentimentFeatureBuilder,
    SentimentFeatureBuilderConfig,
)
from modules.data.sentiment.providers import (
    AlternativeMeProvider,
    CryptoCompareProvider,
    HtxSentimentProvider,
    MockSentimentProvider,
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
        self._phase2_source_aligner = SourceAligner(
            AlignmentConfig(
                max_fill_periods=max(
                    int(getattr(self.sys_config.data, "external_feature_max_fill_periods", 72)),
                    1,
                )
            )
        )
        self._phase2_external_enabled: bool = False
        self._onchain_collector: Optional[OnChainCollector] = None
        self._onchain_feature_builder: Optional[OnChainFeatureBuilder] = None
        self._sentiment_collector: Optional[SentimentCollector] = None
        self._sentiment_feature_builder: Optional[SentimentFeatureBuilder] = None

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
        self._phase3_strategy_candidates: Dict[str, str] = {}
        self._phase3_strategy_candidate_bindings: Dict[str, Dict[str, str]] = {}
        self._phase3_strategy_metric_bindings: Dict[str, str] = {}
        self._phase3_params_artifact_signatures: Dict[str, str] = {}
        self._phase3_params_optimizer_state_store: Optional[object] = None
        self._phase3_params_optimizer_state: Dict[str, Any] = {}
        self._phase3_params_optimizer_thread: Optional[threading.Thread] = None
        self._phase3_params_optimizer_running: bool = False
        self._phase3_candidate_experiments: Dict[str, str] = {}
        self._phase3_candidate_runtime_state: Dict[str, Dict[str, Any]] = {}
        self._phase3_mm_realized_trade_records: Dict[str, List[Dict[str, Any]]] = {}
        self._phase3_mm_last_realized_pnl: Dict[str, float] = {}
        self._phase3_mm_last_halt_reason: Dict[str, str] = {}

        p3_cfg = getattr(self.sys_config, "phase3", None)
        if p3_cfg is not None and getattr(p3_cfg, "enabled", False):
            self._init_phase3_components(p3_cfg)

        self._init_phase2_external_sources()

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
                weekly_params_optimizer_cron=getattr(
                    getattr(p3_cfg, "evolution", None),
                    "weekly_optimization_cron",
                    "",
                ),
            )
            self._phase3_evolution = SelfEvolutionEngine(ev_cfg)
            log.info(
                "[Phase3] SelfEvolutionEngine 已加载: state_dir=storage/phase3_evolution"
            )
        except Exception as _exc:
            log.warning("[Phase3] SelfEvolutionEngine 初始化失败（已忽略）: {}", _exc)

        if self._phase3_evolution is not None:
            try:
                self._phase3_params_optimizer_state = (
                    self._phase3_evolution.weekly_params_optimizer_state()
                )
                self._phase3_params_optimizer_state_store = None
                log.info("[Phase3] 周级参数优化状态已挂接到 SelfEvolutionEngine")
            except Exception as _exc:
                log.warning("[Phase3] 周级参数优化状态挂接失败（已忽略）: {}", _exc)
                self._phase3_params_optimizer_state_store = None
                self._phase3_params_optimizer_state = {}

        if self._phase3_evolution is not None:
            self._register_evolution_risk_params_candidate(source="phase3_init")
            if self._phase3_mm is not None:
                self._register_evolution_market_making_candidate(
                    self._phase3_mm,
                    source="phase3_init",
                )
            if self._phase3_ppo is not None:
                self._register_evolution_policy_candidate(
                    self._phase3_ppo,
                    source="phase3_init",
                )

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

    def _init_phase2_external_sources(self) -> None:
        data_cfg = self.sys_config.data
        onchain_enabled = bool(getattr(data_cfg, "onchain_enabled", False))
        sentiment_enabled = bool(getattr(data_cfg, "sentiment_enabled", False))

        if onchain_enabled:
            try:
                onchain_ttl_sec = int(getattr(data_cfg, "onchain_ttl_sec", 43_200))
                onchain_cache = OnChainCache()
                onchain_freshness = FreshnessConfig(default_ttl_sec=onchain_ttl_sec)
                self._onchain_collector = OnChainCollector(
                    provider=self._create_onchain_provider(
                        str(getattr(data_cfg, "onchain_provider", "public"))
                    ),
                    cache=onchain_cache,
                    config=CollectorConfig(
                        max_retries=1,
                        retry_backoff_sec=1.0,
                        skip_if_fresh=True,
                        freshness_config=onchain_freshness,
                    ),
                )
                self._onchain_feature_builder = OnChainFeatureBuilder(
                    cache=onchain_cache,
                    config=FeatureBuilderConfig(
                        freshness_config=onchain_freshness,
                        ttl_sec=onchain_ttl_sec,
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                self._onchain_collector = None
                self._onchain_feature_builder = None
                log.warning("[Phase2] OnChain external source 初始化失败（已忽略）: {}", exc)

        if sentiment_enabled:
            try:
                sentiment_ttl_sec = int(getattr(data_cfg, "sentiment_ttl_sec", 3_600))
                sentiment_cache = SentimentCache()
                sentiment_freshness = FreshnessConfig(default_ttl_sec=sentiment_ttl_sec)
                self._sentiment_collector = SentimentCollector(
                    provider=self._create_sentiment_provider(
                        str(getattr(data_cfg, "sentiment_provider", "htx"))
                    ),
                    cache=sentiment_cache,
                    config=SentimentCollectorConfig(
                        max_retries=1,
                        retry_backoff_sec=1.0,
                        skip_if_fresh=True,
                        freshness_config=sentiment_freshness,
                    ),
                )
                self._sentiment_feature_builder = SentimentFeatureBuilder(
                    cache=sentiment_cache,
                    config=SentimentFeatureBuilderConfig(
                        freshness_config=sentiment_freshness,
                        ttl_sec=sentiment_ttl_sec,
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                self._sentiment_collector = None
                self._sentiment_feature_builder = None
                log.warning("[Phase2] Sentiment external source 初始化失败（已忽略）: {}", exc)

        self._phase2_external_enabled = any(
            component is not None
            for component in (
                self._onchain_collector,
                self._onchain_feature_builder,
                self._sentiment_collector,
                self._sentiment_feature_builder,
            )
        )
        log.info(
            "[Phase2] external sources ready: onchain={} sentiment={} enabled={}",
            self._onchain_collector is not None,
            self._sentiment_collector is not None,
            self._phase2_external_enabled,
        )

    def _create_onchain_provider(self, provider_name: str):
        normalized = (provider_name or "public").strip().lower()
        if normalized == "public":
            return PublicOnChainProvider()
        if normalized == "glassnode":
            return GlassnodeProvider(api_key=os.getenv("GLASSNODE_API_KEY", ""))
        if normalized == "cryptoquant":
            return CryptoQuantProvider(api_key=os.getenv("CRYPTOQUANT_API_KEY", ""))
        if normalized == "mock":
            return MockOnChainProvider(seed=42)
        raise ValueError(f"未知 onchain provider: {provider_name}")

    def _create_sentiment_provider(self, provider_name: str):
        normalized = (provider_name or "htx").strip().lower()
        if normalized == "htx":
            return HtxSentimentProvider(fear_greed_provider=AlternativeMeProvider())
        if normalized == "alternative_me":
            return AlternativeMeProvider()
        if normalized == "cryptocompare":
            return CryptoCompareProvider(api_key=os.getenv("CRYPTOCOMPARE_API_KEY", ""))
        if normalized == "mock":
            return MockSentimentProvider(seed=42)
        raise ValueError(f"未知 sentiment provider: {provider_name}")

    def _collect_external_source_frame(
        self,
        symbol: str,
        collector: object,
        builder: object,
        source_label: str,
        kline_index: pd.DatetimeIndex,
    ) -> Optional[pd.DataFrame]:
        if collector is None or builder is None or len(kline_index) == 0:
            return None

        try:
            record = collector.collect(symbol)
            source_frame = (
                builder.build(symbol, record)
                if record is not None
                else builder.build_from_cache(symbol)
            )
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "[Phase2] {} collect/build 失败，尝试缓存降级: symbol={} error={}",
                source_label,
                symbol,
                exc,
            )
            try:
                source_frame = builder.build_from_cache(symbol)
            except Exception as cache_exc:  # noqa: BLE001
                log.debug(
                    "[Phase2] {} cache fallback 失败: symbol={} error={}",
                    source_label,
                    symbol,
                    cache_exc,
                )
                return None

        if source_frame is None or source_frame.is_empty:
            return None

        try:
            aligned = self._phase2_source_aligner.align(source_frame, kline_index)
            if not aligned.is_usable or aligned.aligned_frame.empty:
                return None
            return aligned.aligned_frame
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "[Phase2] {} align 失败: symbol={} error={}",
                source_label,
                symbol,
                exc,
            )
            return None

    def _build_external_feature_frame(
        self,
        symbol: str,
        kline_index: pd.DatetimeIndex,
    ) -> pd.DataFrame:
        if not self._phase2_external_enabled or len(kline_index) == 0:
            return pd.DataFrame(index=kline_index)

        frames: list[pd.DataFrame] = []
        onchain_frame = self._collect_external_source_frame(
            symbol,
            self._onchain_collector,
            self._onchain_feature_builder,
            "onchain",
            kline_index,
        )
        if onchain_frame is not None and not onchain_frame.empty:
            frames.append(onchain_frame)

        sentiment_frame = self._collect_external_source_frame(
            symbol,
            self._sentiment_collector,
            self._sentiment_feature_builder,
            "sentiment",
            kline_index,
        )
        if sentiment_frame is not None and not sentiment_frame.empty:
            frames.append(sentiment_frame)

        if not frames:
            return pd.DataFrame(index=kline_index)

        merged = pd.concat(frames, axis=1)
        merged = merged.loc[:, ~merged.columns.duplicated()]
        return merged

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

        enriched_df = ohlcv_df
        kline_index = pd.DatetimeIndex(ohlcv_df["timestamp"])
        external_frame = self._build_external_feature_frame(symbol, kline_index)
        if not external_frame.empty:
            enriched_df = (
                ohlcv_df.set_index("timestamp")
                .join(external_frame, how="left")
                .reset_index()
            )

        kitchen = self._get_or_create_data_kitchen(symbol)
        try:
            if kitchen.contract is None:
                views, _ = kitchen.fit(enriched_df)
            else:
                views = kitchen.transform(enriched_df, validate_contract=False)
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
                self._record_market_making_evolution_feedback(
                    f"phase3_mm_{_normalize_symbol_key(symbol)}",
                    _decision,
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
                    self._apply_evolution_runtime_state(_report)
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

        if self._phase3_evolution is not None and not hasattr(strategy_obj, "model"):
            self._register_evolution_strategy_params_candidate(
                strategy_obj,
                source="initial_load",
            )
        
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

        if kline_events:
            self._maybe_rollout_updated_ml_params_candidates()

        self._maybe_start_weekly_ml_params_optimization(now)

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
                self._record_evolution_risk_violation(req.strategy_id, stage="kill-switch")
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
                self._record_evolution_risk_violation(req.strategy_id, stage="adaptive-risk")
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
                self._record_evolution_risk_violation(req.strategy_id, stage="budget")
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
            self._record_evolution_risk_violation(req.strategy_id, stage="risk-manager")
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
        strategy = self._find_strategy_by_id(strategy_id)
        if strategy is not None:
            if hasattr(strategy, 'model'):
                old_type = getattr(strategy.model, 'model_type', '?')
                strategy.model = new_model
                log.info(
                    "[CL] 模型热替换成功: {} old={} → new={}",
                    strategy_id, old_type, getattr(new_model, 'model_type', '?'),
                )
                replaced = True
            if hasattr(strategy, 'set_thresholds') and hasattr(learner, 'get_optimal_thresholds'):
                buy_t, sell_t = learner.get_optimal_thresholds()
                strategy.set_thresholds(buy_t, sell_t)
                log.info("[CL] 自适应阈值注入: {} buy={:.3f} sell={:.3f}", strategy_id, buy_t, sell_t)
                current_params_candidate_id = self._get_evolution_candidate_id(
                    strategy_id,
                    slot="params",
                )
                if current_params_candidate_id is not None:
                    self._store_ml_params_candidate_runtime_state(
                        strategy_id,
                        current_params_candidate_id,
                        learner=learner,
                    )
        if replaced:
            self._register_evolution_model_candidate(
                strategy_id,
                learner,
                new_model,
                source="continuous_learner",
            )
        if not replaced:
            log.warning("[CL] 未找到对应策略进行热替换: {}", strategy_id)

    def _register_evolution_model_candidate(
        self,
        strategy_id: str,
        learner,
        model,
        *,
        source: str,
        model_path: Optional[str] = None,
    ) -> Optional[str]:
        """将当前运行中的模型版本注册为自进化候选。"""
        if self._phase3_evolution is None:
            return None

        from modules.alpha.contracts.evolution_types import CandidateType

        metadata: Dict[str, Any] = {
            "strategy_id": strategy_id,
            "family_key": strategy_id,
            "binding_slot": "model",
            "source": source,
        }
        if model_path:
            metadata["model_path"] = model_path

        version_id = None
        active_version = getattr(learner, "_active_version", None)
        if active_version is not None:
            version_id = getattr(active_version, "version_id", None)
            if getattr(active_version, "trained_at", None) is not None:
                metadata["trained_at"] = active_version.trained_at.isoformat()
            if getattr(active_version, "train_bars", None) is not None:
                metadata["train_bars"] = active_version.train_bars
            if getattr(active_version, "oos_accuracy", None) is not None:
                metadata["oos_accuracy"] = active_version.oos_accuracy
            if getattr(active_version, "oos_f1", None) is not None:
                metadata["oos_f1"] = active_version.oos_f1
            if getattr(active_version, "model_path", None):
                metadata["artifact_model_path"] = active_version.model_path

        if version_id is None and learner is not None and hasattr(learner, "get_model_version_info"):
            try:
                versions = learner.get_model_version_info()
            except Exception:  # noqa: BLE001
                versions = []
            if isinstance(versions, list) and versions:
                info = next((item for item in versions if item.get("is_active")), versions[-1])
                version_id = info.get("version_id")
                metadata.update({
                    key: info[key]
                    for key in ("trained_at", "train_bars", "oos_accuracy", "oos_f1", "model_path")
                    if key in info and info[key] is not None
                })

        if version_id is None and model_path:
            version_id = Path(model_path).stem
        if version_id is None:
            version_id = f"{_normalize_symbol_key(strategy_id)}_{int(datetime.now(tz=timezone.utc).timestamp())}"

        current_candidate_id = self._get_evolution_candidate_id(strategy_id, slot="model")
        if current_candidate_id is not None:
            try:
                current = self._phase3_evolution.get_candidate(current_candidate_id)
            except Exception:  # noqa: BLE001
                current = None
            if current is not None and current.version == version_id:
                self._store_ml_candidate_runtime_state(strategy_id, current_candidate_id)
                return current_candidate_id

        snap = self._phase3_evolution.register_candidate(
            CandidateType.MODEL,
            owner=f"ml/{getattr(model, 'model_type', 'unknown')}",
            version=version_id,
            metadata=metadata,
        )
        self._bind_evolution_candidate(
            strategy_id,
            snap.candidate_id,
            slot="model",
            set_metric_owner=True,
        )
        self._store_ml_candidate_runtime_state(strategy_id, snap.candidate_id)
        log.info(
            "[Phase3/EV] 模型候选已注册: strategy_id={} candidate_id={} version={} source={}",
            strategy_id,
            snap.candidate_id,
            version_id,
            source,
        )
        self._activate_or_stage_evolution_candidate(
            snap.candidate_id,
            strategy_id=strategy_id,
            family_key=metadata["family_key"],
            source=source,
        )
        self._sync_evolution_strategy_metrics(strategy_id)
        return snap.candidate_id

    def _register_evolution_params_candidate(
        self,
        strategy_id: str,
        learner,
        runtime_artifacts: Dict[str, Any],
        *,
        source: str,
        set_metric_owner: bool = False,
    ) -> Optional[str]:
        """将当前运行中的阈值/训练参数注册为自进化参数候选。"""
        if self._phase3_evolution is None:
            return None

        strategy = self._find_strategy_by_id(strategy_id)
        if strategy is None:
            return None

        from modules.alpha.contracts.evolution_types import CandidateType

        cfg = getattr(strategy, "cfg", None)
        buy_threshold = runtime_artifacts.get("buy_threshold")
        sell_threshold = runtime_artifacts.get("sell_threshold")
        if cfg is not None:
            if buy_threshold is None:
                buy_threshold = getattr(cfg, "buy_threshold", None)
            if sell_threshold is None:
                sell_threshold = getattr(cfg, "sell_threshold", None)

        trainer_model_type = runtime_artifacts.get("trainer_model_type")
        trainer_model_params = dict(runtime_artifacts.get("trainer_model_params") or {})
        trainer = getattr(learner, "trainer", None)
        if trainer is not None:
            if trainer_model_type is None:
                trainer_model_type = getattr(trainer, "model_type", None)
            if not trainer_model_params:
                trainer_model_params = dict(getattr(trainer, "model_params", {}) or {})

        version_payload = {
            "buy_threshold": buy_threshold,
            "sell_threshold": sell_threshold,
            "trainer_model_type": trainer_model_type,
            "trainer_model_params": trainer_model_params,
            "params_source": runtime_artifacts.get("params_source"),
            "threshold_source": runtime_artifacts.get("threshold_source"),
        }
        artifact_signature = self._ml_params_artifact_signature(version_payload)
        version_digest = artifact_signature[:10]
        version = (
            f"bt{float(buy_threshold or 0.0):.3f}_st{float(sell_threshold or 0.0):.3f}_{version_digest}"
            .replace(".", "p")
        )

        current_candidate_id = self._get_evolution_candidate_id(strategy_id, slot="params")
        if current_candidate_id is not None:
            try:
                current = self._phase3_evolution.get_candidate(current_candidate_id)
            except Exception:  # noqa: BLE001
                current = None
            if current is not None and current.version == version:
                self._store_ml_params_candidate_runtime_state(
                    strategy_id,
                    current_candidate_id,
                    learner=learner,
                    runtime_artifacts=runtime_artifacts,
                )
                self._phase3_params_artifact_signatures[strategy_id] = artifact_signature
                if set_metric_owner:
                    restored = self._restore_ml_params_candidate_runtime_state(
                        strategy_id,
                        current_candidate_id,
                    )
                    if restored:
                        self._set_metric_owner_binding(strategy_id, "params")
                        self._sync_evolution_strategy_metrics(strategy_id)
                return current_candidate_id

        metadata = {
            "strategy_id": strategy_id,
            "family_key": f"{strategy_id}/params",
            "binding_slot": "params",
            "source": source,
            "buy_threshold": buy_threshold,
            "sell_threshold": sell_threshold,
            "trainer_model_type": trainer_model_type,
            "trainer_model_params": trainer_model_params,
            "params_source": runtime_artifacts.get("params_source"),
            "threshold_source": runtime_artifacts.get("threshold_source"),
        }

        snap = self._phase3_evolution.register_candidate(
            CandidateType.PARAMS,
            owner="ml/params",
            version=version,
            metadata=metadata,
        )
        self._bind_evolution_candidate(
            strategy_id,
            snap.candidate_id,
            slot="params",
            set_metric_owner=set_metric_owner,
        )
        self._store_ml_params_candidate_runtime_state(
            strategy_id,
            snap.candidate_id,
            learner=learner,
            runtime_artifacts=runtime_artifacts,
        )
        self._phase3_params_artifact_signatures[strategy_id] = artifact_signature
        log.info(
            "[Phase3/EV] 参数候选已注册: strategy_id={} candidate_id={} version={} source={} owner={}",
            strategy_id,
            snap.candidate_id,
            version,
            source,
            "params" if set_metric_owner else self._phase3_strategy_metric_bindings.get(strategy_id, "model"),
        )
        self._activate_or_stage_evolution_candidate(
            snap.candidate_id,
            strategy_id=strategy_id,
            family_key=metadata["family_key"],
            source=source,
        )
        if set_metric_owner:
            restored = self._restore_ml_params_candidate_runtime_state(
                strategy_id,
                snap.candidate_id,
            )
            if restored:
                self._set_metric_owner_binding(strategy_id, "params")
                self._sync_evolution_strategy_metrics(strategy_id)
        return snap.candidate_id

    @staticmethod
    def _params_payload_signature(payload: Dict[str, Any]) -> str:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha1(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _apply_marshaled_param_payload(target: Any, payload: Dict[str, Any]) -> None:
        for key, value in payload.items():
            current = getattr(target, key, None)
            if isinstance(current, Decimal):
                setattr(target, key, Decimal(str(value)))
            else:
                setattr(target, key, value)

    def _extract_strategy_params_payload(self, strategy) -> Optional[Dict[str, Any]]:
        if strategy is None or hasattr(strategy, "model"):
            return None

        if all(
            hasattr(strategy, attr)
            for attr in (
                "fast_window",
                "slow_window",
                "order_qty",
                "use_ema",
                "adx_filter",
                "adx_entry_threshold",
                "adx_close_threshold",
                "volume_filter",
                "vol_ma_window",
                "vol_multiplier",
                "timeframe",
            )
        ):
            return {
                "fast_window": int(strategy.fast_window),
                "slow_window": int(strategy.slow_window),
                "order_qty": float(strategy.order_qty),
                "use_ema": bool(strategy.use_ema),
                "adx_filter": bool(strategy.adx_filter),
                "adx_entry_threshold": float(strategy.adx_entry_threshold),
                "adx_close_threshold": float(strategy.adx_close_threshold),
                "volume_filter": bool(strategy.volume_filter),
                "vol_ma_window": int(strategy.vol_ma_window),
                "vol_multiplier": float(strategy.vol_multiplier),
                "timeframe": str(strategy.timeframe),
            }

        if all(
            hasattr(strategy, attr)
            for attr in (
                "roc_window",
                "roc_entry_pct",
                "rsi_window",
                "rsi_upper",
                "rsi_lower",
                "atr_window",
                "order_qty",
                "timeframe",
            )
        ):
            return {
                "roc_window": int(strategy.roc_window),
                "roc_entry_pct": float(strategy.roc_entry_pct),
                "rsi_window": int(strategy.rsi_window),
                "rsi_upper": float(strategy.rsi_upper),
                "rsi_lower": float(strategy.rsi_lower),
                "atr_window": int(strategy.atr_window),
                "order_qty": float(strategy.order_qty),
                "timeframe": str(strategy.timeframe),
            }

        return None

    def _extract_risk_params_payload(self) -> Optional[Dict[str, Any]]:
        payload: Dict[str, Any] = {}

        risk_manager_cfg = getattr(getattr(self, "risk_manager", None), "config", None)
        if risk_manager_cfg is not None:
            payload["risk_manager"] = {
                "max_position_pct": float(getattr(risk_manager_cfg, "max_position_pct", 0.0)),
                "max_portfolio_drawdown": float(
                    getattr(risk_manager_cfg, "max_portfolio_drawdown", 0.0)
                ),
                "max_daily_loss": float(getattr(risk_manager_cfg, "max_daily_loss", 0.0)),
                "max_consecutive_losses": int(
                    getattr(risk_manager_cfg, "max_consecutive_losses", 0)
                ),
                "circuit_breaker_cooldown_minutes": int(
                    getattr(risk_manager_cfg, "circuit_breaker_cooldown_minutes", 0)
                ),
            }

        adaptive_cfg = getattr(getattr(self, "_adaptive_risk", None), "config", None)
        if adaptive_cfg is not None:
            payload["adaptive_risk"] = {
                "max_drawdown_for_entry": float(
                    getattr(adaptive_cfg, "max_drawdown_for_entry", 0.0)
                ),
                "max_daily_loss_for_entry": float(
                    getattr(adaptive_cfg, "max_daily_loss_for_entry", 0.0)
                ),
                "drawdown_scalar_per_pct": float(
                    getattr(adaptive_cfg, "drawdown_scalar_per_pct", 0.0)
                ),
                "drawdown_scalar_floor": float(
                    getattr(adaptive_cfg, "drawdown_scalar_floor", 0.0)
                ),
                "low_confidence_threshold": float(
                    getattr(adaptive_cfg, "low_confidence_threshold", 0.0)
                ),
                "high_confidence_threshold": float(
                    getattr(adaptive_cfg, "high_confidence_threshold", 0.0)
                ),
                "low_confidence_scalar": float(
                    getattr(adaptive_cfg, "low_confidence_scalar", 0.0)
                ),
                "high_confidence_scalar": float(
                    getattr(adaptive_cfg, "high_confidence_scalar", 0.0)
                ),
                "high_vol_scalar": float(getattr(adaptive_cfg, "high_vol_scalar", 0.0)),
                "unknown_regime_scalar": float(
                    getattr(adaptive_cfg, "unknown_regime_scalar", 0.0)
                ),
                "max_position_scalar": float(
                    getattr(adaptive_cfg, "max_position_scalar", 0.0)
                ),
                "default_cooldown_minutes": int(
                    getattr(adaptive_cfg, "default_cooldown_minutes", 0)
                ),
            }

        budget_cfg = getattr(getattr(self, "_budget_checker", None), "config", None)
        if budget_cfg is not None:
            payload["budget_checker"] = {
                "max_budget_usage_pct": float(
                    getattr(budget_cfg, "max_budget_usage_pct", 0.0)
                ),
                "max_single_order_budget_pct": float(
                    getattr(budget_cfg, "max_single_order_budget_pct", 0.0)
                ),
                "fee_reserve_pct": float(getattr(budget_cfg, "fee_reserve_pct", 0.0)),
                "slippage_reserve_pct": float(
                    getattr(budget_cfg, "slippage_reserve_pct", 0.0)
                ),
                "dca_budget_cap_pct": float(
                    getattr(budget_cfg, "dca_budget_cap_pct", 0.0)
                ),
                "min_order_budget_pct": float(
                    getattr(budget_cfg, "min_order_budget_pct", 0.0)
                ),
                "intraday_budget_cap_pct": float(
                    getattr(budget_cfg, "intraday_budget_cap_pct", 0.0)
                ),
            }

        kill_switch_cfg = getattr(getattr(self, "_kill_switch", None), "config", None)
        if kill_switch_cfg is not None:
            payload["kill_switch"] = {
                "drawdown_trigger": float(getattr(kill_switch_cfg, "drawdown_trigger", 0.0)),
                "daily_loss_trigger": float(getattr(kill_switch_cfg, "daily_loss_trigger", 0.0)),
                "max_consecutive_rejections": int(
                    getattr(kill_switch_cfg, "max_consecutive_rejections", 0)
                ),
                "max_consecutive_failures": int(
                    getattr(kill_switch_cfg, "max_consecutive_failures", 0)
                ),
                "stale_data_timeout_sec": int(
                    getattr(kill_switch_cfg, "stale_data_timeout_sec", 0)
                ),
                "stale_sources_trigger_count": int(
                    getattr(kill_switch_cfg, "stale_sources_trigger_count", 0)
                ),
                "auto_recover_minutes": int(
                    getattr(kill_switch_cfg, "auto_recover_minutes", 0)
                ),
            }

        position_sizer = getattr(self, "position_sizer", None)
        if position_sizer is not None:
            payload["position_sizer"] = {
                "max_position_pct": float(
                    getattr(position_sizer, "_max_position_pct", 0.0)
                )
            }

        return payload or None

    def _collect_phase3_param_optimization_targets(self) -> List[Dict[str, Any]]:
        targets: List[Dict[str, Any]] = []
        for strategy in getattr(self, "_strategies", []):
            strategy_id = getattr(strategy, "strategy_id", "")
            if not strategy_id:
                continue
            if hasattr(strategy, "model"):
                targets.append(
                    {
                        "target_kind": "ml_strategy",
                        "strategy_id": strategy_id,
                        "symbol": getattr(strategy, "symbol", None),
                    }
                )
                continue

            params_payload = self._extract_strategy_params_payload(strategy)
            if isinstance(params_payload, dict) and params_payload:
                targets.append(
                    {
                        "target_kind": "strategy_params",
                        "strategy_id": strategy_id,
                        "params_payload": params_payload,
                    }
                )

        risk_payload = self._extract_risk_params_payload()
        if isinstance(risk_payload, dict) and risk_payload:
            targets.append(
                {
                    "target_kind": "risk_params",
                    "strategy_id": "phase2_risk_runtime",
                    "params_payload": risk_payload,
                }
            )
        return targets

    def _store_strategy_params_candidate_runtime_state(
        self,
        strategy_id: str,
        candidate_id: str,
        params_payload: Dict[str, Any],
    ) -> None:
        self._ensure_phase3_candidate_bindings()
        self._phase3_candidate_runtime_state[candidate_id] = {
            "strategy_id": strategy_id,
            "runtime_kind": "strategy_params",
            "binding_slot": "params",
            "params_payload": dict(params_payload),
            "metric_owner": self._phase3_strategy_metric_bindings.get(strategy_id) == "params",
        }

    def _store_risk_params_candidate_runtime_state(
        self,
        strategy_id: str,
        candidate_id: str,
        params_payload: Dict[str, Any],
    ) -> None:
        self._ensure_phase3_candidate_bindings()
        self._phase3_candidate_runtime_state[candidate_id] = {
            "strategy_id": strategy_id,
            "runtime_kind": "risk_params",
            "binding_slot": "params",
            "params_payload": dict(params_payload),
            "metric_owner": False,
        }

    def _register_evolution_strategy_params_candidate(
        self,
        strategy,
        *,
        source: str,
        set_metric_owner: bool = True,
    ) -> Optional[str]:
        if self._phase3_evolution is None:
            return None

        strategy_id = getattr(strategy, "strategy_id", "")
        params_payload = self._extract_strategy_params_payload(strategy)
        if not strategy_id or not isinstance(params_payload, dict) or not params_payload:
            return None

        from modules.alpha.contracts.evolution_types import CandidateType

        payload_signature = self._params_payload_signature(params_payload)
        version = f"params_{payload_signature[:12]}"
        current_candidate_id = self._get_evolution_candidate_id(strategy_id, slot="params")
        if current_candidate_id is not None:
            try:
                current = self._phase3_evolution.get_candidate(current_candidate_id)
            except Exception:
                current = None
            if current is not None and current.version == version:
                self._store_strategy_params_candidate_runtime_state(
                    strategy_id,
                    current_candidate_id,
                    params_payload,
                )
                if set_metric_owner:
                    restored = self._restore_strategy_params_candidate_runtime_state(
                        strategy_id,
                        current_candidate_id,
                    )
                    if restored:
                        self._set_metric_owner_binding(strategy_id, "params")
                        self._sync_evolution_strategy_metrics(strategy_id)
                return current_candidate_id

        metadata = {
            "strategy_id": strategy_id,
            "family_key": f"{strategy_id}/params",
            "binding_slot": "params",
            "source": source,
            "params_kind": "strategy_params",
            "strategy_class": type(strategy).__name__,
            "params_payload": dict(params_payload),
        }
        snap = self._phase3_evolution.register_candidate(
            CandidateType.PARAMS,
            owner=f"strategy/{type(strategy).__name__.lower()}",
            version=version,
            metadata=metadata,
        )
        self._bind_evolution_candidate(
            strategy_id,
            snap.candidate_id,
            slot="params",
            set_metric_owner=set_metric_owner,
        )
        self._store_strategy_params_candidate_runtime_state(
            strategy_id,
            snap.candidate_id,
            params_payload,
        )
        self._activate_or_stage_evolution_candidate(
            snap.candidate_id,
            strategy_id=strategy_id,
            family_key=metadata["family_key"],
            source=source,
        )
        if set_metric_owner:
            restored = self._restore_strategy_params_candidate_runtime_state(
                strategy_id,
                snap.candidate_id,
            )
            if restored:
                self._set_metric_owner_binding(strategy_id, "params")
                self._sync_evolution_strategy_metrics(strategy_id)
        return snap.candidate_id

    def _register_evolution_risk_params_candidate(
        self,
        *,
        source: str,
    ) -> Optional[str]:
        if self._phase3_evolution is None:
            return None

        strategy_id = "phase2_risk_runtime"
        params_payload = self._extract_risk_params_payload()
        if not isinstance(params_payload, dict) or not params_payload:
            return None

        from modules.alpha.contracts.evolution_types import CandidateType

        payload_signature = self._params_payload_signature(params_payload)
        version = f"risk_{payload_signature[:12]}"
        current_candidate_id = self._get_evolution_candidate_id(strategy_id, slot="params")
        if current_candidate_id is not None:
            try:
                current = self._phase3_evolution.get_candidate(current_candidate_id)
            except Exception:
                current = None
            if current is not None and current.version == version:
                self._store_risk_params_candidate_runtime_state(
                    strategy_id,
                    current_candidate_id,
                    params_payload,
                )
                return current_candidate_id

        metadata = {
            "strategy_id": strategy_id,
            "family_key": f"{strategy_id}/params",
            "binding_slot": "params",
            "source": source,
            "params_kind": "risk_params",
            "params_payload": dict(params_payload),
        }
        snap = self._phase3_evolution.register_candidate(
            CandidateType.PARAMS,
            owner="risk/params",
            version=version,
            metadata=metadata,
        )
        self._bind_evolution_candidate(
            strategy_id,
            snap.candidate_id,
            slot="params",
            set_metric_owner=False,
        )
        self._store_risk_params_candidate_runtime_state(
            strategy_id,
            snap.candidate_id,
            params_payload,
        )
        self._activate_or_stage_evolution_candidate(
            snap.candidate_id,
            strategy_id=strategy_id,
            family_key=metadata["family_key"],
            source=source,
        )
        return snap.candidate_id

    def _find_strategy_by_id(self, strategy_id: str):
        for strategy in getattr(self, "_strategies", []):
            if getattr(strategy, "strategy_id", "") == strategy_id:
                return strategy
        return None

    def _ensure_phase3_candidate_bindings(self) -> None:
        if not hasattr(self, "_phase3_strategy_candidates"):
            self._phase3_strategy_candidates = {}
        if not hasattr(self, "_phase3_strategy_candidate_bindings"):
            self._phase3_strategy_candidate_bindings = {}
        if not hasattr(self, "_phase3_strategy_metric_bindings"):
            self._phase3_strategy_metric_bindings = {}
        if not hasattr(self, "_phase3_params_artifact_signatures"):
            self._phase3_params_artifact_signatures = {}

    def _bind_evolution_candidate(
        self,
        strategy_id: str,
        candidate_id: str,
        *,
        slot: str,
        set_metric_owner: bool = False,
    ) -> None:
        self._ensure_phase3_candidate_bindings()
        bindings = self._phase3_strategy_candidate_bindings.setdefault(strategy_id, {})
        bindings[slot] = candidate_id

        if strategy_id not in self._phase3_strategy_candidates:
            self._phase3_strategy_candidates[strategy_id] = candidate_id

        if set_metric_owner:
            self._set_metric_owner_binding(strategy_id, slot)

    def _get_evolution_candidate_id(
        self,
        strategy_id: str,
        *,
        slot: Optional[str] = None,
        metric_owner: bool = False,
    ) -> Optional[str]:
        self._ensure_phase3_candidate_bindings()
        bindings = self._phase3_strategy_candidate_bindings.get(strategy_id, {})

        if slot is None and metric_owner:
            slot = self._phase3_strategy_metric_bindings.get(strategy_id)

        if slot is not None:
            candidate_id = bindings.get(slot)
            if candidate_id is not None:
                return candidate_id
            if slot != "params":
                return self._phase3_strategy_candidates.get(strategy_id)
            return None

        return self._phase3_strategy_candidates.get(strategy_id)

    def _set_metric_owner_binding(self, strategy_id: str, slot: str) -> None:
        self._ensure_phase3_candidate_bindings()
        candidate_id = self._get_evolution_candidate_id(strategy_id, slot=slot)
        if candidate_id is None:
            return

        self._phase3_strategy_metric_bindings[strategy_id] = slot
        self._phase3_strategy_candidates[strategy_id] = candidate_id

        bindings = self._phase3_strategy_candidate_bindings.get(strategy_id, {})
        for binding_slot, binding_candidate_id in bindings.items():
            state = self._phase3_candidate_runtime_state.get(binding_candidate_id)
            if isinstance(state, dict):
                state["metric_owner"] = binding_slot == slot

    @staticmethod
    def _default_metric_binding_slot(active_slots: List[str]) -> Optional[str]:
        for slot in ("model", "policy", "strategy", "params"):
            if slot in active_slots:
                return slot
        return None

    def _get_candidate_binding_slot(
        self,
        *,
        candidate=None,
        candidate_id: Optional[str] = None,
    ) -> str:
        metadata = getattr(candidate, "metadata", {}) or {}
        slot = metadata.get("binding_slot")
        if isinstance(slot, str) and slot:
            return slot

        state = self._phase3_candidate_runtime_state.get(candidate_id or "")
        if isinstance(state, dict):
            slot = state.get("binding_slot")
            if isinstance(slot, str) and slot:
                return slot

            runtime_kind = state.get("runtime_kind")
            if runtime_kind == "ml_params":
                return "params"
            if runtime_kind == "rl_policy":
                return "policy"
            if runtime_kind == "market_making":
                return "strategy"
            if runtime_kind == "ml_model":
                return "model"

        candidate_type = getattr(candidate, "candidate_type", None)
        if candidate_type == "params":
            return "params"
        if candidate_type == "policy":
            return "policy"
        if candidate_type == "strategy":
            return "strategy"

        strategy_id = metadata.get("strategy_id")
        if isinstance(strategy_id, str):
            if strategy_id.startswith("phase3_rl_"):
                return "policy"
            if strategy_id.startswith("phase3_mm_"):
                return "strategy"

        return "model"

    def _get_evolution_candidate_strategy_id(
        self,
        *,
        candidate=None,
        candidate_id: Optional[str] = None,
    ) -> Optional[str]:
        metadata = getattr(candidate, "metadata", {}) or {}
        strategy_id = metadata.get("strategy_id")
        if isinstance(strategy_id, str) and strategy_id:
            return strategy_id

        if candidate_id is None and candidate is not None:
            candidate_id = getattr(candidate, "candidate_id", None)
        if not candidate_id:
            return None

        state = self._phase3_candidate_runtime_state.get(candidate_id)
        if isinstance(state, dict):
            strategy_id = state.get("strategy_id")
            if isinstance(strategy_id, str) and strategy_id:
                return strategy_id

        if self._phase3_evolution is not None:
            try:
                snapshot = self._phase3_evolution.get_candidate(candidate_id)
            except Exception:  # noqa: BLE001
                snapshot = None
            metadata = getattr(snapshot, "metadata", {}) or {}
            strategy_id = metadata.get("strategy_id")
            if isinstance(strategy_id, str) and strategy_id:
                return strategy_id

        for strategy_id, bindings in self._phase3_strategy_candidate_bindings.items():
            if candidate_id in bindings.values():
                return strategy_id
        return None

    def _ml_params_artifact_signature(self, runtime_artifacts: Dict[str, Any]) -> str:
        payload = {
            "buy_threshold": runtime_artifacts.get("buy_threshold"),
            "sell_threshold": runtime_artifacts.get("sell_threshold"),
            "trainer_model_type": runtime_artifacts.get("trainer_model_type"),
            "trainer_model_params": dict(runtime_artifacts.get("trainer_model_params") or {}),
            "params_source": runtime_artifacts.get("params_source"),
            "threshold_source": runtime_artifacts.get("threshold_source"),
        }
        return hashlib.sha1(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def _load_strategy_ml_runtime_artifacts(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        strategy = self._find_strategy_by_id(strategy_id)
        if strategy is None or not hasattr(strategy, "model"):
            return None

        symbol = getattr(strategy, "symbol", None)
        if not isinstance(symbol, str) or not symbol:
            return None

        model_dir = Path("./models")
        if not model_dir.exists():
            return None

        learner = self._continuous_learners.get(strategy_id)
        active_version = getattr(learner, "_active_version", None)
        model_path_raw = getattr(active_version, "model_path", None)
        model_path = Path(model_path_raw) if model_path_raw else None
        return _load_ml_runtime_artifacts(model_dir, symbol, model_path)

    def _maybe_rollout_updated_ml_params_candidates(self) -> None:
        if self._phase3_evolution is None:
            return

        for strategy in getattr(self, "_strategies", []):
            strategy_id = getattr(strategy, "strategy_id", "")
            if not strategy_id or not hasattr(strategy, "model"):
                continue

            runtime_artifacts = self._load_strategy_ml_runtime_artifacts(strategy_id)
            if not isinstance(runtime_artifacts, dict) or not runtime_artifacts:
                continue

            artifact_signature = self._ml_params_artifact_signature(runtime_artifacts)
            previous_signature = self._phase3_params_artifact_signatures.get(strategy_id)
            if previous_signature is None:
                self._phase3_params_artifact_signatures[strategy_id] = artifact_signature
                continue
            if artifact_signature == previous_signature:
                continue

            learner = self._continuous_learners.get(strategy_id)
            candidate_id = self._register_evolution_params_candidate(
                strategy_id,
                learner,
                runtime_artifacts,
                source="artifact_refresh",
                set_metric_owner=True,
            )
            self._phase3_params_artifact_signatures[strategy_id] = artifact_signature
            if candidate_id is None:
                continue

            log.info(
                "[Phase3/EV] 参数工件变更已rollout: strategy_id={} candidate_id={} threshold_source={} params_source={}",
                strategy_id,
                candidate_id,
                runtime_artifacts.get("threshold_source"),
                runtime_artifacts.get("params_source"),
            )

    def _save_phase3_params_optimizer_state(self, **updates: Any) -> None:
        state = dict(getattr(self, "_phase3_params_optimizer_state", {}) or {})
        state.update(updates)

        evolution = getattr(self, "_phase3_evolution", None)
        if evolution is not None and callable(
            getattr(evolution, "save_weekly_params_optimizer_state", None)
        ):
            try:
                self._phase3_params_optimizer_state = (
                    evolution.save_weekly_params_optimizer_state(state)
                )
                return
            except Exception as exc:
                log.warning("[Phase3/EV] 周级参数优化状态写入演进引擎失败: {}", exc)

        self._phase3_params_optimizer_state = state

        state_store = getattr(self, "_phase3_params_optimizer_state_store", None)
        if state_store is None:
            return

        try:
            state_store.save_scheduler_state(state)
        except Exception as exc:
            log.warning("[Phase3/EV] 周级参数优化状态保存失败: {}", exc)

    @staticmethod
    def _cron_field_matches(
        expression: str,
        value: int,
        *,
        minimum: int,
        maximum: int,
        sunday_is_zero: bool = False,
    ) -> bool:
        expression = (expression or "").strip()
        if not expression:
            return False
        if expression == "*":
            return True

        def _normalize(raw: int) -> int:
            if sunday_is_zero and raw == 7:
                return 0
            return raw

        try:
            for token in expression.split(","):
                token = token.strip()
                if not token:
                    continue

                step = 1
                if "/" in token:
                    token, step_text = token.split("/", 1)
                    step = max(1, int(step_text))

                if token == "*":
                    start, end = minimum, maximum
                    if start <= value <= end and (value - start) % step == 0:
                        return True
                    continue

                if "-" in token:
                    start_text, end_text = token.split("-", 1)
                    start = _normalize(int(start_text))
                    end = _normalize(int(end_text))
                    if start <= value <= end and (value - start) % step == 0:
                        return True
                    continue

                candidate = _normalize(int(token))
                if minimum <= candidate <= maximum and candidate == value:
                    return True
        except ValueError:
            return False

        return False

    def _current_weekly_ml_params_optimization_slot(
        self,
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        now = now or datetime.now(timezone.utc)
        evolution_cfg = getattr(getattr(self.sys_config, "phase3", None), "evolution", None)
        cron_expr = getattr(evolution_cfg, "weekly_optimization_cron", "")
        if not isinstance(cron_expr, str) or not cron_expr.strip():
            return None

        fields = cron_expr.split()
        if len(fields) != 5:
            log.warning("[Phase3/EV] 非法 weekly_optimization_cron，已跳过: {}", cron_expr)
            return None

        minute_expr, hour_expr, day_expr, month_expr, weekday_expr = fields
        weekday_value = now.isoweekday() % 7
        if not self._cron_field_matches(minute_expr, now.minute, minimum=0, maximum=59):
            return None
        if not self._cron_field_matches(hour_expr, now.hour, minimum=0, maximum=23):
            return None
        if not self._cron_field_matches(day_expr, now.day, minimum=1, maximum=31):
            return None
        if not self._cron_field_matches(month_expr, now.month, minimum=1, maximum=12):
            return None
        if not self._cron_field_matches(
            weekday_expr,
            weekday_value,
            minimum=0,
            maximum=6,
            sunday_is_zero=True,
        ):
            return None

        return now.replace(second=0, microsecond=0).isoformat()

    def _start_weekly_ml_params_optimization(self, slot_id: str) -> bool:
        if self._phase3_params_optimizer_running:
            return False

        worker = threading.Thread(
            target=self._run_weekly_ml_params_optimization,
            args=(slot_id,),
            daemon=True,
            name=f"phase3-weekly-params-{slot_id}",
        )
        self._phase3_params_optimizer_thread = worker
        self._phase3_params_optimizer_running = True
        worker.start()
        return True

    def _maybe_start_weekly_ml_params_optimization(
        self,
        now: Optional[datetime] = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        evolution = getattr(self, "_phase3_evolution", None)
        if evolution is not None and callable(
            getattr(evolution, "get_due_weekly_params_optimizer_slot", None)
        ):
            slot_id = evolution.get_due_weekly_params_optimizer_slot(now)
            try:
                self._phase3_params_optimizer_state = (
                    evolution.weekly_params_optimizer_state()
                )
            except Exception:
                pass
        else:
            slot_id = self._current_weekly_ml_params_optimization_slot(now)
        if slot_id is None:
            return

        if self._phase3_params_optimizer_state.get("last_successful_slot") == slot_id:
            return

        worker = self._phase3_params_optimizer_thread
        if self._phase3_params_optimizer_running or (
            worker is not None and worker.is_alive()
        ):
            return

        if evolution is not None and callable(
            getattr(evolution, "record_weekly_params_optimizer_start", None)
        ):
            try:
                self._phase3_params_optimizer_state = (
                    evolution.record_weekly_params_optimizer_start(slot_id, now=now)
                )
            except Exception as exc:
                log.warning("[Phase3/EV] 周级参数优化启动记录失败，回退本地状态: {}", exc)
                self._save_phase3_params_optimizer_state(
                    last_attempted_slot=slot_id,
                    last_attempted_at=now.isoformat(),
                    status="running",
                )
        else:
            self._save_phase3_params_optimizer_state(
                last_attempted_slot=slot_id,
                last_attempted_at=now.isoformat(),
                status="running",
            )
        if self._start_weekly_ml_params_optimization(slot_id):
            log.info("[Phase3/EV] 周级参数优化任务已启动: slot={}", slot_id)

    def _build_weekly_ml_optimization_dataframe(
        self,
        symbol: str,
        *,
        lookback_bars: int = 1200,
        min_bars: int = 900,
    ) -> Optional[pd.DataFrame]:
        timeframe = getattr(getattr(self.sys_config, "data", None), "default_timeframe", "1m")
        candles = self.gateway.fetch_ohlcv(symbol, timeframe=timeframe, limit=lookback_bars)
        if not candles or len(candles) <= 1:
            return None

        rows: List[Dict[str, Any]] = []
        for candle in candles[:-1]:
            if not isinstance(candle, (list, tuple)) or len(candle) < 6:
                continue
            timestamp_ms, open_, high, low, close, volume = candle[:6]
            rows.append(
                {
                    "timestamp": pd.to_datetime(timestamp_ms, unit="ms", utc=True),
                    "open": float(open_),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                    "volume": float(volume),
                    "symbol": symbol,
                }
            )

        if len(rows) < min_bars:
            return None

        return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)

    def _run_weekly_ml_params_optimization(self, slot_id: str) -> None:
        optimized_symbols: List[Dict[str, Any]] = []
        errors: Dict[str, str] = {}
        evolution = getattr(self, "_phase3_evolution", None)

        try:
            from scripts.optimize_phase1_params import optimize_params_from_dataframe

            seen_symbols: Set[str] = set()
            for target in self._collect_phase3_param_optimization_targets():
                if target.get("target_kind") != "ml_strategy":
                    continue

                strategy_id = str(target.get("strategy_id") or "")
                symbol = target.get("symbol")
                if (
                    not strategy_id
                    or not isinstance(symbol, str)
                    or not symbol
                    or symbol in seen_symbols
                ):
                    continue

                seen_symbols.add(symbol)
                try:
                    df = self._build_weekly_ml_optimization_dataframe(symbol)
                    if df is None or df.empty:
                        errors[strategy_id] = "insufficient_ohlcv_data"
                        continue

                    result = optimize_params_from_dataframe(
                        df,
                        symbol=symbol,
                        output_dir=Path("./models"),
                    )
                    candidate_id = None
                    learner = self._continuous_learners.get(strategy_id)
                    runtime_artifacts = self._load_strategy_ml_runtime_artifacts(strategy_id)
                    if learner is not None and isinstance(runtime_artifacts, dict) and runtime_artifacts:
                        candidate_id = self._register_evolution_params_candidate(
                            strategy_id,
                            learner,
                            runtime_artifacts,
                            source="weekly_optimization",
                            set_metric_owner=False,
                        )
                    optimized_symbols.append(
                        {
                            "strategy_id": strategy_id,
                            "symbol": symbol,
                            "rows": int(len(df)),
                            "runtime_threshold_path": str(result["runtime_threshold_path"]),
                            "runtime_params_path": str(result["runtime_params_path"]),
                            "candidate_id": candidate_id,
                        }
                    )
                except Exception as exc:
                    errors[strategy_id] = str(exc)
                    log.exception(
                        "[Phase3/EV] 周级参数优化失败: strategy_id={} symbol={}",
                        strategy_id,
                        symbol,
                    )

            finished_at = datetime.now(timezone.utc)
            if optimized_symbols:
                status = "success" if not errors else "partial_success"
                if evolution is not None and callable(
                    getattr(evolution, "record_weekly_params_optimizer_finish", None)
                ):
                    self._phase3_params_optimizer_state = (
                        evolution.record_weekly_params_optimizer_finish(
                            slot_id,
                            status=status,
                            optimized_symbols=optimized_symbols,
                            errors=errors or None,
                            now=finished_at,
                        )
                    )
                else:
                    self._save_phase3_params_optimizer_state(
                        status=status,
                        last_successful_slot=slot_id,
                        last_successful_run_at=finished_at.isoformat(),
                        last_finished_at=finished_at.isoformat(),
                        optimized_symbols=optimized_symbols,
                        last_error=errors or None,
                    )
                log.info(
                    "[Phase3/EV] 周级参数优化完成: slot={} optimized_symbols={}",
                    slot_id,
                    [item.get("symbol") for item in optimized_symbols],
                )
                return

            status = "skipped" if not errors else "error"
            if evolution is not None and callable(
                getattr(evolution, "record_weekly_params_optimizer_finish", None)
            ):
                self._phase3_params_optimizer_state = (
                    evolution.record_weekly_params_optimizer_finish(
                        slot_id,
                        status=status,
                        optimized_symbols=[],
                        errors=errors or None,
                        now=finished_at,
                    )
                )
            else:
                self._save_phase3_params_optimizer_state(
                    status=status,
                    last_finished_at=finished_at.isoformat(),
                    optimized_symbols=[],
                    last_error=errors or None,
                )
            if errors:
                log.warning(
                    "[Phase3/EV] 周级参数优化未产出工件: slot={} errors={}",
                    slot_id,
                    errors,
                )
            else:
                log.info("[Phase3/EV] 周级参数优化跳过: slot={} reason=no_ml_strategies", slot_id)
        except Exception as exc:
            finished_at = datetime.now(timezone.utc)
            if evolution is not None and callable(
                getattr(evolution, "record_weekly_params_optimizer_finish", None)
            ):
                self._phase3_params_optimizer_state = (
                    evolution.record_weekly_params_optimizer_finish(
                        slot_id,
                        status="error",
                        optimized_symbols=[],
                        errors={"task": str(exc)},
                        now=finished_at,
                    )
                )
            else:
                self._save_phase3_params_optimizer_state(
                    status="error",
                    last_finished_at=finished_at.isoformat(),
                    last_error=str(exc),
                )
            log.exception("[Phase3/EV] 周级参数优化任务执行失败: slot={}", slot_id)
        finally:
            self._phase3_params_optimizer_running = False

    def _store_ml_candidate_runtime_state(self, strategy_id: str, candidate_id: str) -> None:
        self._ensure_phase3_candidate_bindings()
        strategy = self._find_strategy_by_id(strategy_id)
        if strategy is None or not hasattr(strategy, "model"):
            return

        state: Dict[str, Any] = {
            "strategy_id": strategy_id,
            "runtime_kind": "ml_model",
            "binding_slot": "model",
            "model": strategy.model,
            "metric_owner": self._phase3_strategy_metric_bindings.get(strategy_id) == "model",
        }
        cfg = getattr(strategy, "cfg", None)
        if cfg is not None:
            state["buy_threshold"] = getattr(cfg, "buy_threshold", None)
            state["sell_threshold"] = getattr(cfg, "sell_threshold", None)

        self._phase3_candidate_runtime_state[candidate_id] = state

    def _store_ml_params_candidate_runtime_state(
        self,
        strategy_id: str,
        candidate_id: str,
        *,
        learner=None,
        runtime_artifacts: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._ensure_phase3_candidate_bindings()
        strategy = self._find_strategy_by_id(strategy_id)
        if strategy is None:
            return

        cfg = getattr(strategy, "cfg", None)
        buy_threshold = None
        sell_threshold = None
        trainer_model_type = None
        trainer_model_params: Dict[str, Any] = {}
        params_source = None
        threshold_source = None

        if isinstance(runtime_artifacts, dict):
            buy_threshold = runtime_artifacts.get("buy_threshold")
            sell_threshold = runtime_artifacts.get("sell_threshold")
            trainer_model_type = runtime_artifacts.get("trainer_model_type")
            trainer_model_params = dict(runtime_artifacts.get("trainer_model_params") or {})
            params_source = runtime_artifacts.get("params_source")
            threshold_source = runtime_artifacts.get("threshold_source")

        if cfg is not None:
            if buy_threshold is None:
                buy_threshold = getattr(cfg, "buy_threshold", None)
            if sell_threshold is None:
                sell_threshold = getattr(cfg, "sell_threshold", None)

        if learner is None:
            learner = self._continuous_learners.get(strategy_id)
        trainer = getattr(learner, "trainer", None)
        if trainer is not None:
            if trainer_model_type is None:
                trainer_model_type = getattr(trainer, "model_type", None)
            if not trainer_model_params:
                trainer_model_params = dict(getattr(trainer, "model_params", {}) or {})

        self._phase3_candidate_runtime_state[candidate_id] = {
            "strategy_id": strategy_id,
            "runtime_kind": "ml_params",
            "binding_slot": "params",
            "metric_owner": self._phase3_strategy_metric_bindings.get(strategy_id) == "params",
            "buy_threshold": buy_threshold,
            "sell_threshold": sell_threshold,
            "trainer_model_type": trainer_model_type,
            "trainer_model_params": trainer_model_params,
            "params_source": params_source,
            "threshold_source": threshold_source,
        }

    def _store_policy_candidate_runtime_state(self, strategy_id: str, candidate_id: str, policy) -> None:
        self._ensure_phase3_candidate_bindings()
        if policy is None:
            return

        self._phase3_candidate_runtime_state[candidate_id] = {
            "strategy_id": strategy_id,
            "runtime_kind": "rl_policy",
            "binding_slot": "policy",
            "policy": policy,
            "policy_mode": self._phase3_rl_policy_mode,
            "metric_owner": self._phase3_strategy_metric_bindings.get(strategy_id) == "policy",
        }

    def _store_market_making_candidate_runtime_state(self, strategy_id: str, candidate_id: str, strategy) -> None:
        self._ensure_phase3_candidate_bindings()
        if strategy is None:
            return

        self._phase3_candidate_runtime_state[candidate_id] = {
            "strategy_id": strategy_id,
            "runtime_kind": "market_making",
            "binding_slot": "strategy",
            "strategy": strategy,
            "metric_owner": self._phase3_strategy_metric_bindings.get(strategy_id) == "strategy",
        }

    def _restore_ml_candidate_runtime_state(self, strategy_id: str, candidate_id: str) -> bool:
        state = self._phase3_candidate_runtime_state.get(candidate_id)
        if not isinstance(state, dict) or state.get("strategy_id") != strategy_id:
            return False

        strategy = self._find_strategy_by_id(strategy_id)
        if strategy is None or not hasattr(strategy, "model"):
            return False

        strategy.model = state["model"]
        buy_threshold = state.get("buy_threshold")
        sell_threshold = state.get("sell_threshold")
        if (
            buy_threshold is not None
            and sell_threshold is not None
            and hasattr(strategy, "set_thresholds")
        ):
            strategy.set_thresholds(float(buy_threshold), float(sell_threshold))

        return True

    def _restore_policy_candidate_runtime_state(self, strategy_id: str, candidate_id: str) -> bool:
        state = self._phase3_candidate_runtime_state.get(candidate_id)
        if not isinstance(state, dict) or state.get("strategy_id") != strategy_id:
            return False

        policy = state.get("policy")
        if policy is None:
            return False

        self._phase3_ppo = policy
        policy_mode = state.get("policy_mode")
        if isinstance(policy_mode, str) and policy_mode:
            self._phase3_rl_policy_mode = policy_mode
        return True

    def _restore_ml_params_candidate_runtime_state(self, strategy_id: str, candidate_id: str) -> bool:
        state = self._phase3_candidate_runtime_state.get(candidate_id)
        if not isinstance(state, dict) or state.get("strategy_id") != strategy_id:
            return False

        strategy = self._find_strategy_by_id(strategy_id)
        if strategy is None:
            return False

        buy_threshold = state.get("buy_threshold")
        sell_threshold = state.get("sell_threshold")
        restored = False
        if buy_threshold is not None and sell_threshold is not None:
            if hasattr(strategy, "set_thresholds"):
                strategy.set_thresholds(float(buy_threshold), float(sell_threshold))
            else:
                cfg = getattr(strategy, "cfg", None)
                if cfg is not None:
                    cfg.buy_threshold = float(buy_threshold)
                    cfg.sell_threshold = float(sell_threshold)
            restored = True

        learner = self._continuous_learners.get(strategy_id)
        if learner is not None:
            if buy_threshold is not None:
                learner._optimal_buy_threshold = float(buy_threshold)
                restored = True
            if sell_threshold is not None:
                learner._optimal_sell_threshold = float(sell_threshold)
                restored = True

            trainer = getattr(learner, "trainer", None)
            if trainer is not None:
                trainer_model_type = state.get("trainer_model_type")
                if isinstance(trainer_model_type, str) and trainer_model_type:
                    trainer.model_type = trainer_model_type
                    restored = True

                trainer_model_params = state.get("trainer_model_params")
                if isinstance(trainer_model_params, dict):
                    trainer.model_params = dict(trainer_model_params)
                    restored = True

        return restored

    def _restore_strategy_params_candidate_runtime_state(self, strategy_id: str, candidate_id: str) -> bool:
        state = self._phase3_candidate_runtime_state.get(candidate_id)
        if not isinstance(state, dict) or state.get("strategy_id") != strategy_id:
            return False

        strategy = self._find_strategy_by_id(strategy_id)
        params_payload = state.get("params_payload")
        if strategy is None or not isinstance(params_payload, dict) or not params_payload:
            return False

        self._apply_marshaled_param_payload(strategy, params_payload)
        return True

    def _restore_risk_params_candidate_runtime_state(self, strategy_id: str, candidate_id: str) -> bool:
        state = self._phase3_candidate_runtime_state.get(candidate_id)
        if not isinstance(state, dict) or state.get("strategy_id") != strategy_id:
            return False

        params_payload = state.get("params_payload")
        if not isinstance(params_payload, dict) or not params_payload:
            return False

        restored = False
        risk_manager_cfg = getattr(getattr(self, "risk_manager", None), "config", None)
        if risk_manager_cfg is not None and isinstance(params_payload.get("risk_manager"), dict):
            self._apply_marshaled_param_payload(
                risk_manager_cfg,
                params_payload["risk_manager"],
            )
            restored = True

        adaptive_cfg = getattr(getattr(self, "_adaptive_risk", None), "config", None)
        if adaptive_cfg is not None and isinstance(params_payload.get("adaptive_risk"), dict):
            self._apply_marshaled_param_payload(
                adaptive_cfg,
                params_payload["adaptive_risk"],
            )
            restored = True

        budget_cfg = getattr(getattr(self, "_budget_checker", None), "config", None)
        if budget_cfg is not None and isinstance(params_payload.get("budget_checker"), dict):
            self._apply_marshaled_param_payload(
                budget_cfg,
                params_payload["budget_checker"],
            )
            restored = True

        kill_switch_cfg = getattr(getattr(self, "_kill_switch", None), "config", None)
        if kill_switch_cfg is not None and isinstance(params_payload.get("kill_switch"), dict):
            self._apply_marshaled_param_payload(
                kill_switch_cfg,
                params_payload["kill_switch"],
            )
            restored = True

        position_sizer_cfg = params_payload.get("position_sizer")
        if isinstance(position_sizer_cfg, dict) and self.position_sizer is not None:
            max_position_pct = position_sizer_cfg.get("max_position_pct")
            if max_position_pct is not None:
                self.position_sizer._max_position_pct = Decimal(str(max_position_pct))
                restored = True

        return restored

    def _restore_params_candidate_runtime_state(self, strategy_id: str, candidate_id: str) -> bool:
        state = self._phase3_candidate_runtime_state.get(candidate_id)
        if not isinstance(state, dict) or state.get("strategy_id") != strategy_id:
            return False

        runtime_kind = state.get("runtime_kind")
        if runtime_kind == "ml_params":
            return self._restore_ml_params_candidate_runtime_state(strategy_id, candidate_id)
        if runtime_kind == "strategy_params":
            return self._restore_strategy_params_candidate_runtime_state(
                strategy_id,
                candidate_id,
            )
        if runtime_kind == "risk_params":
            return self._restore_risk_params_candidate_runtime_state(
                strategy_id,
                candidate_id,
            )
        return False

    def _restore_market_making_candidate_runtime_state(self, strategy_id: str, candidate_id: str) -> bool:
        state = self._phase3_candidate_runtime_state.get(candidate_id)
        if not isinstance(state, dict) or state.get("strategy_id") != strategy_id:
            return False

        strategy = state.get("strategy")
        if strategy is None:
            return False

        self._phase3_mm = strategy
        return True

    def _restore_evolution_slot_runtime_state(self, strategy_id: str, slot: str) -> bool:
        candidate_id = self._get_evolution_candidate_id(strategy_id, slot=slot)
        if candidate_id is None:
            return False

        if slot == "model":
            return self._restore_ml_candidate_runtime_state(strategy_id, candidate_id)
        if slot == "params":
            return self._restore_params_candidate_runtime_state(strategy_id, candidate_id)
        if slot == "policy":
            return self._restore_policy_candidate_runtime_state(strategy_id, candidate_id)
        if slot == "strategy":
            return self._restore_market_making_candidate_runtime_state(strategy_id, candidate_id)
        return False

    def _get_evolution_realized_trade_pnls(
        self,
        strategy_id: str,
        *,
        limit: Optional[int] = None,
        end_time: Optional[datetime] = None,
    ) -> List[float]:
        if strategy_id.startswith("phase3_mm_"):
            records = list(self._phase3_mm_realized_trade_records.get(strategy_id, []))
            if end_time is not None:
                records = [r for r in records if r.get("timestamp") <= end_time]
            if limit is not None and limit > 0:
                records = records[-limit:]
            return [float(r.get("pnl", 0.0)) for r in records]

        if hasattr(self, "attributor"):
            return self.attributor.get_strategy_realized_trade_pnls(
                strategy_id,
                limit=limit,
                end_time=end_time,
            )
        return []

    def _get_market_making_evolution_metrics(
        self,
        strategy_id: str,
        *,
        min_samples: int = 5,
    ) -> Dict[str, float]:
        pnl_series = np.array(
            self._get_evolution_realized_trade_pnls(strategy_id),
            dtype=float,
        )
        if len(pnl_series) < min_samples:
            return {}

        mean_pnl = float(np.mean(pnl_series))
        std_pnl = float(np.std(pnl_series, ddof=1)) if len(pnl_series) > 1 else 0.0
        if std_pnl > 1e-12:
            sharpe_proxy = mean_pnl / std_pnl * float(np.sqrt(len(pnl_series)))
        elif abs(mean_pnl) > 1e-12:
            sharpe_proxy = float(np.sign(mean_pnl) * min(np.sqrt(len(pnl_series)), 3.0))
        else:
            sharpe_proxy = 0.0

        base_equity = max(float(np.sum(np.abs(pnl_series))), 1.0)
        equity_curve = base_equity + np.cumsum(pnl_series)
        rolling_peak = np.maximum.accumulate(np.concatenate(([base_equity], equity_curve)))
        drawdown = (rolling_peak[1:] - equity_curve) / np.maximum(rolling_peak[1:], 1.0)
        max_drawdown = float(np.max(drawdown)) if len(drawdown) > 0 else 0.0
        win_rate = float(np.sum(pnl_series > 0) / len(pnl_series))

        return {
            "strategy_id": strategy_id,
            "sell_trades": float(len(pnl_series)),
            "sharpe_30d": round(sharpe_proxy, 6),
            "max_drawdown_30d": round(max_drawdown, 6),
            "win_rate_30d": round(win_rate, 6),
            "total_realized_pnl_usdt": round(float(np.sum(pnl_series)), 4),
        }

    def _record_market_making_evolution_feedback(self, strategy_id: str, decision: object) -> None:
        if self._phase3_evolution is None or self._phase3_mm is None:
            return

        diagnostics = {}
        try:
            diagnostics = self._phase3_mm.diagnostics()
        except Exception:  # noqa: BLE001
            diagnostics = {}

        inventory = diagnostics.get("inventory", {}) if isinstance(diagnostics, dict) else {}
        realized_pnl = float(inventory.get("realized_pnl", 0.0) or 0.0)
        previous_realized_pnl = self._phase3_mm_last_realized_pnl.get(strategy_id)
        if previous_realized_pnl is None:
            self._phase3_mm_last_realized_pnl[strategy_id] = realized_pnl
        else:
            pnl_delta = realized_pnl - previous_realized_pnl
            if abs(pnl_delta) > 1e-9:
                self._phase3_mm_realized_trade_records.setdefault(strategy_id, []).append({
                    "timestamp": datetime.now(tz=timezone.utc),
                    "pnl": float(pnl_delta),
                })
                self._phase3_mm_last_realized_pnl[strategy_id] = realized_pnl
                self._sync_evolution_strategy_metrics(strategy_id)
                self._record_evolution_ab_step(strategy_id, float(pnl_delta))

        reason_codes = list(getattr(decision, "reason_codes", []) or [])
        halt_reason = next(
            (
                reason
                for reason in reason_codes
                if reason in {"RISK_BLOCKED", "INVENTORY_HALT", "SNAPSHOT_UNHEALTHY"}
            ),
            None,
        )
        previous_halt_reason = self._phase3_mm_last_halt_reason.get(strategy_id)
        if halt_reason is None:
            self._phase3_mm_last_halt_reason.pop(strategy_id, None)
        elif halt_reason != previous_halt_reason:
            self._phase3_mm_last_halt_reason[strategy_id] = halt_reason
            self._record_evolution_risk_violation(
                strategy_id,
                stage=f"market-making:{halt_reason.lower()}",
            )

    def _apply_evolution_runtime_state(self, report) -> None:
        active_snapshot = list(getattr(report, "active_snapshot", []) or [])
        if not active_snapshot:
            return

        previous_metric_bindings = dict(
            getattr(self, "_phase3_strategy_metric_bindings", {}) or {}
        )
        params_transitioned_strategies: Set[str] = set()
        for decision in list(getattr(report, "decisions", []) or []):
            candidate_id = getattr(decision, "candidate_id", None)
            if not candidate_id:
                continue
            if self._get_candidate_binding_slot(candidate_id=candidate_id) != "params":
                continue
            strategy_id = self._get_evolution_candidate_strategy_id(
                candidate_id=candidate_id,
            )
            if strategy_id:
                params_transitioned_strategies.add(strategy_id)

        ordered_snapshot = sorted(
            active_snapshot,
            key=lambda candidate: 1
            if self._get_candidate_binding_slot(
                candidate=candidate,
                candidate_id=getattr(candidate, "candidate_id", None),
            )
            == "params"
            else 0,
        )
        active_slots_by_strategy: Dict[str, set[str]] = {}

        for candidate in ordered_snapshot:
            strategy_id = self._get_evolution_candidate_strategy_id(candidate=candidate)
            if not strategy_id:
                continue

            active_candidate_id = getattr(candidate, "candidate_id", None)
            if not active_candidate_id:
                continue

            slot = self._get_candidate_binding_slot(
                candidate=candidate,
                candidate_id=active_candidate_id,
            )
            active_slots_by_strategy.setdefault(strategy_id, set()).add(slot)
            current_candidate_id = self._get_evolution_candidate_id(strategy_id, slot=slot)
            if current_candidate_id == active_candidate_id:
                self._bind_evolution_candidate(
                    strategy_id,
                    active_candidate_id,
                    slot=slot,
                    set_metric_owner=False,
                )
                continue

            strategy = self._find_strategy_by_id(strategy_id)
            restored = False
            if slot == "params":
                restored = self._restore_params_candidate_runtime_state(
                    strategy_id,
                    active_candidate_id,
                )
            elif strategy is not None and hasattr(strategy, "model"):
                restored = self._restore_ml_candidate_runtime_state(
                    strategy_id,
                    active_candidate_id,
                )
            elif slot == "strategy" or strategy_id.startswith("phase3_mm_"):
                restored = self._restore_market_making_candidate_runtime_state(
                    strategy_id,
                    active_candidate_id,
                )
            elif slot == "policy" or strategy_id.startswith("phase3_rl_"):
                restored = self._restore_policy_candidate_runtime_state(
                    strategy_id,
                    active_candidate_id,
                )

            if not restored and slot in {"model", "params"} and strategy is not None and hasattr(strategy, "model"):
                continue
            if (
                not restored
                and strategy is None
                and slot not in {"policy", "strategy"}
                and not strategy_id.startswith("phase3_rl_")
                and not strategy_id.startswith("phase3_mm_")
            ):
                continue

            self._bind_evolution_candidate(
                strategy_id,
                active_candidate_id,
                slot=slot,
                set_metric_owner=False,
            )
            log.info(
                "[Phase3/EV] runtime active candidate 已同步: strategy_id={} slot={} candidate_id={} restored={}",
                strategy_id,
                slot,
                active_candidate_id,
                restored,
            )

        for strategy_id, active_slots in active_slots_by_strategy.items():
            if strategy_id in params_transitioned_strategies:
                if "params" in active_slots:
                    restored = self._restore_evolution_slot_runtime_state(
                        strategy_id,
                        "params",
                    )
                    self._set_metric_owner_binding(strategy_id, "params")
                    log.info(
                        "[Phase3/EV] 参数候选生产切换已同步: strategy_id={} slot=params restored={}",
                        strategy_id,
                        restored,
                    )
                    continue

                fallback_slot = self._default_metric_binding_slot(
                    sorted(slot for slot in active_slots if slot != "params")
                )
                if fallback_slot is not None:
                    restored = self._restore_evolution_slot_runtime_state(
                        strategy_id,
                        fallback_slot,
                    )
                    self._set_metric_owner_binding(strategy_id, fallback_slot)
                    log.info(
                        "[Phase3/EV] 参数候选生产回滚已同步: strategy_id={} slot={} restored={}",
                        strategy_id,
                        fallback_slot,
                        restored,
                    )
                    continue

            metric_slot = previous_metric_bindings.get(strategy_id)
            if metric_slot not in active_slots:
                metric_slot = self._default_metric_binding_slot(sorted(active_slots))
            if metric_slot is not None:
                self._set_metric_owner_binding(strategy_id, metric_slot)

    def _sync_evolution_strategy_metrics(self, strategy_id: str) -> None:
        """将按策略聚合的真实成交指标回灌给当前候选。"""
        if self._phase3_evolution is None:
            return

        candidate_id = self._get_evolution_candidate_id(strategy_id, metric_owner=True)
        if candidate_id is None:
            return

        if strategy_id.startswith("phase3_mm_"):
            metrics = self._get_market_making_evolution_metrics(strategy_id)
        else:
            if not hasattr(self, "attributor"):
                return
            try:
                metrics = self.attributor.get_strategy_evolution_metrics(strategy_id)
            except Exception:  # noqa: BLE001
                log.debug("[Phase3/EV] 策略指标提取失败（已忽略）: strategy_id={}", strategy_id)
                return

        if not isinstance(metrics, dict) or not metrics:
            return

        self._phase3_evolution.update_metrics(
            candidate_id,
            sharpe_30d=metrics.get("sharpe_30d"),
            max_drawdown_30d=metrics.get("max_drawdown_30d"),
            win_rate_30d=metrics.get("win_rate_30d"),
        )
        log.debug(
            "[Phase3/EV] 候选指标已回灌: strategy_id={} candidate_id={} sharpe={} maxdd={} win_rate={}",
            strategy_id,
            candidate_id,
            metrics.get("sharpe_30d"),
            metrics.get("max_drawdown_30d"),
            metrics.get("win_rate_30d"),
        )

    def _record_evolution_risk_violation(
        self,
        strategy_id: str,
        *,
        n: int = 1,
        stage: Optional[str] = None,
    ) -> None:
        """将风控拒单同步为候选风险违规计数。"""
        if self._phase3_evolution is None:
            return

        candidate_id = self._get_evolution_candidate_id(strategy_id, metric_owner=True)
        if candidate_id is None:
            return

        self._phase3_evolution.record_risk_violation(candidate_id, n=n)
        log.debug(
            "[Phase3/EV] 风险违规已记录: strategy_id={} candidate_id={} stage={} n={}",
            strategy_id,
            candidate_id,
            stage or "unknown",
            n,
        )

    def _register_evolution_market_making_candidate(
        self,
        strategy,
        *,
        source: str,
    ) -> Optional[str]:
        """注册 Phase 3 做市策略候选。"""
        if self._phase3_evolution is None or strategy is None:
            return None

        from modules.alpha.contracts.evolution_types import CandidateType

        cfg = getattr(strategy, "config", None)
        symbol = getattr(cfg, "symbol", "BTC/USDT")
        symbol_key = _normalize_symbol_key(symbol)
        gamma = float(getattr(getattr(cfg, "avellaneda", None), "gamma", 0.12))
        max_inventory_pct = float(
            getattr(getattr(cfg, "inventory", None), "max_inventory_pct", 0.20)
        )
        version = f"g{gamma:.4f}_inv{max_inventory_pct:.4f}".replace(".", "p")
        strategy_id = f"phase3_mm_{symbol_key}"
        current_candidate_id = self._get_evolution_candidate_id(strategy_id, slot="strategy")
        if current_candidate_id is not None:
            try:
                current = self._phase3_evolution.get_candidate(current_candidate_id)
            except Exception:  # noqa: BLE001
                current = None
            if current is not None and current.version == version:
                self._store_market_making_candidate_runtime_state(
                    strategy_id,
                    current_candidate_id,
                    strategy,
                )
                return current_candidate_id

        metadata = {
            "strategy_id": strategy_id,
            "family_key": f"mm/avellaneda/{symbol_key}",
            "binding_slot": "strategy",
            "source": source,
            "symbol": symbol,
            "exchange": getattr(cfg, "exchange", None),
            "gamma": gamma,
            "max_inventory_pct": max_inventory_pct,
            "paper_mode": getattr(cfg, "paper_mode", None),
        }

        snap = self._phase3_evolution.register_candidate(
            CandidateType.STRATEGY,
            owner="market_making/avellaneda",
            version=version,
            metadata=metadata,
        )
        self._bind_evolution_candidate(
            strategy_id,
            snap.candidate_id,
            slot="strategy",
            set_metric_owner=True,
        )
        self._store_market_making_candidate_runtime_state(strategy_id, snap.candidate_id, strategy)
        log.info(
            "[Phase3/EV] 做市候选已注册: strategy_id={} candidate_id={} version={} source={}",
            strategy_id,
            snap.candidate_id,
            version,
            source,
        )
        self._activate_or_stage_evolution_candidate(
            snap.candidate_id,
            strategy_id=strategy_id,
            family_key=metadata["family_key"],
            source=source,
        )
        return snap.candidate_id

    def _register_evolution_policy_candidate(
        self,
        policy,
        *,
        source: str,
    ) -> Optional[str]:
        """注册 Phase 3 RL policy 候选，并绑定到 paper 订单使用的 strategy_id。"""
        if self._phase3_evolution is None or policy is None:
            return None

        from modules.alpha.contracts.evolution_types import CandidateType

        version = None
        if hasattr(policy, "version"):
            try:
                version = policy.version()
            except Exception:  # noqa: BLE001
                version = None
        if version is None:
            version = getattr(policy, "_version", "ppo_v0")

        strategy_id = f"phase3_rl_{version}"
        current_candidate_id = self._get_evolution_candidate_id(strategy_id, slot="policy")
        if current_candidate_id is not None:
            try:
                current = self._phase3_evolution.get_candidate(current_candidate_id)
            except Exception:  # noqa: BLE001
                current = None
            if current is not None and current.version == version:
                self._store_policy_candidate_runtime_state(
                    strategy_id,
                    current_candidate_id,
                    policy,
                )
                return current_candidate_id

        metadata = {
            "strategy_id": strategy_id,
            "family_key": "rl/ppo",
            "binding_slot": "policy",
            "policy_mode": self._phase3_rl_policy_mode,
            "source": source,
        }
        if hasattr(policy, "config"):
            metadata.update({
                "obs_dim": getattr(policy.config, "obs_dim", None),
                "n_actions": getattr(policy.config, "n_actions", None),
            })

        snap = self._phase3_evolution.register_candidate(
            CandidateType.POLICY,
            owner="rl/ppo",
            version=version,
            metadata=metadata,
        )
        self._bind_evolution_candidate(
            strategy_id,
            snap.candidate_id,
            slot="policy",
            set_metric_owner=True,
        )
        self._store_policy_candidate_runtime_state(strategy_id, snap.candidate_id, policy)
        log.info(
            "[Phase3/EV] RL policy 候选已注册: strategy_id={} candidate_id={} version={} source={}",
            strategy_id,
            snap.candidate_id,
            version,
            source,
        )
        self._activate_or_stage_evolution_candidate(
            snap.candidate_id,
            strategy_id=strategy_id,
            family_key=metadata["family_key"],
            source=source,
        )
        self._sync_evolution_strategy_metrics(strategy_id)
        return snap.candidate_id

    def _activate_or_stage_evolution_candidate(
        self,
        candidate_id: str,
        *,
        strategy_id: str,
        family_key: str,
        source: str,
    ) -> None:
        """初始运行版本直接标记为 active baseline；后续版本尝试启动 A/B。"""
        if self._phase3_evolution is None:
            return

        from modules.alpha.contracts.evolution_types import CandidateStatus

        active_baseline = self._get_active_candidate_for_family(family_key)
        if source in {"initial_load", "phase3_init"}:
            if active_baseline is None:
                self._phase3_evolution.force_promote(
                    candidate_id,
                    CandidateStatus.ACTIVE,
                    reason="INITIAL_RUNTIME_BASELINE",
                )
            return

        if active_baseline is None or active_baseline.candidate_id == candidate_id:
            return

        self._bootstrap_evolution_ab_experiment(
            control_candidate=active_baseline,
            test_candidate_id=candidate_id,
            test_strategy_id=strategy_id,
        )

    def _get_active_candidate_for_family(self, family_key: str):
        """按 family_key 查找当前 active baseline。"""
        if self._phase3_evolution is None:
            return None

        try:
            active_candidates = self._phase3_evolution.list_active()
        except Exception:  # noqa: BLE001
            return None

        if not isinstance(active_candidates, list):
            return None

        for candidate in active_candidates:
            metadata = getattr(candidate, "metadata", {}) or {}
            if metadata.get("family_key") == family_key:
                return candidate
        return None

    def _bootstrap_evolution_ab_experiment(
        self,
        *,
        control_candidate,
        test_candidate_id: str,
        test_strategy_id: str,
    ) -> Optional[str]:
        """用当前 active baseline 的最近真实成交 PnL 预填 control 样本。"""
        if self._phase3_evolution is None:
            return None
        if test_candidate_id in self._phase3_candidate_experiments:
            return self._phase3_candidate_experiments[test_candidate_id]

        ab_cfg = getattr(getattr(self._phase3_evolution, "config", None), "ab_test", None)
        min_samples = max(int(getattr(ab_cfg, "min_samples", 0)), 1)
        control_strategy_id = (getattr(control_candidate, "metadata", {}) or {}).get("strategy_id")
        if not control_strategy_id:
            return None

        try:
            control_pnls = self._get_evolution_realized_trade_pnls(
                control_strategy_id,
                limit=min_samples,
                end_time=datetime.now(tz=timezone.utc),
            )
        except Exception:  # noqa: BLE001
            log.debug("[Phase3/EV] control PnL 历史提取失败（已忽略）: strategy_id={}", control_strategy_id)
            return None

        if len(control_pnls) < min_samples:
            return None

        experiment_id = self._phase3_evolution.create_ab_experiment(
            control_candidate.candidate_id,
            test_candidate_id,
        )
        for pnl in control_pnls:
            self._phase3_evolution.record_ab_step(
                experiment_id,
                is_test=False,
                step_pnl=float(pnl),
            )

        self._phase3_candidate_experiments[test_candidate_id] = experiment_id
        log.info(
            "[Phase3/EV] A/B 实验已启动: experiment_id={} control={} test={} control_samples={} strategy_id={}",
            experiment_id,
            control_candidate.candidate_id,
            test_candidate_id,
            len(control_pnls),
            test_strategy_id,
        )
        return experiment_id

    def _record_evolution_ab_step(self, strategy_id: str, step_pnl: float) -> None:
        """向候选的 A/B 实验写入 test 侧成交 PnL，并在样本满足后自动收尾。"""
        if self._phase3_evolution is None:
            return

        candidate_id = self._get_evolution_candidate_id(strategy_id, metric_owner=True)
        if candidate_id is None:
            return

        experiment_id = self._phase3_candidate_experiments.get(candidate_id)
        if experiment_id is None:
            return

        self._phase3_evolution.record_ab_step(
            experiment_id,
            is_test=True,
            step_pnl=float(step_pnl),
        )

        try:
            status = self._phase3_evolution.ab_experiment_status(experiment_id)
        except Exception:  # noqa: BLE001
            status = None

        if status and status.get("has_sufficient_samples"):
            result = self._phase3_evolution.conclude_ab_experiment(experiment_id)
            if result is not None:
                self._phase3_candidate_experiments.pop(candidate_id, None)
                log.info(
                    "[Phase3/EV] A/B 实验已完结: experiment_id={} candidate_id={} lift={:+.4f}",
                    experiment_id,
                    candidate_id,
                    result.lift,
                )

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
            self._sync_evolution_strategy_metrics(rec.strategy_id)
            if rec.side == "sell":
                self._record_evolution_ab_step(rec.strategy_id, pnl)
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
    from modules.alpha.ml.feature_builder import MLFeatureBuilder
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
        symbol_safe = _normalize_symbol_key(symbol)

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
            runtime_artifacts = _load_ml_runtime_artifacts(model_dir, symbol, model_path)
            log.info(
                "[MLReg] 运行时工件: {} threshold_source={} trainer_source={}",
                symbol,
                runtime_artifacts["threshold_source"],
                runtime_artifacts["params_source"] or getattr(model, "model_type", "default"),
            )

            # 优先使用离线优化产物；无工件时回退到保守默认值。
            config = PredictorConfig(
                buy_threshold=runtime_artifacts["buy_threshold"],
                sell_threshold=runtime_artifacts["sell_threshold"],
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
                evolution_registered = False
                try:
                    feature_builder = MLFeatureBuilder()
                    labeler = ReturnLabeler()
                    trainer_model_type = (
                        runtime_artifacts["trainer_model_type"]
                        or getattr(model, "model_type", "rf")
                    )
                    trainer_model_params = dict(runtime_artifacts["trainer_model_params"])
                    if not trainer_model_params:
                        trainer_model_params = dict(getattr(model, "params", {}) or {})
                    trainer = WalkForwardTrainer(
                        feature_builder=feature_builder,
                        labeler=labeler,
                        model_type=trainer_model_type,
                        model_params=trainer_model_params,
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
                    trader._register_evolution_model_candidate(
                        predictor.strategy_id,
                        learner,
                        model,
                        source="initial_load",
                        model_path=str(model_path),
                    )
                    evolution_registered = True
                except Exception as cl_exc:
                    log.warning("[MLReg] ContinuousLearner 创建失败: {} error={}", symbol, cl_exc)
            else:
                evolution_registered = False

            if not evolution_registered:
                trader._register_evolution_model_candidate(
                    predictor.strategy_id,
                    None,
                    model,
                    source="initial_load",
                    model_path=str(model_path),
                )

            trader._register_evolution_params_candidate(
                predictor.strategy_id,
                trader._continuous_learners.get(predictor.strategy_id),
                runtime_artifacts,
                source="initial_load",
                set_metric_owner=False,
            )
        except Exception as exc:
            log.warning("[MLReg] ML 策略注册失败: {} error={}", symbol, exc)

    if registered > 0:
        log.info("[MLReg] ML 策略注册完成: {} 个品种", registered)
    else:
        log.info("[MLReg] 无可用 ML 模型文件，ML 策略未注册（可通过 train_ml_model.py 训练）")


def _normalize_symbol_key(symbol: str) -> str:
    return "".join(ch for ch in symbol.lower() if ch.isalnum())


def _load_ml_runtime_artifacts(
    model_dir: Path,
    symbol: str,
    model_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """加载 ML runtime 可消费的阈值/参数工件。"""
    from modules.alpha.ml.model_registry import ModelRegistry
    from modules.alpha.ml.threshold_calibrator import CalibrationResult

    runtime_artifacts: Dict[str, Any] = {
        "buy_threshold": 0.60,
        "sell_threshold": 0.40,
        "threshold_source": "default",
        "trainer_model_type": None,
        "trainer_model_params": {},
        "params_source": None,
    }

    symbol_safe = _normalize_symbol_key(symbol)

    for threshold_path in (
        model_dir / f"{symbol_safe}_threshold.json",
        model_dir / "threshold_v1.json",
    ):
        if not threshold_path.exists():
            continue
        try:
            calibration = CalibrationResult.load(threshold_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("[MLReg] 阈值工件加载失败: {} error={}", threshold_path, exc)
            continue

        runtime_artifacts["buy_threshold"] = calibration.recommended_buy_threshold
        runtime_artifacts["sell_threshold"] = calibration.recommended_sell_threshold
        runtime_artifacts["threshold_source"] = threshold_path.name
        break

    params_path = model_dir / f"{symbol_safe}_best_params.json"
    if params_path.exists():
        try:
            with open(params_path, "r", encoding="utf-8") as f:
                best_params_info = json.load(f)
            params = dict(best_params_info.get("params") or {})
            runtime_artifacts["trainer_model_type"] = (
                params.pop("model_type_lgbm", None)
                or params.pop("model_type", None)
            )
            runtime_artifacts["trainer_model_params"] = params
            runtime_artifacts["params_source"] = params_path.name
        except Exception as exc:  # noqa: BLE001
            log.warning("[MLReg] 参数工件加载失败: {} error={}", params_path, exc)

    registry_path = model_dir / "registry.json"
    if not registry_path.exists():
        return runtime_artifacts

    try:
        registry = ModelRegistry(models_dir=model_dir)
        registry_version = None
        for version in [registry.active_version, registry.latest_version()]:
            if (
                model_path is not None
                and version is not None
                and version.model_path == model_path.name
            ):
                registry_version = version
                break
        if registry_version is None:
            for version in reversed(registry.all_versions):
                if model_path is not None and version.model_path == model_path.name:
                    registry_version = version
                    break

        if registry_version is None:
            return runtime_artifacts

        if runtime_artifacts["threshold_source"] == "default":
            runtime_artifacts["buy_threshold"] = registry_version.recommended_buy_threshold
            runtime_artifacts["sell_threshold"] = registry_version.recommended_sell_threshold
            runtime_artifacts["threshold_source"] = f"registry:{registry_version.version_id}"

        if runtime_artifacts["trainer_model_type"] is None:
            runtime_artifacts["trainer_model_type"] = registry_version.model_type
            runtime_artifacts["params_source"] = (
                runtime_artifacts["params_source"]
                or f"registry:{registry_version.version_id}"
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("[MLReg] ModelRegistry 工件加载失败: {} error={}", registry_path, exc)

    return runtime_artifacts



if __name__ == "__main__":
    main()
