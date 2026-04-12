"""
modules/monitoring/metrics.py — Prometheus 指标定义与采集

设计说明：
- 使用 prometheus_client 定义所有系统指标
- 指标分为四大类：账户、交易、系统、风控
- 所有指标都带有 exchange/mode/symbol 等标签，便于多维度查询

指标命名规范（遵循 Prometheus 最佳实践）：
- 计数器（Counter）: *_total 后缀
- 仪表盘（Gauge）: 当前状态值
- 直方图（Histogram）: *_seconds 或 *_bytes

接口：
    SystemMetrics(exchange_id, mode)
    .update_equity(equity)
    .update_position(symbol, qty, notional)
    .record_order_submitted(symbol, side, order_type)
    .record_order_filled(symbol, side, qty, notional, fee)
    .record_order_rejected(symbol, reason)
    .record_circuit_breaker(state)
    .record_data_latency(latency_ms)
    .start_http_server(port)
"""

from __future__ import annotations

from typing import Optional

from core.logger import get_logger

log = get_logger(__name__)

# 尝试导入 prometheus_client，若未安装则使用空实现（避免强制依赖）
try:
    from prometheus_client import (
        Counter,
        Gauge,
        Histogram,
        start_http_server as _start_http_server,
        REGISTRY,
    )
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False
    log.warning("prometheus_client 未安装，指标采集将禁用。安装: pip install prometheus-client")


class _NoOpMetric:
    """未安装 prometheus_client 时的空实现，防止代码崩溃。"""
    def labels(self, **kwargs):
        return self
    def inc(self, *args): pass
    def set(self, *args): pass
    def observe(self, *args): pass


def _metric(metric_type, name, desc, labelnames=(), buckets=None):
    """工厂函数：若 prometheus 可用则创建真实指标，否则返回空实现。"""
    if not _PROMETHEUS_AVAILABLE:
        return _NoOpMetric()
    try:
        if metric_type == "counter":
            return Counter(name, desc, labelnames)
        elif metric_type == "gauge":
            return Gauge(name, desc, labelnames)
        elif metric_type == "histogram":
            return Histogram(name, desc, labelnames, buckets=buckets or Histogram.DEFAULT_BUCKETS)
    except Exception:  # noqa: BLE001
        # 同一进程内重复注册时返回空实现
        return _NoOpMetric()


# ══════════════════════════════════════════════════════════════
# 全局指标定义（模块级单例，避免重复注册）
# ══════════════════════════════════════════════════════════════

# 账户净值（USDT）
_equity_gauge = _metric(
    "gauge", "trader_equity_usdt", "当前账户净值（USDT）",
    ["exchange", "mode"],
)

# 持仓市值（USDT）
_position_notional_gauge = _metric(
    "gauge", "trader_position_notional_usdt", "当前持仓市值（USDT）",
    ["exchange", "mode", "symbol"],
)

# 持仓数量（基础币）
_position_qty_gauge = _metric(
    "gauge", "trader_position_qty", "当前持仓数量（基础币）",
    ["exchange", "mode", "symbol"],
)

# 订单提交计数
_orders_submitted_total = _metric(
    "counter", "trader_orders_submitted_total", "累计提交订单数量",
    ["exchange", "mode", "symbol", "side", "order_type"],
)

# 订单成交计数
_orders_filled_total = _metric(
    "counter", "trader_orders_filled_total", "累计成交订单数量",
    ["exchange", "mode", "symbol", "side"],
)

# 成交金额
_filled_notional_total = _metric(
    "counter", "trader_filled_notional_usdt_total", "累计成交名义金额（USDT）",
    ["exchange", "mode", "symbol", "side"],
)

# 手续费
_fee_total = _metric(
    "counter", "trader_fee_usdt_total", "累计手续费（USDT）",
    ["exchange", "mode"],
)

# 当日盈亏
_daily_pnl_gauge = _metric(
    "gauge", "trader_daily_pnl_usdt", "当日盈亏（USDT）",
    ["exchange", "mode"],
)

# 订单被风控拒绝计数
_orders_rejected_total = _metric(
    "counter", "trader_orders_rejected_total", "被风控拒绝的订单数量",
    ["exchange", "mode", "reason_category"],
)

# 熔断状态（0=正常, 1=熔断中）
_circuit_breaker_gauge = _metric(
    "gauge", "trader_circuit_breaker_active", "熔断器状态（1=触发，0=正常）",
    ["exchange", "mode"],
)

# 连续亏损计数
_consecutive_losses_gauge = _metric(
    "gauge", "trader_consecutive_losses", "当前连续亏损次数",
    ["exchange", "mode"],
)

# 数据延迟（毫秒直方图）
_data_latency_ms = _metric(
    "histogram", "trader_data_latency_ms", "数据接收延迟（毫秒）",
    ["exchange", "mode"],
    buckets=[10, 50, 100, 200, 500, 1000, 2000, 5000],
)

# 策略信号生成数
_signals_total = _metric(
    "counter", "trader_signals_total", "策略信号产出数量",
    ["exchange", "mode", "strategy_id", "direction"],
)

