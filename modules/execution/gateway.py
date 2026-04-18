"""
modules/execution/gateway.py — CCXT 交易所执行网关

设计说明：
- 统一封装 CCXT 的下单、撤单、查询订单接口
- 所有交易所适配差异在此层消化，上层模块只调用标准接口
- 支持 live（实盘）和 paper（模拟）两种模式
  - paper 模式: API 调用前添加 [PAPER] 标记，不实际发单
  - live 模式: 真实调用交易所 API

严格安全规则：
- API Key 只在初始化时从环境变量读取，不做任何形式的日志输出
- 下单前必须通过 RiskManager.check()（调用者职责）
- 所有网络异常必须被捕获并分类（不允许裸异常透传到上层）

接口：
    CCXTGateway(exchange_id, mode, api_key, secret)
    .submit_order(symbol, side, order_type, qty, price) → str (order_id)
    .cancel_order(order_id, symbol)                     → bool
    .fetch_order(order_id, symbol)                      → dict
    .fetch_open_orders(symbol)                          → list[dict]
    .fetch_balance()                                    → dict
    .fetch_ticker(symbol)                               → dict
    .close()                                            → None

失败模式：
- 网络超时/断线: 抛出 ExchangeConnectionError（可重试）
- 余额不足/交易对下架: 抛出 OrderSubmissionError（不重试）
- 订单超时: 抛出 OrderTimeoutError（调用方决定撤单还是追单）
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import ccxt

from core.exceptions import (
    ExchangeConnectionError,
    OrderSubmissionError,
    OrderTimeoutError,
)
from core.logger import audit_log, get_logger

log = get_logger(__name__)


class CCXTGateway:
    """
    CCXT 交易所执行网关。

    支持 live 和 paper 两种运行模式；
    paper 模式下所有发单操作仅记录日志，不实际调用交易所。

    Args:
        exchange_id:      CCXT 交易所 ID（如 "binance"、"okx"）
        mode:             "live" | "paper"（默认 "paper"）
        api_key:          API Key（从环境变量获取，不直接传入代码）
        secret:           API Secret
        passphrase:       部分交易所需要的 Passphrase（如 OKX）
        timeout_ms:       单次请求超时（毫秒）
        max_retries:      网络错误最大重试次数
    """

    def __init__(
        self,
        exchange_id: str = "binance",
        mode: str = "paper",
        api_key: str = "",
        secret: str = "",
        passphrase: str = "",
        timeout_ms: int = 10_000,
        max_retries: int = 3,
    ) -> None:
        if mode not in {"live", "paper"}:
            raise ValueError(f"mode 必须是 'live' 或 'paper'，实际: {mode}")

        self.mode = mode
        self.exchange_id = exchange_id
        self.max_retries = max_retries

        # 构建 CCXT 实例
        exchange_class = getattr(ccxt, exchange_id, None)
        if exchange_class is None:
            raise OrderSubmissionError(f"未知交易所 ID: {exchange_id}")

        config: Dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": timeout_ms,
        }
        if api_key:
            config["apiKey"] = api_key
        if secret:
            config["secret"] = secret
        if passphrase:
            config["password"] = passphrase

        self._exchange: ccxt.Exchange = exchange_class(config)

        log.info(
            "CCXTGateway 初始化: exchange={} mode={}",
            exchange_id,
            mode,
        )
        # 注意：绝不在日志中输出 API Key

    # ────────────────────────────────────────────────────────────
    # 核心交易接口
    # ────────────────────────────────────────────────────────────

    def submit_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> str:
        """
        提交订单。

        Paper 模式：生成虚拟订单 ID，记录审计日志，不实际发单。
        Live 模式：调用 CCXT create_order()，返回交易所订单 ID。

        Args:
            symbol:          交易对，如 "BTC/USDT"
            side:            "buy" | "sell"
            order_type:      "limit" | "market"
            quantity:        数量
            price:           限价单价格（市价单传 None）
            client_order_id: 可选，客户端自定义订单 ID（部分交易所支持）

        Returns:
            订单 ID（paper 模式为本地 UUID 格式，live 模式为交易所 ID）

        Raises:
            OrderSubmissionError:     订单被交易所拒绝（余额不足、交易对不合规等）
            ExchangeConnectionError:  网络异常
        """
        log.info(
            "[{}] 提交订单: {} {} {} qty={} price={}",
            self.mode.upper(),
            symbol,
            side,
            order_type,
            quantity,
            price,
        )

        if self.mode == "paper":
            return self._paper_submit(symbol, side, order_type, quantity, price)

        # Live 模式：真实下单
        return self._live_submit(symbol, side, order_type, quantity, price, client_order_id)

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        """
        撤销订单。

        Returns:
            True = 撤单成功，False = 订单已成交/不存在（无需再撤）
        """
        log.info("[{}] 撤单: order_id={} symbol={}", self.mode.upper(), order_id, symbol)

        if self.mode == "paper":
            audit_log("ORDER_CANCELLED", order_id=order_id, symbol=symbol, mode="paper")
            return True

        try:
            self._exchange.cancel_order(order_id, symbol)
            audit_log("ORDER_CANCELLED", order_id=order_id, symbol=symbol, mode="live")
            return True
        except ccxt.OrderNotFound:
            log.warning("撤单目标不存在（可能已成交）: order_id={}", order_id)
            return False
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            raise ExchangeConnectionError(f"撤单网络错误: {exc}") from exc
        except ccxt.ExchangeError as exc:
            raise OrderSubmissionError(f"撤单失败: {exc}") from exc

    def fetch_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        """
        查询单个订单状态。

        Returns:
            标准化的订单字典，包含 status/filled/remaining/average_price 等字段。
        """
        if self.mode == "paper":
            return {"id": order_id, "status": "closed", "filled": 0, "remaining": 0}

        try:
            return self._exchange.fetch_order(order_id, symbol)
        except ccxt.OrderNotFound:
            return {"id": order_id, "status": "not_found"}
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            raise ExchangeConnectionError(f"查询订单网络错误: {exc}") from exc

    def fetch_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取所有未成交挂单。"""
        if self.mode == "paper":
            return []

        try:
            return self._exchange.fetch_open_orders(symbol) or []
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            raise ExchangeConnectionError(f"获取挂单网络错误: {exc}") from exc

    def fetch_balance(self) -> Dict[str, Any]:
        """
        获取账户余额。

        Returns:
            CCXT 标准格式的余额字典，包含 free/used/total 三类余额。
        """
        if self.mode == "paper":
            return {"USDT": {"free": 0, "used": 0, "total": 0}}

        try:
            return self._exchange.fetch_balance()
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            raise ExchangeConnectionError(f"获取余额网络错误: {exc}") from exc
        except ccxt.AuthenticationError as exc:
            raise ExchangeConnectionError(f"API 认证失败，请检查 API Key: {exc}") from exc

    def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        """
        获取单个交易对的最新行情（last/bid/ask/volume）。
        """
        try:
            return self._exchange.fetch_ticker(symbol)
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            raise ExchangeConnectionError(f"获取行情网络错误: {exc}") from exc

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 50,
    ) -> List[List]:
        """
        获取历史 OHLCV K 线数据。

        Returns:
            CCXT 标准格式的 K 线列表: [[timestamp, open, high, low, close, volume], ...]
        """
        try:
            return self._exchange.fetch_ohlcv(symbol, timeframe, limit=limit) or []
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            raise ExchangeConnectionError(f"获取K线网络错误: {exc}") from exc
        except ccxt.ExchangeError as exc:
            raise ExchangeConnectionError(f"获取K线交易所错误: {exc}") from exc

    def close(self) -> None:
        """释放连接和资源（asyncio 场景下需调用）。"""
        if hasattr(self._exchange, "close"):
            try:
                self._exchange.close()
            except Exception:  # noqa: BLE001
                pass
        log.info("CCXTGateway 已关闭: exchange={}", self.exchange_id)

    # ────────────────────────────────────────────────────────────
    # 私有实现
    # ────────────────────────────────────────────────────────────

    def _paper_submit(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float],
    ) -> str:
        """Paper 模式：生成虚拟订单 ID，写入审计日志。"""
        import uuid
        order_id = f"paper_{uuid.uuid4().hex[:12]}"

        audit_log(
            "ORDER_SUBMITTED",
            mode="paper",
            order_id=order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
        )
        log.info("[PAPER] 虚拟订单已记录: order_id={}", order_id)
        return order_id

    def _live_submit(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float],
        client_order_id: Optional[str],
    ) -> str:
        """Live 模式：调用 CCXT API 实际发单，含重试机制。"""
        params: Dict[str, Any] = {}
        if client_order_id:
            params["clientOrderId"] = client_order_id

        last_exc: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                if order_type == "market":
                    resp = self._exchange.create_market_order(symbol, side, quantity, params=params)
                else:
                    if price is None:
                        raise OrderSubmissionError("限价单必须提供 price")
                    resp = self._exchange.create_limit_order(symbol, side, quantity, price, params=params)

                order_id = resp.get("id", "unknown")
                audit_log(
                    "ORDER_SUBMITTED",
                    mode="live",
                    order_id=order_id,
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    quantity=quantity,
                    price=price,
                )
                log.info("[LIVE] 订单已提交: order_id={}", order_id)
                return order_id

            except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
                last_exc = exc
                wait = 2 ** attempt
                log.warning("发单网络错误，第 {}/{} 次重试，等待 {}s", attempt, self.max_retries, wait)
                time.sleep(wait)

            except ccxt.InsufficientFunds as exc:
                raise OrderSubmissionError(f"余额不足: {exc}") from exc

            except ccxt.InvalidOrder as exc:
                raise OrderSubmissionError(f"无效订单参数: {exc}") from exc

            except ccxt.ExchangeError as exc:
                raise OrderSubmissionError(f"交易所错误: {exc}") from exc

        raise ExchangeConnectionError(
            f"下单失败（已重试 {self.max_retries} 次）"
        ) from last_exc
