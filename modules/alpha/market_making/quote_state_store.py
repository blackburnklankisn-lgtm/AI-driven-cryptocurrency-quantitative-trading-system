"""
modules/alpha/market_making/quote_state_store.py — 报价状态持久化

设计说明：
- 原子写入当前 quote 状态、库存状态和 PnL 到 JSON 文件
- 模式与 W12/W13 的 cache 保持一致（tmp → os.replace）
- 支持读取恢复（系统重启后恢复库存和 realized PnL）
- 线程安全（threading.Lock）
- 不负责业务逻辑，只负责"原子写入 + 按需读取"

日志标签：[QuoteStateStore]
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger

log = get_logger(__name__)

_DEFAULT_PATH = "storage/quote_state.json"


@dataclass
class QuoteStateSnapshot:
    """
    可持久化的报价状态快照（JSON 序列化）。

    Attributes:
        symbol:          交易对
        base_qty:        当前 base 持仓
        quote_value:     当前 quote 持仓
        realized_pnl:    已实现 PnL
        total_trades:    累计成交笔数
        saved_at:        存储时间（ISO 格式字符串）
    """

    symbol: str
    base_qty: float
    quote_value: float
    realized_pnl: float
    total_trades: int
    saved_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )
    metadata: dict[str, Any] = field(default_factory=dict)


class QuoteStateStore:
    """
    报价状态持久化存储。

    每次 save() 执行原子写入：
        1. 写入 .tmp 文件
        2. os.replace() 替换正式文件
    保证不产生半写状态文件。

    Args:
        path: JSON 存储路径（目录不存在时自动创建）
    """

    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        log.info("[QuoteStateStore] 初始化: path={}", path)

    def save(self, snapshot: QuoteStateSnapshot) -> None:
        """
        原子写入报价状态快照。

        Args:
            snapshot: 要保存的状态快照
        """
        with self._lock:
            tmp_path = self._path + ".tmp"
            data = {
                "symbol": snapshot.symbol,
                "base_qty": snapshot.base_qty,
                "quote_value": snapshot.quote_value,
                "realized_pnl": snapshot.realized_pnl,
                "total_trades": snapshot.total_trades,
                "saved_at": snapshot.saved_at,
                "metadata": snapshot.metadata,
            }
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, self._path)
                log.debug(
                    "[QuoteStateStore] 状态已保存: symbol={} base_qty={:.6f} "
                    "realized_pnl={:.4f}",
                    snapshot.symbol,
                    snapshot.base_qty,
                    snapshot.realized_pnl,
                )
            except Exception:
                log.exception("[QuoteStateStore] 保存失败: path={}", self._path)
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                raise

    def load(self) -> Optional[QuoteStateSnapshot]:
        """
        读取已保存的状态快照。

        Returns:
            QuoteStateSnapshot，或 None（文件不存在或解析失败时）
        """
        with self._lock:
            if not os.path.exists(self._path):
                log.info("[QuoteStateStore] 状态文件不存在，跳过恢复: path={}", self._path)
                return None
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                snap = QuoteStateSnapshot(
                    symbol=data["symbol"],
                    base_qty=float(data["base_qty"]),
                    quote_value=float(data["quote_value"]),
                    realized_pnl=float(data["realized_pnl"]),
                    total_trades=int(data["total_trades"]),
                    saved_at=data.get("saved_at", ""),
                    metadata=data.get("metadata", {}),
                )
                log.info(
                    "[QuoteStateStore] 状态已恢复: symbol={} base_qty={:.6f} "
                    "realized_pnl={:.4f} saved_at={}",
                    snap.symbol, snap.base_qty, snap.realized_pnl, snap.saved_at,
                )
                return snap
            except Exception:
                log.exception("[QuoteStateStore] 恢复失败: path={}", self._path)
                return None

    def clear(self) -> None:
        """删除状态文件（用于 paper 重置或测试清理）。"""
        with self._lock:
            if os.path.exists(self._path):
                os.remove(self._path)
                log.info("[QuoteStateStore] 状态文件已删除: path={}", self._path)

    @property
    def path(self) -> str:
        return self._path
