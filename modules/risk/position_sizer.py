"""
modules/risk/position_sizer.py — 仓位大小计算器

设计说明：
- 将风险预算转化为具体的开仓数量
- 支持多种仓位计算方法，均经过 crypto 高波动环境验证
- 所有方法必须输出 Decimal，保持精度

支持的方法：
1. 固定名义金额（Fixed Notional）：每次下单固定 USDT 金额，计算数量
2. 固定风险金额（Fixed Risk）：基于止损距离决定数量（每次最多亏 X USDT）
3. 波动率目标（Volatility Target）：目标日波动率 N%，以 ATR 归一化
4. 分数 Kelly（Fractional Kelly）：结合胜率和盈亏比的最优 Kelly 公式，乘以保守系数

重要设计决策：
- 禁止使用全额 Kelly（在加密高波动下极其危险）
- 默认 Kelly 分数上限为 0.25（四分之一 Kelly）
- 所有方法均有最大仓位占净值比例的硬性上限，来自 RiskConfig

接口：
    PositionSizer(max_position_pct, min_qty, price_precision)
    .fixed_notional(notional, price) → Decimal
    .fixed_risk(risk_amount, entry_price, stop_price, price) → Decimal
    .volatility_target(equity, atr_pct, target_vol, price) → Decimal
    .fractional_kelly(win_rate, profit_loss_ratio, equity, price, kelly_fraction) → Decimal
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN

from core.logger import get_logger

log = get_logger(__name__)


class PositionSizer:
    """
    仓位大小计算器。

    Args:
        max_position_pct: 单币种最大仓位占净值比例（0~1），硬性上限
        min_qty:          最小下单数量（交易所限制）
        qty_step:         数量精度步长（如 0.001 BTC）
    """

    def __init__(
        self,
        max_position_pct: float = 0.20,
        min_qty: float = 1e-6,
        qty_step: float = 1e-6,
    ) -> None:
        self._max_position_pct = Decimal(str(max_position_pct))
        self._min_qty = Decimal(str(min_qty))
        self._qty_step = Decimal(str(qty_step))

    # ────────────────────────────────────────────────────────────
    # 方法一：固定名义金额
    # ────────────────────────────────────────────────────────────

    def fixed_notional(
        self,
        notional: float,
        price: float,
        equity: float,
    ) -> Decimal:
        """
        固定名义金额法：将 USDT 金额转换为对应数量。

        下单数量 = min(notional, max_position_pct * equity) / price

        Args:
            notional: 目标下单 USDT 金额
            price:    当前价格
            equity:   当前账户净值

        Returns:
            合法的下单数量
        """
        max_notional = self._max_position_pct * Decimal(str(equity))
        actual_notional = min(Decimal(str(notional)), max_notional)
        qty = actual_notional / Decimal(str(price))
        return self._round_qty(qty)

    # ────────────────────────────────────────────────────────────
    # 方法二：固定风险金额（基于止损）
    # ────────────────────────────────────────────────────────────

    def fixed_risk(
        self,
        risk_amount: float,
        entry_price: float,
        stop_price: float,
        equity: float,
    ) -> Decimal:
        """
        固定风险金额法。

        每笔交易最大亏损 = risk_amount（USDT）
        数量 = risk_amount / |entry_price - stop_price|

        适用于：已知止损位的情况（ATR 止损、支撑位止损等）

        Args:
            risk_amount:  每笔最大可承受亏损（USDT）
            entry_price:  预计入场价格
            stop_price:   止损价格
            equity:       当前账户净值

        Returns:
            合法的下单数量
        """
        risk_per_unit = abs(entry_price - stop_price)
        if risk_per_unit < 1e-10:
            log.warning("fixed_risk: 止损距离接近 0，跳过计算，返回 0")
            return Decimal("0")

        qty = Decimal(str(risk_amount)) / Decimal(str(risk_per_unit))

        # 硬性上限约束
        max_qty = self._max_position_pct * Decimal(str(equity)) / Decimal(str(entry_price))
        qty = min(qty, max_qty)

        return self._round_qty(qty)

    # ────────────────────────────────────────────────────────────
    # 方法三：波动率目标仓位
    # ────────────────────────────────────────────────────────────

    def volatility_target(
        self,
        equity: float,
        atr_pct: float,
        target_vol: float,
        price: float,
    ) -> Decimal:
        """
        波动率目标仓位法。

        目标是使持仓的日波动率 = target_vol * equity。

        数量 = (target_vol * equity) / (atr_pct * equity * price)
             = target_vol / (atr_pct * price)

        适合多币种组合管理，使各仓位贡献等量波动率。

        Args:
            equity:     账户净值
            atr_pct:    当前品种的 ATR/Close 百分比（如 0.02 = 2%）
            target_vol: 目标日波动率（占净值比例，如 0.01 = 1%）
            price:      当前价格

        Returns:
            合法的下单数量
        """
        if atr_pct <= 0:
            log.warning("volatility_target: atr_pct <= 0，返回 0")
            return Decimal("0")

        qty = Decimal(str(target_vol * equity)) / (
            Decimal(str(atr_pct)) * Decimal(str(price))
        )

        max_qty = self._max_position_pct * Decimal(str(equity)) / Decimal(str(price))
        qty = min(qty, max_qty)

        return self._round_qty(qty)

    # ────────────────────────────────────────────────────────────
    # 方法四：分数 Kelly 公式
    # ────────────────────────────────────────────────────────────

    def fractional_kelly(
        self,
        win_rate: float,
        profit_loss_ratio: float,
        equity: float,
        price: float,
        kelly_fraction: float = 0.25,
    ) -> Decimal:
        """
        分数 Kelly 公式。

        Kelly f* = (win_rate * (profit_loss_ratio + 1) - 1) / profit_loss_ratio
        实际使用 f_actual = min(f*, max_position_pct) * kelly_fraction

        警告：
        - 禁止 kelly_fraction > 0.5，加密市场下极具破坏性
        - Kelly 公式对参数误差极为敏感，建议仅在有充分历史验证后使用

        Args:
            win_rate:          胜率（0~1），基于历史回测
            profit_loss_ratio: 平均盈亏比（平均盈利 / 平均亏损）
            equity:            账户净值
            price:             当前价格
            kelly_fraction:    Kelly 折扣系数（默认 0.25，即四分之一 Kelly）

        Returns:
            合法的下单名义金额对应数量
        """
        # 安全约束
        kelly_fraction = min(kelly_fraction, 0.5)

        # Kelly 公式
        b = profit_loss_ratio
        p = win_rate
        kelly_f = (p * (b + 1) - 1) / b if b > 0 else 0.0

        # 负 Kelly 意味着此策略没有正期望值
        if kelly_f <= 0:
            log.warning(
                "fractional_kelly: Kelly f* = {:.4f} <= 0，策略可能无正期望，返回 0",
                kelly_f,
            )
            return Decimal("0")

        # 应用折扣和硬性上限
        effective_f = min(kelly_f * kelly_fraction, float(self._max_position_pct))
        notional = effective_f * equity
        qty = Decimal(str(notional)) / Decimal(str(price))

        log.debug(
            "Kelly: f*={:.4f} 折后={:.4f} 名义={:.2f}",
            kelly_f,
            effective_f,
            notional,
        )

        return self._round_qty(qty)

    # ────────────────────────────────────────────────────────────
    # 工具方法
    # ────────────────────────────────────────────────────────────

    def _round_qty(self, qty: Decimal) -> Decimal:
        """
        将数量向下取整到 qty_step 精度，同时确保 >= min_qty。
        返回 0 如果不足最小数量。
        """
        if qty <= Decimal("0"):
            return Decimal("0")

        # 向下取整到 qty_step
        rounded = (qty // self._qty_step) * self._qty_step

        if rounded < self._min_qty:
            return Decimal("0")

        return rounded
