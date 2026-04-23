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
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional
import threading
import uvicorn
import asyncio
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.config import load_config
from core.event import EventBus, EventType, KlineEvent, OrderRequestEvent
from core.logger import audit_log, get_logger, setup_logging, trade_log
from modules.execution.gateway import CCXTGateway
from modules.execution.order_manager import OrderManager
from modules.monitoring.metrics import SystemMetrics
from modules.risk.manager import RiskConfig, RiskManager
from modules.risk.position_sizer import PositionSizer
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

        # ── 6. 策略列表（外部注入） ───────────────────────────────
        self._strategies = []

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

        log.info(
            "LiveTrader 初始化完成: exchange={} mode={} symbols={}",
            exc_cfg.exchange_id,
            self.mode,
            self.sys_config.data.default_symbols,
        )

    # ────────────────────────────────────────────────────────────
    # 策略注册接口
    # ────────────────────────────────────────────────────────────

    def add_strategy(self, strategy_obj) -> None:
        """
        注册策略对象（需实现 on_kline(event) → List[OrderRequestEvent]）。
        """
        self._strategies.append(strategy_obj)
        log.info("注册实盘策略: {}", getattr(strategy_obj, "strategy_id", type(strategy_obj).__name__))

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

        # Step 4: 周期性 AI 深度分析 (如每小时一次)
        if now.minute == 0 and now.second < 10:
             self._run_ai_analysis()

        # Step 5: 轮询成交回报
        fills = self.order_manager.poll_fills()
        log.debug("[Loop#{0}] 成交回报: {1}笔", seq, len(fills))
        for fill in fills:
            self._on_fill(fill)

        # Step 5b: 撤销超时订单
        cancelled = self.order_manager.cancel_timed_out_orders()
        if cancelled > 0:
            log.warning("超时撤单 {} 笔", cancelled)

        # Step 6: 更新账户快照与指标
        self._update_account_snapshot()
        log.debug(
            "[Loop#{0}] 账户快照: equity={1:.2f} positions={2} entry_prices={3}",
            seq, self._current_equity,
            {s: float(q) for s, q in self._positions.items() if q > 0},
            {s: f"{p:.4f}" for s, p in self._entry_prices.items()},
        )

        # Step 7: 硬止损检查（熔断时强制平仓）
        self._check_stop_loss()

        # Step 8: 每日重置检测（UTC 00:00 后的第一次循环）
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

                # 同时更新最新价格（用最后一根的收盘价）
                _, _, _, _, last_c, _ = candles[-1]
                self._latest_prices[symbol] = float(last_c)
                # Paper 模式：同步行情到网关（用于模拟成交价计算）
                if self.mode == "paper":
                    self.gateway.update_paper_price(symbol, float(last_c))

            except Exception as exc:  # noqa: BLE001
                err_str = str(exc)[:200]
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
        """
        # 发布到事件总线（其他订阅者可扩展）
        self.bus.publish(event)

        # 更新风控状态（仅当 equity 已被正确计算时才更新，避免注入假值）
        if self._current_equity > 0:
            self.risk_manager.update_equity(self._current_equity)

        # 驱动所有策略
        for strategy in self._strategies:
            try:
                order_requests = strategy.on_kline(event)
                for req in (order_requests or []):
                    self._process_order_request(req, self._current_equity)
            except Exception:  # noqa: BLE001
                log.exception("策略异常: strategy={}", getattr(strategy, "strategy_id", "unknown"))

    def _process_order_request(self, req: OrderRequestEvent, equity: float) -> None:
        """
        处理单个订单请求：动态仓位 → 风控审核 → 提交到 OrderManager。
        """
        # 记录信号指标
        self.metrics.record_signal(req.strategy_id, req.side)
        log.debug(
            "[OrderReq] strategy={} symbol={} side={} qty={} price={}",
            req.strategy_id, req.symbol, req.side, req.quantity, req.price,
        )

        # 动态仓位：买单使用波动率目标法替代固定 qty
        quantity = req.quantity
        if req.side == "buy":
            price = self._latest_prices.get(req.symbol, 0)
            if price > 0 and equity > 0:
                klines = self._kline_store.get(req.symbol, [])
                if len(klines) >= 15:
                    import pandas as _pd
                    from modules.alpha.features import FeatureEngine as _FE
                    _df = _pd.DataFrame(klines[-50:])
                    atr_pct_series = _FE.atr_pct(_df, window=14)
                    atr_pct = atr_pct_series.iloc[-1]
                    if _pd.notna(atr_pct) and atr_pct > 0:
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
                                req.symbol, req.quantity, dynamic_qty,
                                equity, price, atr_pct,
                            )
                            quantity = dynamic_qty
                        else:
                            log.debug(
                                "[DynPos] {} volatility_target 返回 0，使用策略建议 qty={}",
                                req.symbol, req.quantity,
                            )
                    else:
                        log.debug(
                            "[DynPos] {} ATR% 无效({})，回退到策略建议 qty={}",
                            req.symbol, atr_pct, req.quantity,
                        )
                else:
                    log.debug(
                        "[DynPos] {} klines 不足({}<15)，回退到策略建议 qty={}",
                        req.symbol, len(klines), req.quantity,
                    )
            else:
                log.debug(
                    "[DynPos] {} price={} equity={:.2f} 无效，回退到策略建议 qty={}",
                    req.symbol, price, equity, req.quantity,
                )

        # 风控审核
        allowed, reason = self.risk_manager.check(
            side=req.side,
            symbol=req.symbol,
            quantity=quantity,
            price=float(req.price or self._latest_prices.get(req.symbol, 0)),
            current_equity=equity,
            positions=dict(self._positions),
        )

        if not allowed:
            log.warning(
                "[RiskBlock] strategy={} {} {} qty={} 被拒绝: {}",
                req.strategy_id, req.symbol, req.side, quantity, reason,
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
            return

        log.info(
            "[OrderSubmit] strategy={} {} {} qty={} 通过风控，提交下单",
            req.strategy_id, req.symbol, req.side, quantity,
        )
        # 通过风控，提交订单
        try:
            self.metrics.record_order_submitted(req.symbol, req.side, req.order_type)
            self.order_manager.submit(
                symbol=req.symbol,
                side=req.side,
                order_type=req.order_type,
                quantity=quantity,
                price=req.price,
                strategy_id=req.strategy_id,
                request_id=req.request_id,
            )
        except Exception as exc:
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
            # 清仓时移除入场价，并清除止损待处理标记
            if self._positions[rec.symbol] <= 0:
                self._entry_prices.pop(rec.symbol, None)
                self._stop_loss_pending.discard(rec.symbol)
                log.info("[Fill] {} 已清仓，移除入场价和止损待处理标记", rec.symbol)

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

    _STOP_LOSS_PCT = 0.05  # 单笔持仓亏损超过 5% 强制平仓

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
                stop_price = entry_price * (1 - self._STOP_LOSS_PCT)
                loss_pct = (entry_price - current_price) / entry_price * 100
                log.debug(
                    "[StopLoss] {} entry={:.4f} stop={:.4f} current={:.4f} loss={:.2f}%",
                    sym, entry_price, stop_price, current_price, loss_pct,
                )
                if current_price <= stop_price:
                    reason = (
                        f"入场价止损: entry={entry_price:.4f} "
                        f"stop={stop_price:.4f} current={current_price:.4f} "
                        f"loss={loss_pct:.2f}%"
                    )
                    should_stop = True
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
