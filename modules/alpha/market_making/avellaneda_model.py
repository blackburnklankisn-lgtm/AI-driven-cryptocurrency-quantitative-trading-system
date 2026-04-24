"""
modules/alpha/market_making/avellaneda_model.py — Avellaneda-Stoikov 做市模型

设计说明：
- 实现 Avellaneda-Stoikov (2008) 做市模型的核心公式
- 只负责数学计算，不直接操作订单簿或下单
- 输入：mid_price, sigma, gamma, T, t, inventory_qty, inventory_max_qty, kappa
- 输出：reservation_price（调整后的 mid）和 optimal_spread（最优总点差）
- 所有公式均有详细注释，便于审计和调参

核心公式：
    reservation_price  = mid - q * gamma * sigma^2 * (T - t)
    optimal_spread     = gamma * sigma^2 * (T - t) + (2/gamma) * ln(1 + gamma/kappa)

其中：
    q     = 当前库存（正 = 做多，负 = 做空），归一化到 [-1, 1]
    gamma = 风险厌恶系数（越大 spread 越宽，skew 越强）
    sigma = 价格波动率（单位：价格/√秒）
    T     = 总时间窗口（秒，通常为一天 86400）
    t     = 已过去时间（秒）
    kappa = 订单到达率参数（控制 spread 底限，越小 spread 越宽）

日志标签：[Avellaneda]
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.alpha.contracts.mm_types import QuoteIntent

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class AvellanedaConfig:
    """
    Avellaneda-Stoikov 模型参数配置。

    Attributes:
        gamma:              风险厌恶系数 (0.01 ~ 0.5)
                            越大 → spread 越宽，inventory skew 越强
        kappa:              订单到达率（0.5 ~ 5.0）
                            越小 → optimal spread 越宽（流动性越差时应调小）
        T_sec:              总时间窗口（秒），默认一天 86400
        min_spread_bps:     最小 spread 下限（基点），防止 spread 过于收窄
        max_spread_bps:     最大 spread 上限（基点），防止报价过于宽松失去竞争力
        max_inventory_skew_bps: 库存 skew 最大值（基点），防止极端偏斜
        sigma_floor:        波动率 sigma 下限（防止极低波动率导致 spread 为 0）
        sigma_cap:          波动率 sigma 上限（防止极高波动率时 spread 失控）
        allow_one_sided:    是否允许库存过重时单边报价（默认 True）
        config_version:     配置版本（用于诊断日志）
    """

    gamma: float = 0.12
    kappa: float = 1.5
    T_sec: float = 86400.0       # 一天
    min_spread_bps: float = 1.0
    max_spread_bps: float = 100.0
    max_inventory_skew_bps: float = 30.0
    sigma_floor: float = 0.0001  # 防止 sigma 为 0
    sigma_cap: float = 0.10      # 约 10% 日波动率
    allow_one_sided: bool = True
    config_version: str = "v1.0"

    def __post_init__(self):
        if not (0 < self.gamma <= 1.0):
            raise ValueError(f"gamma 应在 (0, 1]，当前: {self.gamma}")
        if not (self.kappa > 0):
            raise ValueError(f"kappa 必须 > 0，当前: {self.kappa}")
        if not (self.min_spread_bps < self.max_spread_bps):
            raise ValueError("min_spread_bps 必须 < max_spread_bps")


# ══════════════════════════════════════════════════════════════
# 二、AvellanedaModel 主体
# ══════════════════════════════════════════════════════════════

class AvellanedaModel:
    """
    Avellaneda-Stoikov 做市模型计算引擎。

    无状态：每次 compute() 调用都是独立计算，不持有任何运行时状态。
    纯数学计算，不直接操作订单簿、不下单。

    Args:
        config: AvellanedaConfig

    接口：
        compute(mid_price, sigma, inventory_qty, inventory_max_qty,
                elapsed_sec=0, session_start=None) → QuoteIntent
        reservation_price(...)                                → float
        optimal_spread_bps(sigma, elapsed_sec)               → float
        inventory_skew_bps(inventory_qty, inventory_max_qty) → float
    """

    def __init__(self, config: Optional[AvellanedaConfig] = None) -> None:
        self.config = config or AvellanedaConfig()
        log.info(
            "[Avellaneda] 模型初始化: gamma={} kappa={} T_sec={} "
            "min_spread={}bps max_spread={}bps version={}",
            self.config.gamma,
            self.config.kappa,
            self.config.T_sec,
            self.config.min_spread_bps,
            self.config.max_spread_bps,
            self.config.config_version,
        )

    # ──────────────────────────────────────────────────────────
    # 主计算接口
    # ──────────────────────────────────────────────────────────

    def compute(
        self,
        symbol: str,
        mid_price: float,
        sigma: float,
        inventory_qty: float,
        inventory_max_qty: float,
        elapsed_sec: float = 0.0,
    ) -> QuoteIntent:
        """
        计算当前时刻的做市报价意图。

        Args:
            symbol:            交易对
            mid_price:         当前订单簿 mid price
            sigma:             当前价格波动率（日波动率，如 0.02 = 2%）
            inventory_qty:     当前 base 持仓量（正 = 持有 base，负 = 做空）
            inventory_max_qty: 最大允许 base 持仓量（绝对值）
            elapsed_sec:       当前会话已过去的秒数

        Returns:
            QuoteIntent（包含 reservation_price、optimal_spread_bps、skew 等）
        """
        if mid_price <= 0:
            raise ValueError(f"mid_price 必须 > 0，当前: {mid_price}")
        if inventory_max_qty <= 0:
            raise ValueError(f"inventory_max_qty 必须 > 0，当前: {inventory_max_qty}")

        # sigma 裁剪
        sigma_clamped = max(self.config.sigma_floor, min(self.config.sigma_cap, sigma))

        # 时间因子 (T - t) / T，约束 > 0
        time_ratio = max(0.001, (self.config.T_sec - elapsed_sec) / self.config.T_sec)

        # 归一化库存 q ∈ [-1, 1]
        q = inventory_qty / inventory_max_qty
        q = max(-1.0, min(1.0, q))

        # reservation price
        r_price = self._reservation_price(mid_price, q, sigma_clamped, time_ratio)

        # optimal spread（基点）
        opt_spread_bps = self._optimal_spread_bps(sigma_clamped, time_ratio)

        # inventory skew（基点）
        skew_bps = self._inventory_skew_bps(q)

        # 允许报价侧判断
        allow_bid = True
        allow_ask = True
        reason_codes: list[str] = []

        if self.config.allow_one_sided:
            if q >= 1.0:
                # 最大多头持仓：禁止再买
                allow_bid = False
                reason_codes.append("MAX_LONG_INVENTORY")
                log.debug(
                    "[Avellaneda] 禁用 BID（最大多头）: symbol={} q={:.4f}",
                    symbol, q,
                )
            elif q <= -1.0:
                # 最大空头持仓：禁止再卖
                allow_ask = False
                reason_codes.append("MAX_SHORT_INVENTORY")
                log.debug(
                    "[Avellaneda] 禁用 ASK（最大空头）: symbol={} q={:.4f}",
                    symbol, q,
                )

        log.debug(
            "[Avellaneda] compute: symbol={} mid={:.4f} r_price={:.4f} "
            "spread={}bps skew={:.2f}bps q={:.4f} sigma={:.6f} time_ratio={:.4f} "
            "allow_bid={} allow_ask={}",
            symbol, mid_price, r_price, opt_spread_bps, skew_bps,
            q, sigma_clamped, time_ratio, allow_bid, allow_ask,
        )

        return QuoteIntent(
            symbol=symbol,
            mid_price=mid_price,
            reservation_price=r_price,
            optimal_spread_bps=opt_spread_bps,
            sigma=sigma_clamped,
            gamma=self.config.gamma,
            inventory_deviation=q,
            allow_bid=allow_bid,
            allow_ask=allow_ask,
            reason_codes=reason_codes,
            debug_payload={
                "q": q,
                "time_ratio": time_ratio,
                "sigma_clamped": sigma_clamped,
                "skew_bps": skew_bps,
                "elapsed_sec": elapsed_sec,
                "kappa": self.config.kappa,
            },
        )

    # ──────────────────────────────────────────────────────────
    # 公开子公式（供测试和调试独立调用）
    # ──────────────────────────────────────────────────────────

    def reservation_price(
        self,
        mid_price: float,
        q: float,
        sigma: float,
        time_ratio: float = 1.0,
    ) -> float:
        """
        计算 reservation price（含库存 skew 的调整 mid）。

        公式：r = mid - q * γ * σ² * (T-t)
        其中 (T-t) 用 time_ratio * T_sec 表示
        """
        return self._reservation_price(mid_price, q, sigma, time_ratio)

    def optimal_spread_bps(self, sigma: float, time_ratio: float = 1.0) -> float:
        """
        计算最优总点差（基点）。

        公式：δ = γ * σ² * (T-t) + (2/γ) * ln(1 + γ/κ)
        """
        return self._optimal_spread_bps(sigma, time_ratio)

    def inventory_skew_bps(self, q: float) -> float:
        """
        计算库存 skew 调整量（基点）。

        正 q（偏多）→ 正 skew → reservation_price 下移 → bid 更低，ask 更低
        负 q（偏空）→ 负 skew → reservation_price 上移 → bid 更高，ask 更高
        """
        return self._inventory_skew_bps(q)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "gamma": self.config.gamma,
            "kappa": self.config.kappa,
            "T_sec": self.config.T_sec,
            "min_spread_bps": self.config.min_spread_bps,
            "max_spread_bps": self.config.max_spread_bps,
            "config_version": self.config.config_version,
        }

    # ──────────────────────────────────────────────────────────
    # 内部计算（私有）
    # ──────────────────────────────────────────────────────────

    def _reservation_price(
        self,
        mid_price: float,
        q: float,
        sigma: float,
        time_ratio: float,
    ) -> float:
        """
        r = mid - q × γ × σ² × (T × time_ratio)
        """
        T_remaining = self.config.T_sec * time_ratio
        skew_abs = q * self.config.gamma * sigma ** 2 * T_remaining
        r = mid_price - skew_abs
        return r

    def _optimal_spread_bps(self, sigma: float, time_ratio: float) -> float:
        """
        δ = γ × σ² × (T × time_ratio) + (2/γ) × ln(1 + γ/κ)

        转换为基点：× 10000
        """
        T_remaining = self.config.T_sec * time_ratio

        # 两项之和
        term1 = self.config.gamma * sigma ** 2 * T_remaining
        # 防止 ln 参数 <= 0
        ln_arg = 1.0 + self.config.gamma / self.config.kappa
        term2 = (2.0 / self.config.gamma) * math.log(ln_arg)

        spread_raw = term1 + term2

        # 转换为基点（假设 sigma 是日波动率，T_remaining 是秒，需转为价格比例）
        # spread_raw 的单位是价格，转换为 bps 需除以 mid（此处用 1.0 近似，调用方用 mid 换算）
        # 实际上这里 spread_raw 已经是无量纲的比率了，×10000 转为 bps
        spread_bps = spread_raw * 10000.0

        # clip
        spread_bps = max(self.config.min_spread_bps, min(self.config.max_spread_bps, spread_bps))
        return spread_bps

    def _inventory_skew_bps(self, q: float) -> float:
        """
        skew_bps = q × max_inventory_skew_bps
        q ∈ [-1, 1]，skew ∈ [-max, max]
        """
        skew = q * self.config.max_inventory_skew_bps
        return max(-self.config.max_inventory_skew_bps, min(self.config.max_inventory_skew_bps, skew))
