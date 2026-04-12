"""
modules/alpha/ml/predictor.py — ML 信号推理器（实时推理适配器）

设计说明：
- 封装已训练的 SignalModel，提供与 BaseAlpha 策略接口一致的在线推理
- 维护滑动特征缓冲区，每来一根 K 线追加并重新计算特征
- 输出概率阈值过滤（避免低置信度信号进入执行层）

防未来函数保证：
- 每次推理只使用当前及历史 K 线数据
- 绝不使用 event.timestamp 之后的任何数据
- 如果缓冲区数据不足（預热期），返回空列表

信号过滤机制：
- 只有当"买入概率 > buy_threshold"时才产出买入信号
- 只有当"买入概率 < sell_threshold"时（且持仓）才产出卖出信号
- 连续信号抑制（cooling_bars）：信号触发后，N 根 K 线内不再产出相同方向

接口：
    MLPredictor(model, feature_builder, config)
    .on_kline(event) → List[OrderRequestEvent]  （遵循 BaseAlpha 接口约定）
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Deque, List, Optional

import pandas as pd

from core.event import KlineEvent, OrderRequestEvent
from core.logger import get_logger
from modules.alpha.base import BaseAlpha
from modules.alpha.ml.feature_builder import FeatureConfig, MLFeatureBuilder
from modules.alpha.ml.model import SignalModel

log = get_logger(__name__)


@dataclass
class PredictorConfig:
    """ML 推理器配置。"""
    buy_threshold: float = 0.60      # 买入概率阈值（高置信度才买）
    sell_threshold: float = 0.40     # 卖出概率阈值（低置信度时 + 持仓则卖出）
    order_qty: float = 0.01          # 每次下单数量（基础币）
    cooling_bars: int = 5            # 信号冷却周期（避免频繁换仓）
    min_buffer_size: int = 300       # 最小特征缓冲区大小（预热期保护）


class MLPredictor(BaseAlpha):
    """
    ML 信号推理器。

    将训练好的 SignalModel 包装成遵循 BaseAlpha 接口的实盘推理组件。
    与规则策略（如 MACrossStrategy）完全互换，上层引擎无需感知差异。

    Args:
        model:           已训练的 SignalModel 实例
        symbol:          目标交易对
        config:          推理配置（阈值、数量等）
        feature_builder: 特征构建器（必须与训练时完全一致）
        timeframe:       K 线周期
    """

    def __init__(
        self,
        model: SignalModel,
        symbol: str,
        config: Optional[PredictorConfig] = None,
        feature_builder: Optional[MLFeatureBuilder] = None,
        timeframe: str = "1h",
    ) -> None:
        super().__init__(
            strategy_id=f"ml_predictor_{symbol.replace('/', '_')}_{model.model_type}",
            symbol=symbol,
            timeframe=timeframe,
        )
        self.model = model
        self.cfg = config or PredictorConfig()
        self.feature_builder = feature_builder or MLFeatureBuilder()

        # 滑动 OHLCV 缓冲区（用于在线特征计算）
        max_buf = max(self.cfg.min_buffer_size, 400)
        self._ohlcv_buffer: Deque[dict] = deque(maxlen=max_buf)

        # 策略状态
        self._in_position: bool = False
        self._cooling_counter: int = 0   # 信号冷却计数
        self._last_signal: Optional[str] = None

        log.info(
            "{} 初始化: buy_thresh={} sell_thresh={} qty={} cooling={}",
            self.strategy_id,
            self.cfg.buy_threshold,
            self.cfg.sell_threshold,
            self.cfg.order_qty,
            self.cfg.cooling_bars,
        )

    def on_kline(self, event: KlineEvent) -> List[OrderRequestEvent]:
        """
        处理新 K 线事件，返回 ML 信号产出的订单请求。

        流程：
        1. 追加 OHLCV 到缓冲区
        2. 检查预热期（缓冲区不足时返回空）
        3. 构建特征矩阵（只用缓冲区历史数据）
        4. 调用已训练模型推理
        5. 根据概率阈值和冷却期过滤信号
        6. 产出 OrderRequestEvent
        """
        if not event.is_closed or event.symbol != self.symbol:
            return []

        self._increment_bar(event)

        # 追加到 OHLCV 缓冲区
        self._ohlcv_buffer.append({
            "timestamp": event.timestamp,
            "open":   float(event.open),
            "high":   float(event.high),
            "low":    float(event.low),
            "close":  float(event.close),
            "volume": float(event.volume),
        })

        # 预热期保护
        if len(self._ohlcv_buffer) < self.cfg.min_buffer_size:
            log.debug(
                "{} 预热中 ({}/{})",
                self.strategy_id,
                len(self._ohlcv_buffer),
                self.cfg.min_buffer_size,
            )
            return []

        # 冷却期检查
        if self._cooling_counter > 0:
            self._cooling_counter -= 1
            return []

        # ── 特征计算 + 推理 ──────────────────────────────────────
        try:
            buy_proba = self._infer_buy_probability()
        except Exception as exc:
            log.warning("{} 推理失败（跳过）: {}", self.strategy_id, exc)
            return []

        if buy_proba is None:
            return []

        orders: List[OrderRequestEvent] = []
        qty = Decimal(str(self.cfg.order_qty))

        # 买入信号
        if not self._in_position and buy_proba >= self.cfg.buy_threshold:
            log.info(
                "{} ML 买入信号: proba={:.3f} threshold={}",
                self.strategy_id, buy_proba, self.cfg.buy_threshold,
            )
            orders.append(self._make_market_order(event, "buy", qty))
            self._in_position = True
            self._cooling_counter = self.cfg.cooling_bars
            self._last_signal = "buy"

        # 卖出信号（平多）
        elif self._in_position and buy_proba <= self.cfg.sell_threshold:
            log.info(
                "{} ML 卖出信号: proba={:.3f} threshold={}",
                self.strategy_id, buy_proba, self.cfg.sell_threshold,
            )
            orders.append(self._make_market_order(event, "sell", qty))
            self._in_position = False
            self._cooling_counter = self.cfg.cooling_bars
            self._last_signal = "sell"

        return orders

    # ────────────────────────────────────────────────────────────
    # 私有推理方法
    # ────────────────────────────────────────────────────────────

    def _infer_buy_probability(self) -> Optional[float]:
        """
        基于当前缓冲区数据计算买入概率。

        Returns:
            买入概率 [0, 1] 或 None（数据不足/推理失败时）
        """
        # 将缓冲区转为 DataFrame
        buf_df = pd.DataFrame(list(self._ohlcv_buffer))

        # 构建特征矩阵
        feat_df = self.feature_builder.build(buf_df)
        feature_names = self.feature_builder.get_feature_names()

        if not feature_names or len(feat_df) == 0:
            return None

        # 取最后一行作为当前推理点（已收线 K 线，无未来信息）
        last_row = feat_df[feature_names].iloc[[-1]]

        if last_row.isnull().any().any():
            # 特征含 NaN，说明仍在预热期（某些窗口指标还不完整）
            return None

        # 推理
        proba_array = self.model.predict_signal_proba(last_row)
        return float(proba_array[-1])


class MLStrategy(BaseAlpha):
    """
    通用 ML 策略适配器（比 MLPredictor 更简洁的包装）。

    提供创建不同模型类型的类方法，便于从配置文件快速实例化。

    用法：
        strategy = MLStrategy.from_model_path(
            path="models/btcusdt_rf.pkl",
            symbol="BTC/USDT",
        )
    """

    def __init__(
        self,
        predictor: MLPredictor,
    ) -> None:
        super().__init__(
            strategy_id=predictor.strategy_id,
            symbol=predictor.symbol,
            timeframe=predictor.timeframe,
        )
        self._predictor = predictor

    def on_kline(self, event: KlineEvent) -> List[OrderRequestEvent]:
        return self._predictor.on_kline(event)

    @classmethod
    def from_model_path(
        cls,
        path: str,
        symbol: str,
        config: Optional[PredictorConfig] = None,
        timeframe: str = "1h",
    ) -> "MLStrategy":
        """
        从 pickle 文件加载模型并创建 MLStrategy 实例。
        """
        model = SignalModel.load(path)
        predictor = MLPredictor(
            model=model,
            symbol=symbol,
            config=config,
            timeframe=timeframe,
        )
        return cls(predictor=predictor)
