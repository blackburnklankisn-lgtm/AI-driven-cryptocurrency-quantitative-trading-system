"""
core/exceptions.py — 系统自定义异常体系

设计原则：
- 所有业务异常均继承自 CryptoQuantError，便于全局捕获
- 分层设计，外层模块只抛出自己层级的异常
- 禁止吞噬异常后静默继续，所有异常必须记录
"""

from __future__ import annotations


class CryptoQuantError(Exception):
    """系统顶层异常基类，所有业务异常继承自此。"""


# ─── 数据层 ────────────────────────────────────────────────
class DataLayerError(CryptoQuantError):
    """数据层基础异常。"""


class DataFetchError(DataLayerError):
    """从交易所或数据源拉取数据失败。"""


class DataValidationError(DataLayerError):
    """原始数据校验不通过（坏点、时间戳异常、缺失列等）。"""


class DataAlignmentError(DataLayerError):
    """多数据源时间轴对齐失败。"""


# ─── Alpha 引擎层 ───────────────────────────────────────────
class AlphaLayerError(CryptoQuantError):
    """Alpha 引擎层基础异常。"""


class FeatureEngineeringError(AlphaLayerError):
    """特征计算过程中出现错误（含未来函数检测失败）。"""


class ModelInferenceError(AlphaLayerError):
    """AI/ML 模型推理失败。"""


class FutureLookAheadError(AlphaLayerError):
    """检测到未来函数或数据泄露，强制中断。"""


# ─── 风控层 ────────────────────────────────────────────────
class RiskLayerError(CryptoQuantError):
    """风控层基础异常。"""


class RiskLimitBreached(RiskLayerError):
    """触发了硬风控约束，交易请求被拒绝。"""

    def __init__(self, rule: str, detail: str = "") -> None:
        self.rule = rule
        super().__init__(f"[风控拦截] 规则={rule}  详情={detail}")


class CircuitBreakerTriggered(RiskLayerError):
    """触发熔断器，系统进入保护模式。"""


# ─── 执行层 ────────────────────────────────────────────────
class ExecutionLayerError(CryptoQuantError):
    """执行层基础异常。"""


class OrderSubmissionError(ExecutionLayerError):
    """订单提交到交易所失败。"""


class OrderTimeoutError(ExecutionLayerError):
    """订单等待超时，需要撤单或追单。"""


class ExchangeConnectionError(ExecutionLayerError):
    """交易所连接异常（网络断线/API 限频）。"""


# ─── 配置层 ────────────────────────────────────────────────
class ConfigError(CryptoQuantError):
    """配置加载或验证失败。"""
