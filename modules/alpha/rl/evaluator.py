"""
modules/alpha/rl/evaluator.py — OOS/paper/shadow 评估器

设计说明：
- 在给定 replay 轨迹上评估 PPO policy 的表现
- 输出 EvalResult（Sharpe, max_drawdown, win_rate, turnover, risk_violations）
- 评估时使用 deterministic=True（argmax），不随机采样
- 不修改 policy 权重，纯只读评估
- 支持门禁阈值判断（passes_gate）

日志标签：[RLEval]
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.alpha.contracts.rl_types import (
    ActionType,
    EvalResult,
    RolloutStep,
)
from modules.alpha.rl.ppo_agent import PPOAgent

log = get_logger(__name__)


@dataclass
class EvalGateConfig:
    """
    评估门禁阈值。

    Attributes:
        min_sharpe:         最小 Sharpe 比率
        max_drawdown:       最大允许回撤
        max_risk_violations:风险违规次数上限（0 = 零容忍）
        min_win_rate:       最小胜率
        min_steps:          最小评估步数（样本不足则 passes_gate=False）
    """

    min_sharpe: float = 0.8
    max_drawdown: float = 0.07
    max_risk_violations: int = 0
    min_win_rate: float = 0.45
    min_steps: int = 50


class Evaluator:
    """
    RL Policy 评估器。

    无状态，每次 evaluate() 返回新的 EvalResult。
    """

    def __init__(self, gate: Optional[EvalGateConfig] = None) -> None:
        self.gate = gate or EvalGateConfig()
        log.info(
            "[RLEval] Evaluator 初始化: min_sharpe={} max_dd={} min_steps={}",
            self.gate.min_sharpe, self.gate.max_drawdown, self.gate.min_steps,
        )

    def evaluate(
        self,
        agent: PPOAgent,
        steps: list[RolloutStep],
        eval_mode: str = "oos",
        policy_id: str = "rl_policy",
    ) -> EvalResult:
        """
        在给定步骤序列上评估 policy 性能。

        Args:
            agent:      被评估的 PPO Agent
            steps:      评估用轨迹（按时间顺序的 RolloutStep 列表）
            eval_mode:  "oos" / "paper" / "shadow"
            policy_id:  policy 标识

        Returns:
            EvalResult
        """
        start = datetime.now(tz=timezone.utc)
        n = len(steps)

        if n < self.gate.min_steps:
            log.warning(
                "[RLEval] 评估步数不足: n={} min_steps={}",
                n, self.gate.min_steps,
            )
            return self._make_failed(
                policy_id, agent.version(), eval_mode, n,
                reason=f"INSUFFICIENT_STEPS={n}",
            )

        # ── 运行 deterministic 推理 & 收集指标
        rewards: list[float] = []
        returns: list[float] = []
        episode_return = 0.0
        peak_return = 0.0
        max_dd = 0.0
        wins = 0
        risk_violations = 0
        total_trades = 0
        turnover_sum = 0.0
        ep_count = 0

        cumulative = 0.0

        for step in steps:
            rewards.append(step.reward)
            cumulative += step.reward
            episode_return += step.reward

            # 胜/负
            if step.reward > 0:
                wins += 1

            # 风险违规（kill switch 或大惩罚）
            if step.reward <= -5.0:
                risk_violations += 1

            # 换手率（用 action_type != HOLD 的步骤比率估算）
            if step.action_type != ActionType.HOLD:
                total_trades += 1
                turnover_sum += 1.0

            # 最大回撤（以累计收益为基准）
            if cumulative > peak_return:
                peak_return = cumulative
            dd = (peak_return - cumulative) / max(abs(peak_return), 1e-8)
            max_dd = max(max_dd, dd)

            if step.done:
                returns.append(episode_return)
                episode_return = 0.0
                ep_count += 1

        if episode_return != 0.0:
            returns.append(episode_return)
            ep_count += 1

        n_episodes = max(ep_count, 1)
        total_return = cumulative

        # Sharpe（年化，假设每步 1 秒，anno factor = sqrt(365*24*3600)）
        if len(rewards) > 1:
            r_arr = rewards
            mean_r = sum(r_arr) / len(r_arr)
            var_r = sum((x - mean_r) ** 2 for x in r_arr) / (len(r_arr) - 1)
            std_r = math.sqrt(max(var_r, 1e-10))
            sharpe = (mean_r / std_r) * math.sqrt(252)  # 日化 Sharpe
        else:
            sharpe = 0.0

        win_rate = wins / max(n, 1)
        avg_turnover = turnover_sum / max(n, 1)

        # 门禁判断
        reasons: list[str] = []
        if sharpe < self.gate.min_sharpe:
            reasons.append(f"LOW_SHARPE={sharpe:.3f}<{self.gate.min_sharpe}")
        if max_dd > self.gate.max_drawdown:
            reasons.append(f"HIGH_DD={max_dd:.3f}>{self.gate.max_drawdown}")
        if risk_violations > self.gate.max_risk_violations:
            reasons.append(f"RISK_VIOLATIONS={risk_violations}>{self.gate.max_risk_violations}")
        if win_rate < self.gate.min_win_rate:
            reasons.append(f"LOW_WIN_RATE={win_rate:.3f}<{self.gate.min_win_rate}")

        passes = len(reasons) == 0
        if not reasons:
            reasons.append("ALL_GATES_PASSED")

        end = datetime.now(tz=timezone.utc)

        result = EvalResult(
            policy_id=policy_id,
            policy_version=agent.version(),
            eval_mode=eval_mode,  # type: ignore[arg-type]
            total_return=total_return,
            sharpe=sharpe,
            max_drawdown=max_dd,
            win_rate=win_rate,
            avg_turnover=avg_turnover,
            risk_violations=risk_violations,
            n_episodes=n_episodes,
            n_steps=n,
            eval_start=start,
            eval_end=end,
            passes_gate=passes,
            reason_codes=reasons,
            debug_payload={
                "total_trades": total_trades,
                "rewards_mean": sum(rewards) / max(len(rewards), 1),
                "rewards_std": math.sqrt(
                    sum((x - sum(rewards)/len(rewards))**2 for x in rewards) / max(len(rewards)-1, 1)
                ) if len(rewards) > 1 else 0.0,
            },
        )

        log.info(
            "[RLEval] 评估完成: policy={} mode={} sharpe={:.3f} max_dd={:.3f} "
            "win_rate={:.3f} passes={} steps={}",
            agent.version(), eval_mode, sharpe, max_dd,
            win_rate, passes, n,
        )
        return result

    def _make_failed(
        self,
        policy_id: str,
        version: str,
        eval_mode: str,
        n_steps: int,
        reason: str,
    ) -> EvalResult:
        now = datetime.now(tz=timezone.utc)
        return EvalResult(
            policy_id=policy_id,
            policy_version=version,
            eval_mode=eval_mode,  # type: ignore[arg-type]
            total_return=0.0,
            sharpe=0.0,
            max_drawdown=0.0,
            win_rate=0.0,
            avg_turnover=0.0,
            risk_violations=0,
            n_episodes=0,
            n_steps=n_steps,
            eval_start=now,
            eval_end=now,
            passes_gate=False,
            reason_codes=[reason],
        )