# 系统心跳计数
_heartbeat_total = _metric(
    "counter", "trader_heartbeat_total", "系统心跳次数（存活探针）",
    ["exchange", "mode"],
)


class SystemMetrics:
    """
    系统监控指标聚合类。

    封装所有 Prometheus 指标的更新逻辑，
    对外提供语义清晰的业务方法，隐藏 prometheus_client 细节。

    Args:
        exchange_id: 交易所标识
        mode:        运行模式（"live" | "paper" | "backtest"）
    """

    def __init__(self, exchange_id: str = "binance", mode: str = "paper") -> None:
        self.exchange_id = exchange_id
        self.mode = mode
        self._labels = {"exchange": exchange_id, "mode": mode}
        log.info("SystemMetrics 初始化: exchange={} mode={}", exchange_id, mode)

    # ────────────────────────────────────────────────────────────
    # 账户与持仓
    # ────────────────────────────────────────────────────────────

    def update_equity(self, equity: float) -> None:
        """更新账户净值指标。"""
        _equity_gauge.labels(**self._labels).set(equity)

    def update_position(self, symbol: str, qty: float, notional: float) -> None:
        """更新单个币种持仓指标。"""
        labels = {**self._labels, "symbol": symbol}
        _position_qty_gauge.labels(**labels).set(qty)
        _position_notional_gauge.labels(**labels).set(notional)

    def update_daily_pnl(self, pnl: float) -> None:
        """更新当日盈亏指标。"""
        _daily_pnl_gauge.labels(**self._labels).set(pnl)

    # ────────────────────────────────────────────────────────────
    # 订单与成交
    # ────────────────────────────────────────────────────────────

    def record_order_submitted(
        self,
        symbol: str,
        side: str,
        order_type: str,
    ) -> None:
        """记录订单提交事件。"""
        _orders_submitted_total.labels(
            **self._labels, symbol=symbol, side=side, order_type=order_type
        ).inc()

    def record_order_filled(
        self,
        symbol: str,
        side: str,
        qty: float,
        notional: float,
        fee: float,
    ) -> None:
        """记录订单成交事件（含金额和手续费）。"""
        labels_sym = {**self._labels, "symbol": symbol, "side": side}
        _orders_filled_total.labels(**labels_sym).inc()
        _filled_notional_total.labels(**labels_sym).inc(notional)
        _fee_total.labels(**self._labels).inc(fee)

    def record_order_rejected(self, symbol: str, reason: str) -> None:
        """记录订单被风控拒绝事件。"""
        # 对拒绝原因进行分类，避免高基数标签
        category = self._categorize_rejection(reason)
        _orders_rejected_total.labels(**self._labels, reason_category=category).inc()

    # ────────────────────────────────────────────────────────────
    # 风控与系统状态
    # ────────────────────────────────────────────────────────────

    def record_circuit_breaker(self, active: bool) -> None:
        """更新熔断状态（1=触发，0=正常）。"""
        _circuit_breaker_gauge.labels(**self._labels).set(1 if active else 0)

    def update_consecutive_losses(self, count: int) -> None:
        """更新连续亏损计数。"""
        _consecutive_losses_gauge.labels(**self._labels).set(count)

    def record_data_latency(self, latency_ms: float) -> None:
        """记录数据接收延迟（毫秒直方图）。"""
        _data_latency_ms.labels(**self._labels).observe(latency_ms)

    def record_signal(self, strategy_id: str, direction: str) -> None:
        """记录策略信号产出。"""
        _signals_total.labels(
            **self._labels, strategy_id=strategy_id, direction=direction
        ).inc()

    def record_heartbeat(self) -> None:
        """记录系统心跳（存活探针）。"""
        _heartbeat_total.labels(**self._labels).inc()

    # ────────────────────────────────────────────────────────────
    # HTTP 暴露接口
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def start_http_server(port: int = 8000) -> None:
        """
        启动 Prometheus HTTP 指标暴露服务。

        Prometheus Server 通过 HTTP GET /metrics 拉取指标数据。

        Args:
            port: HTTP 服务端口（默认 8000）
        """
        if not _PROMETHEUS_AVAILABLE:
            log.warning("prometheus_client 未安装，无法启动指标服务")
            return
        _start_http_server(port)
        log.info("Prometheus 指标 HTTP 服务已启动: http://0.0.0.0:{}/metrics", port)

    # ────────────────────────────────────────────────────────────
    # 工具方法
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def _categorize_rejection(reason: str) -> str:
        """
        将风控拒绝原因分类为有限的 Prometheus 标签值。

        避免 Prometheus 高基数问题（拒绝原因不能作为原始字符串标签）。
        """
        reason_lower = reason.lower()
        if "仓位" in reason_lower or "position" in reason_lower:
            return "position_limit"
        elif "回撤" in reason_lower or "drawdown" in reason_lower:
            return "drawdown_limit"
        elif "熔断" in reason_lower or "circuit" in reason_lower:
            return "circuit_breaker"
        elif "亏损" in reason_lower or "loss" in reason_lower:
            return "daily_loss"
        elif "黑名单" in reason_lower or "blacklist" in reason_lower:
            return "blacklist"
        else:
            return "other"
