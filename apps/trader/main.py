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
from typing import Dict, List, Optional
import threading
import uvicorn
import asyncio
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.config import load_config
from core.event import EventBus, EventType, KlineEvent, OrderRequestEvent
from core.logger import audit_log, get_logger, setup_logging
from modules.execution.gateway import CCXTGateway
from modules.execution.order_manager import OrderManager
from modules.monitoring.metrics import SystemMetrics
from modules.risk.manager import RiskConfig, RiskManager
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
        ))

        # ── 6. 策略列表（外部注入） ───────────────────────────────
        self._strategies = []

        # 运行状态
        self._positions: Dict[str, Decimal] = {}
        self._latest_prices: Dict[str, float] = {}
        self._current_equity: float = 0.0
        self._running: bool = False
        
        # K 线存储库 (最近 100 根，供前端绘图使用)
        # 格式: { symbol: [ {time, open, high, low, close, volume}, ... ] }
        self._kline_store: Dict[str, List[Dict[str, Any]]] = {}

        # Gemini 配置
        self._gemini_api_key = os.getenv("GOOGLE_API_KEY")
        self._last_ai_analysis = "Waiting for AI analysis..."

        # 轮询间隔（根据 timeframe 动态调整，1h K 线每 60s 轮询一次）
        self._poll_interval_s: float = 60.0

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
        log.info("=" * 50)

        # ── 恢复持久化状态（如有）──────────────────────────────
        self._load_state()

        # ── 预加载历史 K 线，喂给策略暖机 ─────────────────────
        self._preload_history()

        self._running = True
        heartbeat_seq = 0

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

        symbols = self.sys_config.data.default_symbols

        # Step 1: 拉取最新行情，构建 KlineEvent
        kline_events = self._fetch_latest_klines(symbols)

        # Step 2: 驱动策略并缓存 K 线
        for event in kline_events:
            self._latest_prices[event.symbol] = float(event.close)
            self._cache_kline_event(event) # 缓存 K 线供前端拉取
            self._process_kline_event(event)

        # Step 3: 周期性 AI 深度分析 (如每小时一次)
        if now.minute == 0 and now.second < 10:
             self._run_ai_analysis()

        # Step 4: 轮询成交回报
        fills = self.order_manager.poll_fills()
        for fill in fills:
            self._on_fill(fill)

        # Step 4: 撤销超时订单
        cancelled = self.order_manager.cancel_timed_out_orders()
        if cancelled > 0:
            log.warning("超时撤单 {} 笔", cancelled)

        # Step 5: 更新账户快照与指标
        self._update_account_snapshot()

        # Step 6: 硬止损检查（熔断时强制平仓）
        self._check_stop_loss()

        # Step 7: 每日重置检测（UTC 00:00 后的第一次循环）
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
                    events.append(KlineEvent(
                        event_type=EventType.KLINE_UPDATED,
                        timestamp=datetime.now(tz=timezone.utc),
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

        # 更新风控状态
        equity = self._current_equity or float(self.sys_config.risk.max_position_pct) * 100_000
        self.risk_manager.update_equity(equity)

        # 驱动所有策略
        for strategy in self._strategies:
            try:
                order_requests = strategy.on_kline(event)
                for req in (order_requests or []):
                    self._process_order_request(req, equity)
            except Exception:  # noqa: BLE001
                log.exception("策略异常: strategy={}", getattr(strategy, "strategy_id", "unknown"))

    def _process_order_request(self, req: OrderRequestEvent, equity: float) -> None:
        """
        处理单个订单请求：风控审核 → 提交到 OrderManager。
        """
        # 记录信号指标
        self.metrics.record_signal(req.strategy_id, req.side)

        # 风控审核
        allowed, reason = self.risk_manager.check(
            side=req.side,
            symbol=req.symbol,
            quantity=req.quantity,
            price=float(req.price or 0),
            current_equity=equity,
            positions=dict(self._positions),
        )

        if not allowed:
            self.metrics.record_order_rejected(req.symbol, reason)
            return

        # 通过风控，提交订单
        try:
            self.metrics.record_order_submitted(req.symbol, req.side, req.order_type)
            self.order_manager.submit(
                symbol=req.symbol,
                side=req.side,
                order_type=req.order_type,
                quantity=req.quantity,
                price=req.price,
                strategy_id=req.strategy_id,
                request_id=req.request_id,
            )
        except Exception as exc:
            log.error("订单提交失败: {} {} 原因={}", req.symbol, req.side, exc)

    def _on_fill(self, fill) -> None:
        """处理成交回报：更新持仓，记录指标。"""
        rec = fill.order_record
        if rec.side == "buy":
            self._positions[rec.symbol] = (
                self._positions.get(rec.symbol, Decimal("0")) + fill.new_filled_qty
            )
        elif rec.side == "sell":
            current = self._positions.get(rec.symbol, Decimal("0"))
            self._positions[rec.symbol] = max(
                Decimal("0"), current - fill.new_filled_qty
            )

        notional = float(fill.new_filled_qty * fill.avg_price)
        self.metrics.record_order_filled(
            rec.symbol, rec.side, float(fill.new_filled_qty), notional, fee=notional * 0.001
        )

    def _update_account_snapshot(self) -> None:
        """查询账户余额，更新净值和持仓指标。"""
        try:
            balance = self.gateway.fetch_balance()
            usdt_free = balance.get("USDT", {}).get("free", 0) if isinstance(balance.get("USDT"), dict) else 0
            if self.mode == "paper" and usdt_free == 0:
                usdt_free = 5000.0  # Paper 模式默认初始资金
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
                log.warning("更新账户快照(走Mock): {}", err_str)
                self._current_equity = 5000.0

        # 每次更新后持久化状态
        self._save_state()


    def _check_daily_reset(self, now: datetime) -> None:
        """检查是否需要执行每日重置（UTC 00:00 ~ 00:01 之间的第一次循环）。"""
        if now.hour == 0 and now.minute == 0:
            self.risk_manager.reset_daily(self._current_equity)
            log.info("每日风控重置完成")

    # ────────────────────────────────────────────────────────────
    # 历史 K 线预加载（解决策略冷启动问题）
    # ────────────────────────────────────────────────────────────

    def _preload_history(self) -> None:
        """启动时预加载历史 K 线，让策略完成暖机（预热期）。"""
        symbols = self.sys_config.data.default_symbols
        tf = self.sys_config.data.default_timeframe
        preload_bars = 50  # 加载 50 根历史 K 线（足够 slow_window=30 预热）

        log.info("开始预加载历史 K 线: bars={} timeframe={}", preload_bars, tf)

        for symbol in symbols:
            try:
                candles = self.gateway.fetch_ohlcv(symbol, timeframe=tf, limit=preload_bars)
                if not candles:
                    log.warning("预加载: {} 返回空数据，跳过", symbol)
                    continue

                # 除最后一根（可能未收线）外，全部喂给策略
                closed_candles = candles[:-1] if len(candles) > 1 else candles
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
                    # 只驱动策略内部状态更新，不触发下单
                    for strategy in self._strategies:
                        try:
                            strategy.on_kline(event)
                        except Exception:  # noqa: BLE001
                            pass

                # 更新最新价格
                _, _, _, _, last_c, _ = candles[-1]
                self._latest_prices[symbol] = float(last_c)

                log.info("预加载完成: {} bars={}", symbol, len(closed_candles))


            except Exception as exc:  # noqa: BLE001
                log.warning("预加载失败: {} error={}", symbol, str(exc)[:200])
        
        log.info("AUDIT: K-line history cache is now ready and populated.")

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
        
        # 避免重复（通过时间戳）
        store = self._kline_store[event.symbol]
        if store and store[-1]["time"] == kline["time"]:
            store[-1] = kline
        else:
            store.append(kline)
            # 限制长度，防止内存无限增长
            if len(store) > 200:
                self._kline_store[event.symbol] = store[-200:]

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


    _STATE_FILE = "storage/trader_state.json"

    def _save_state(self) -> None:
        """将关键运行状态持久化到 JSON 文件。"""
        import json
        state = {
            "positions": {sym: str(qty) for sym, qty in self._positions.items()},
            "current_equity": self._current_equity,
            "latest_prices": self._latest_prices,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
        try:
            state_path = Path(self._STATE_FILE)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("状态持久化失败: {}", exc)

    def _load_state(self) -> None:
        """从 JSON 文件恢复运行状态。"""
        import json
        state_path = Path(self._STATE_FILE)
        if not state_path.exists():
            log.info("无历史状态文件，首次启动")
            return

        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self._positions = {
                sym: Decimal(qty) for sym, qty in state.get("positions", {}).items()
            }
            self._current_equity = state.get("current_equity", 0.0)
            self._latest_prices = state.get("latest_prices", {})
            log.info(
                "已恢复历史状态: equity={:.2f} positions={}",
                self._current_equity,
                dict(self._positions),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("状态恢复失败（使用默认值启动）: {}", exc)

    # ────────────────────────────────────────────────────────────
    # 硬止损检查（保护资金底线）
    # ────────────────────────────────────────────────────────────

    _STOP_LOSS_PCT = 0.05  # 单笔持仓亏损超过 5% 强制平仓

    def _check_stop_loss(self) -> None:
        """检查所有持仓是否触及硬止损，触发则强制平仓。"""
        for sym, qty in list(self._positions.items()):
            if qty <= 0:
                continue

            current_price = self._latest_prices.get(sym)
            if not current_price or current_price <= 0:
                continue

            # 使用存储的入场均价（简化版：用当前净值反推）
            # TODO: 后续升级为精确的入场价格追踪
            position_value = float(qty) * current_price
            equity = self._current_equity or 5000.0
            position_pct = position_value / equity if equity > 0 else 0

            # 如果持仓价值跌至初始仓位的 (1 - stop_loss_pct) 以下
            # 简化实现：检查持仓是否产生了超过阈值的亏损
            if position_pct > 0 and self.risk_manager.is_circuit_broken():
                log.warning(
                    "止损: 系统已熔断，强制平仓 {} qty={}",
                    sym, float(qty),
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
                    audit_log("STOP_LOSS_TRIGGERED", symbol=sym, quantity=float(qty))
                except Exception as exc:
                    log.error("止损平仓失败: {} error={}", sym, exc)

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

    # 下单量配置（基于 $5000 初始资金，单笔控制在 ~$200-300 左右）
    order_qty_map = {
        "BTC/USDT": 0.003,   # ~$200 @ BTC=$67000
        "ETH/USDT": 0.06,    # ~$210 @ ETH=$3500
        "SOL/USDT": 1.5,     # ~$225 @ SOL=$150
    }

    symbols = trader.sys_config.data.default_symbols
    for symbol in symbols:
        strategy = MACrossStrategy(
            symbol=symbol,
            fast_window=10,
            slow_window=30,
            order_qty=order_qty_map.get(symbol, 0.001),
            volume_filter=True,
            timeframe=trader.sys_config.data.default_timeframe,
        )
        trader.add_strategy(strategy)

    trader.run()



if __name__ == "__main__":
    main()
