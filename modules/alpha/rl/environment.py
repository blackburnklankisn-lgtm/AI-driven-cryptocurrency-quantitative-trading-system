"""
modules/alpha/rl/environment.py — RL 交易环境

设计说明：
- Gymnasium-compatible 接口（reset / step）
- 只消费 replay/backtest/paper 数据，不接触 live 执行网关
- 状态机：未开始 → RUNNING → DONE（达到 max_steps 或账户爆仓）
- 每步驱动：
    1. ObservationBuilder 构建 RLObservation
    2. RewardEngine 计算即时奖励
    3. 更新模拟账户状态（position, equity）
- 支持 replay 数据注入（list[dict] 格式，每个 dict 包含一步的市场状态）

约束：
- 不直接调用 CCXTGateway 或任何 live 接口
- 不依赖 asyncio（eventlet 兼容）

日志标签：[RLEnv]
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from core.logger import get_logger
from modules.alpha.contracts.rl_types import ActionType, RLObservation, RolloutStep
from modules.alpha.rl.observation_builder import ObservationBuilder, ObservationBuilderConfig
from modules.alpha.rl.reward_engine import RewardConfig, RewardEngine
from modules.risk.snapshot import RiskSnapshot

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class TradingEnvConfig:
    """
    TradingEnvironment 配置。

    Attributes:
        symbol:              交易对
        exchange:            交易所名称
        max_steps:           每个 episode 最大步数
        initial_equity:      初始资金（USDT）
        fee_rate:            单边手续费率
        target_inventory_pct: 做市目标库存比例
        position_step:       每步动作的仓位调整幅度（占权益比）
        max_position_pct:    最大仓位比例（止损参考）
        bankruptcy_equity:   权益低于此值时强制结束 episode
    """

    symbol: str = "BTC/USDT"
    exchange: str = "backtest"
    max_steps: int = 1000
    initial_equity: float = 10000.0
    fee_rate: float = 0.001
    target_inventory_pct: float = 0.5
    position_step: float = 0.1
    max_position_pct: float = 0.9
    bankruptcy_equity: float = 100.0


# ══════════════════════════════════════════════════════════════
# 二、模拟账户状态
# ══════════════════════════════════════════════════════════════

@dataclass
class SimAccount:
    """
    简化的模拟账户状态。

    Attributes:
        equity:           当前权益（USDT）
        position_pct:     方向性仓位比例 ∈ [-1, 1]（正=多，负=空）
        realized_pnl:     累计已实现 PnL
        unrealized_pnl:   当前未实现 PnL（随 mid 变化）
        fee_paid:         累计手续费
        n_trades:         交易次数
        entry_price:      开仓均价（0 = 空仓）
        peak_equity:      历史最高权益（用于回撤计算）
    """

    equity: float
    position_pct: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    fee_paid: float = 0.0
    n_trades: int = 0
    entry_price: float = 0.0
    peak_equity: float = 0.0

    @property
    def drawdown(self) -> float:
        peak = max(self.peak_equity, self.equity)
        return max(0.0, (peak - self.equity) / max(peak, 1e-8))


# ══════════════════════════════════════════════════════════════
# 三、TradingEnvironment 主体
# ══════════════════════════════════════════════════════════════

class TradingEnvironment:
    """
    RL 交易环境（Gymnasium-style）。

    replay_data 格式（每步一个 dict）：
        {
            "mid_price": float,
            "spread_bps": float,
            "technical": dict,     # 可选
            "onchain": dict,       # 可选
            "sentiment": dict,     # 可选
            "microstructure": dict, # 可选
        }
    """

    def __init__(
        self,
        config: Optional[TradingEnvConfig] = None,
        obs_config: Optional[ObservationBuilderConfig] = None,
        reward_config: Optional[RewardConfig] = None,
    ) -> None:
        self.config = config or TradingEnvConfig()
        self._obs_builder = ObservationBuilder(obs_config)
        self._reward_engine = RewardEngine(reward_config)

        # 运行时状态（reset() 后初始化）
        self._account: SimAccount = SimAccount(equity=self.config.initial_equity)
        self._step: int = 0
        self._done: bool = True
        self._replay_data: list[dict[str, Any]] = []
        self._episode_id: str = ""
        self._last_obs: Optional[RLObservation] = None

        log.info(
            "[RLEnv] TradingEnvironment 初始化: symbol={} max_steps={} initial_equity={}",
            self.config.symbol, self.config.max_steps, self.config.initial_equity,
        )

    def reset(
        self,
        replay_data: Optional[list[dict[str, Any]]] = None,
        risk_snapshot: Optional[RiskSnapshot] = None,
    ) -> RLObservation:
        """
        重置环境，开始新 episode。

        Args:
            replay_data:    步骤数据列表（None = 沿用上次设置的数据）
            risk_snapshot:  初始风险状态（None = 安全默认值）

        Returns:
            初始观测
        """
        if replay_data is not None:
            self._replay_data = replay_data

        self._account = SimAccount(
            equity=self.config.initial_equity,
            peak_equity=self.config.initial_equity,
        )
        self._step = 0
        self._done = False
        self._episode_id = f"ep-{uuid.uuid4().hex[:8]}"

        risk = risk_snapshot or self._safe_risk()
        obs = self._build_obs(risk, step=0)
        self._last_obs = obs

        log.debug(
            "[RLEnv] Episode 开始: episode_id={} replay_len={} equity={}",
            self._episode_id, len(self._replay_data), self.config.initial_equity,
        )
        return obs

    def step(
        self,
        action_index: int,
        action_type: ActionType,
        risk_snapshot: Optional[RiskSnapshot] = None,
    ) -> tuple[RLObservation, float, bool, dict[str, Any]]:
        """
        执行一步动作。

        Args:
            action_index: 离散动作索引
            action_type:  ActionType（由 ActionAdapter 映射）
            risk_snapshot: 当前风险状态

        Returns:
            (next_obs, reward, done, info)
        """
        if self._done:
            raise RuntimeError("[RLEnv] step() called on finished episode; call reset() first")

        risk = risk_snapshot or self._safe_risk()
        prev_unrealized = self._account.unrealized_pnl
        step_data = self._get_step_data(self._step)

        mid = float(step_data.get("mid_price", 50000.0))
        spread_bps = float(step_data.get("spread_bps", 2.0))

        # 执行动作（更新模拟账户）
        realized_pnl, fee = self._execute_action(action_type, mid)
        self._update_unrealized(mid)

        # 更新权益
        self._account.equity += realized_pnl - fee
        if self._account.equity > self._account.peak_equity:
            self._account.peak_equity = self._account.equity

        # 计算 turnover
        turnover = abs(fee / self.config.fee_rate) / max(self._account.equity, 1.0) if fee > 0 else 0.0

        # 计算奖励
        breakdown = self._reward_engine.compute(
            realized_pnl=realized_pnl,
            prev_unrealized_pnl=prev_unrealized,
            curr_unrealized_pnl=self._account.unrealized_pnl,
            fee_paid=fee,
            current_drawdown=self._account.drawdown,
            turnover=turnover,
            kill_switch_active=risk.kill_switch_active,
            inventory_deviation=abs(self._account.position_pct),
            risk_violated=risk.circuit_broken or risk.kill_switch_active,
            portfolio_value=self._account.equity,
        )

        reward = breakdown.total

        # 判断 episode 结束
        self._step += 1
        done = (
            self._step >= self.config.max_steps
            or self._step >= len(self._replay_data)
            or self._account.equity < self.config.bankruptcy_equity
        )
        self._done = done

        # 构建下一步观测
        next_obs = self._build_obs(risk, step=self._step)
        self._last_obs = next_obs

        info = {
            "episode_id": self._episode_id,
            "step": self._step,
            "equity": self._account.equity,
            "realized_pnl": realized_pnl,
            "fee": fee,
            "mid_price": mid,
            "drawdown": self._account.drawdown,
            "position_pct": self._account.position_pct,
            "reward_breakdown": breakdown.to_dict(),
        }

        return next_obs, reward, done, info

    def make_rollout_step(
        self,
        obs: RLObservation,
        action_index: int,
        action_type: ActionType,
        reward: float,
        next_obs: Optional[RLObservation],
        done: bool,
        value_est: float = 0.0,
        log_prob: float = 0.0,
    ) -> RolloutStep:
        """工厂方法：将一步数据打包为 RolloutStep。"""
        return RolloutStep(
            obs=list(obs.feature_vector),
            action_index=action_index,
            action_type=action_type,
            reward=reward,
            next_obs=list(next_obs.feature_vector) if next_obs else None,
            done=done,
            value_est=value_est,
            log_prob=log_prob,
            info={
                "episode_id": self._episode_id,
                "step": self._step,
            },
        )

    def is_done(self) -> bool:
        return self._done

    def current_equity(self) -> float:
        return self._account.equity

    def obs_dim(self) -> int:
        return 24  # ObservationBuilder.OBS_DIM

    def n_actions(self) -> int:
        return 8   # DEFAULT_ACTION_SPACE 大小

    # ──────────────────────────────────────────────────────────
    # 内部辅助
    # ──────────────────────────────────────────────────────────

    def _get_step_data(self, step: int) -> dict[str, Any]:
        if not self._replay_data or step >= len(self._replay_data):
            return {"mid_price": 50000.0, "spread_bps": 2.0}
        return self._replay_data[step]

    def _build_obs(self, risk: RiskSnapshot, step: int) -> RLObservation:
        data = self._get_step_data(step)
        return self._obs_builder.build(
            symbol=self.config.symbol,
            trace_id=f"{self._episode_id}-s{step}",
            risk_snapshot=risk,
            technical=data.get("technical"),
            onchain=data.get("onchain"),
            sentiment=data.get("sentiment"),
            microstructure=data.get("microstructure"),
            inventory_pct=max(0.0, min(1.0, 0.5 + self._account.position_pct * 0.5)),
            position_pct=self._account.position_pct,
            episode_step=step,
        )

    def _execute_action(
        self, action_type: ActionType, mid: float
    ) -> tuple[float, float]:
        """
        执行动作，更新 position_pct，返回 (realized_pnl, fee)。

        简化规则：
        - BUY:    position_pct += position_step（最大 1.0）
        - SELL:   position_pct -= position_step（最小 -1.0）
        - REDUCE: position_pct *= 0.5（减半）
        - HOLD / MM bias: 不变
        """
        realized = 0.0
        fee = 0.0
        step = self.config.position_step
        acc = self._account

        if action_type == ActionType.BUY:
            delta = min(step, self.config.max_position_pct - acc.position_pct)
            if delta > 0:
                cost = delta * acc.equity * mid / mid  # 简化：delta * equity
                fee = cost * self.config.fee_rate
                acc.position_pct += delta
                acc.entry_price = mid
                acc.n_trades += 1
                acc.fee_paid += fee

        elif action_type == ActionType.SELL:
            delta = min(step, acc.position_pct + self.config.max_position_pct)
            if delta > 0:
                if acc.position_pct > 0 and acc.entry_price > 0:
                    # 平多
                    trade_size = min(delta, acc.position_pct)
                    realized = trade_size * acc.equity * (mid - acc.entry_price) / max(acc.entry_price, 1e-8)
                cost = delta * acc.equity
                fee = cost * self.config.fee_rate
                acc.position_pct -= delta
                acc.n_trades += 1
                acc.fee_paid += fee
                acc.realized_pnl += realized

        elif action_type == ActionType.REDUCE:
            if acc.position_pct != 0:
                half = acc.position_pct * 0.5
                if acc.position_pct > 0 and acc.entry_price > 0:
                    realized = abs(half) * acc.equity * (mid - acc.entry_price) / max(acc.entry_price, 1e-8)
                fee = abs(half) * acc.equity * self.config.fee_rate
                acc.position_pct -= half
                acc.realized_pnl += realized
                acc.fee_paid += fee
                acc.n_trades += 1

        # MM bias 动作：不改变 position，但记录（为未来 spread 偏置预留接口）
        # HOLD, WIDEN_QUOTE, NARROW_QUOTE, BIAS_BID, BIAS_ASK: no-op

        return realized, fee

    def _update_unrealized(self, mid: float) -> None:
        """更新未实现 PnL（简化 mark-to-market）。"""
        acc = self._account
        if acc.position_pct != 0 and acc.entry_price > 0:
            position_value = acc.position_pct * acc.equity
            price_return = (mid - acc.entry_price) / max(acc.entry_price, 1e-8)
            acc.unrealized_pnl = position_value * price_return
        else:
            acc.unrealized_pnl = 0.0

    @staticmethod
    def _safe_risk() -> RiskSnapshot:
        """生成安全的默认 RiskSnapshot（无风险约束）。"""
        return RiskSnapshot(
            current_drawdown=0.0,
            daily_loss_pct=0.0,
            consecutive_losses=0,
            circuit_broken=False,
            kill_switch_active=False,
            budget_remaining_pct=1.0,
        )
