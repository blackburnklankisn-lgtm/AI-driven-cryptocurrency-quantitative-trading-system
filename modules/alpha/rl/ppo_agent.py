"""
modules/alpha/rl/ppo_agent.py — 轻量级 PPO 策略代理 (v1)

设计说明：
- 不依赖 PyTorch/JAX，使用 numpy 实现线性 policy + value network
- 适合 v1 "受约束策略代理"角色：特征 → softmax 离散动作
- 接口设计与 PyTorch PPO 兼容（predict/update），方便日后切换
- Policy Network: 双线性层 + softmax（obs_dim → hidden → n_actions）
- Value Network:  双线性层（obs_dim → hidden → 1）
- 支持序列化（save/load 权重为 JSON 格式）
- 训练使用 clipped surrogate objective（PPO-clip, epsilon=0.2）

安全约束：
- predict() 总是返回 (action_index, action_value, confidence, log_prob)
- 训练只在 paper/replay 数据上进行，不接触 live 账户

日志标签：[RLPolicy]
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

from core.logger import get_logger

log = get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# 一、配置
# ══════════════════════════════════════════════════════════════

@dataclass
class PPOConfig:
    """
    PPO Agent 配置。

    Attributes:
        obs_dim:          观测维度（必须与 ObservationBuilder.OBS_DIM 匹配）
        n_actions:        离散动作数量
        hidden_dim:       隐藏层维度
        lr_policy:        policy 网络学习率
        lr_value:         value 网络学习率
        clip_epsilon:     PPO 裁剪系数
        entropy_coeff:    熵正则化系数（防止早熟收敛）
        value_coeff:      value loss 系数
        max_grad_norm:    梯度范数裁剪
        n_epochs:         每次 update 的 mini-epoch 数
        batch_size:       mini-batch 大小
        gamma:            折扣因子
        lambda_:          GAE lambda
        seed:             随机种子
    """

    obs_dim: int = 24
    n_actions: int = 8
    hidden_dim: int = 64
    lr_policy: float = 3e-4
    lr_value: float = 1e-3
    clip_epsilon: float = 0.2
    entropy_coeff: float = 0.01
    value_coeff: float = 0.5
    max_grad_norm: float = 0.5
    n_epochs: int = 4
    batch_size: int = 64
    gamma: float = 0.99
    lambda_: float = 0.95
    seed: int = 42


# ══════════════════════════════════════════════════════════════
# 二、轻量线性网络（numpy）
# ══════════════════════════════════════════════════════════════

class _LinearNet:
    """
    两层全连接网络（numpy 实现）。
    [in_dim] → ReLU → [hidden_dim] → [out_dim]
    """

    def __init__(self, in_dim: int, hidden_dim: int, out_dim: int, rng: np.random.Generator) -> None:
        scale1 = math.sqrt(2.0 / in_dim)
        scale2 = math.sqrt(2.0 / hidden_dim)
        self.W1 = rng.normal(0.0, scale1, (in_dim, hidden_dim)).astype(np.float64)
        self.b1 = np.zeros(hidden_dim, dtype=np.float64)
        self.W2 = rng.normal(0.0, scale2, (hidden_dim, out_dim)).astype(np.float64)
        self.b2 = np.zeros(out_dim, dtype=np.float64)
        # Adam 状态
        self._m_W1 = np.zeros_like(self.W1)
        self._v_W1 = np.zeros_like(self.W1)
        self._m_b1 = np.zeros_like(self.b1)
        self._v_b1 = np.zeros_like(self.b1)
        self._m_W2 = np.zeros_like(self.W2)
        self._v_W2 = np.zeros_like(self.W2)
        self._m_b2 = np.zeros_like(self.b2)
        self._v_b2 = np.zeros_like(self.b2)
        self._t = 0

    def forward(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """
        Returns (output, hidden) for gradient reuse.
        x shape: (batch, in_dim) or (in_dim,)
        """
        if x.ndim == 1:
            x = x.reshape(1, -1)
        h = np.maximum(0.0, x @ self.W1 + self.b1)  # ReLU
        out = h @ self.W2 + self.b2
        return out, h

    def adam_update(
        self,
        grad_W1: np.ndarray,
        grad_b1: np.ndarray,
        grad_W2: np.ndarray,
        grad_b2: np.ndarray,
        lr: float,
        beta1: float = 0.9,
        beta2: float = 0.999,
        eps: float = 1e-8,
    ) -> None:
        """Adam optimizer update."""
        self._t += 1
        t = self._t
        for param, grad, m, v in [
            (self.W1, grad_W1, self._m_W1, self._v_W1),
            (self.b1, grad_b1, self._m_b1, self._v_b1),
            (self.W2, grad_W2, self._m_W2, self._v_W2),
            (self.b2, grad_b2, self._m_b2, self._v_b2),
        ]:
            m[:] = beta1 * m + (1 - beta1) * grad
            v[:] = beta2 * v + (1 - beta2) * (grad ** 2)
            m_hat = m / (1 - beta1 ** t)
            v_hat = v / (1 - beta2 ** t)
            param -= lr * m_hat / (np.sqrt(v_hat) + eps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "W1": self.W1.tolist(),
            "b1": self.b1.tolist(),
            "W2": self.W2.tolist(),
            "b2": self.b2.tolist(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], in_dim: int, hidden_dim: int, out_dim: int) -> "_LinearNet":
        rng = np.random.default_rng(0)
        net = cls(in_dim, hidden_dim, out_dim, rng)
        net.W1 = np.array(d["W1"], dtype=np.float64)
        net.b1 = np.array(d["b1"], dtype=np.float64)
        net.W2 = np.array(d["W2"], dtype=np.float64)
        net.b2 = np.array(d["b2"], dtype=np.float64)
        return net


def _softmax(x: np.ndarray) -> np.ndarray:
    x = x - x.max(axis=-1, keepdims=True)
    e = np.exp(x)
    return e / e.sum(axis=-1, keepdims=True)


# ══════════════════════════════════════════════════════════════
# 三、PPOAgent 主体
# ══════════════════════════════════════════════════════════════

class PPOAgent:
    """
    轻量级 PPO 策略代理（numpy 实现，v1）。

    接口与 PyTorch PPO 兼容，可无缝切换。
    训练只使用 replay/backtest/paper 数据。
    """

    def __init__(self, config: Optional[PPOConfig] = None) -> None:
        self.config = config or PPOConfig()
        rng = np.random.default_rng(self.config.seed)
        self._py_rng = random.Random(self.config.seed)

        # 策略网络（actor）和价值网络（critic）
        self._policy_net = _LinearNet(
            self.config.obs_dim, self.config.hidden_dim, self.config.n_actions, rng
        )
        self._value_net = _LinearNet(
            self.config.obs_dim, self.config.hidden_dim, 1, rng
        )

        self._train_steps: int = 0
        self._version: str = f"ppo_v1_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H')}"

        log.info(
            "[RLPolicy] PPOAgent 初始化: obs_dim={} n_actions={} hidden={} version={}",
            self.config.obs_dim, self.config.n_actions,
            self.config.hidden_dim, self._version,
        )

    # ──────────────────────────────────────────────────────────
    # 推理接口
    # ──────────────────────────────────────────────────────────

    def predict(
        self,
        obs: list[float],
        deterministic: bool = False,
    ) -> tuple[int, float, float, float]:
        """
        从观测向量推理动作。

        Args:
            obs:           特征向量（长度 = obs_dim）
            deterministic: True = argmax（评估用）；False = 采样（训练用）

        Returns:
            (action_index, action_value, confidence, log_prob)
            - action_index: 离散动作索引
            - action_value: 动作强度（confidence 的归一化形式）
            - confidence:   该动作的概率
            - log_prob:     log P(action|obs)（PPO 更新用）
        """
        x = np.array(obs, dtype=np.float64)
        logits, _ = self._policy_net.forward(x)
        probs = _softmax(logits.squeeze())

        if deterministic:
            idx = int(np.argmax(probs))
        else:
            # 按概率采样
            cumsum = np.cumsum(probs)
            r = self._py_rng.random()
            idx = int(np.searchsorted(cumsum, r))
            idx = min(idx, self.config.n_actions - 1)

        confidence = float(probs[idx])
        log_prob = float(np.log(max(probs[idx], 1e-10)))
        action_value = confidence  # v1 中 action_value = confidence

        return idx, action_value, confidence, log_prob

    def value(self, obs: list[float]) -> float:
        """估计状态价值 V(s)。"""
        x = np.array(obs, dtype=np.float64)
        v, _ = self._value_net.forward(x)
        return float(v.squeeze())

    # ──────────────────────────────────────────────────────────
    # 训练接口
    # ──────────────────────────────────────────────────────────

    def update(
        self,
        obs_batch: list[list[float]],
        action_indices: list[int],
        old_log_probs: list[float],
        advantages: list[float],
        returns: list[float],
    ) -> dict[str, float]:
        """
        PPO-Clip 更新一个 mini-batch。

        Args:
            obs_batch:      观测批次
            action_indices: 各步的离散动作索引
            old_log_probs:  收集数据时的 log_prob（旧策略）
            advantages:     GAE 优势估计（已标准化）
            returns:        value target（advantage + value_est）

        Returns:
            loss 字典（用于监控）
        """
        if not obs_batch:
            return {}

        X = np.array(obs_batch, dtype=np.float64)
        actions = np.array(action_indices, dtype=np.int32)
        old_lp = np.array(old_log_probs, dtype=np.float64)
        adv = np.array(advantages, dtype=np.float64)
        ret = np.array(returns, dtype=np.float64)

        # 标准化 advantage
        adv_std = adv.std() + 1e-8
        adv_norm = (adv - adv.mean()) / adv_std

        total_policy_loss = 0.0
        total_value_loss = 0.0
        total_entropy = 0.0

        for _ in range(self.config.n_epochs):
            # ── Policy loss
            logits, hidden_p = self._policy_net.forward(X)
            probs = _softmax(logits)
            log_probs = np.log(np.clip(probs, 1e-10, 1.0))
            batch_size = len(obs_batch)

            # log prob of taken actions
            taken_lp = log_probs[np.arange(batch_size), actions]
            ratio = np.exp(taken_lp - old_lp)
            clipped = np.clip(ratio, 1 - self.config.clip_epsilon, 1 + self.config.clip_epsilon)
            policy_loss = -np.mean(np.minimum(ratio * adv_norm, clipped * adv_norm))

            # entropy bonus
            entropy = -np.mean(np.sum(probs * log_probs, axis=-1))
            total_entropy += entropy

            policy_obj = policy_loss - self.config.entropy_coeff * entropy
            total_policy_loss += policy_loss

            # ── Value loss
            v_out, hidden_v = self._value_net.forward(X)
            v_pred = v_out.squeeze()
            value_loss = 0.5 * np.mean((v_pred - ret) ** 2)
            total_value_loss += value_loss

            # ── Backward (simple SGD-like numerical gradient for policy)
            # Policy gradient (via REINFORCE with ratio)
            grad_policy = np.zeros_like(logits)
            for i in range(batch_size):
                clip_mask = (ratio[i] < 1 + self.config.clip_epsilon) and \
                            (ratio[i] > 1 - self.config.clip_epsilon)
                g = -adv_norm[i] * ratio[i] if clip_mask else 0.0
                g += self.config.entropy_coeff * (log_probs[i] + 1.0)
                grad_policy[i] = g * (probs[i] - (np.arange(self.config.n_actions) == actions[i]))

            grad_policy /= batch_size
            # Clip gradient norm
            gn = np.linalg.norm(grad_policy)
            if gn > self.config.max_grad_norm:
                grad_policy = grad_policy * self.config.max_grad_norm / gn

            # W2 grad = hidden^T @ grad_policy
            gW2 = hidden_p.T @ grad_policy
            gb2 = grad_policy.sum(axis=0)
            grad_h = grad_policy @ self._policy_net.W2.T * (hidden_p > 0)
            gW1 = X.T @ grad_h
            gb1 = grad_h.sum(axis=0)
            self._policy_net.adam_update(gW1, gb1, gW2, gb2, self.config.lr_policy)

            # Value backward
            v_err = (v_pred - ret).reshape(-1, 1)
            gW2_v = hidden_v.T @ v_err / batch_size
            gb2_v = v_err.mean(axis=0)
            grad_h_v = (v_err @ self._value_net.W2.T) * (hidden_v > 0)
            gW1_v = X.T @ grad_h_v / batch_size
            gb1_v = grad_h_v.sum(axis=0) / batch_size
            self._value_net.adam_update(gW1_v, gb1_v, gW2_v, gb2_v, self.config.lr_value)

        self._train_steps += 1
        n_epochs = self.config.n_epochs
        losses = {
            "policy_loss": total_policy_loss / n_epochs,
            "value_loss": total_value_loss / n_epochs,
            "entropy": total_entropy / n_epochs,
            "train_steps": self._train_steps,
        }
        log.debug(
            "[RLPolicy] 训练 step={}: policy_loss={:.4f} value_loss={:.4f} entropy={:.4f}",
            self._train_steps,
            losses["policy_loss"],
            losses["value_loss"],
            losses["entropy"],
        )
        return losses

    # ──────────────────────────────────────────────────────────
    # 序列化接口
    # ──────────────────────────────────────────────────────────

    def version(self) -> str:
        return self._version

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self._version,
            "train_steps": self._train_steps,
            "config": {
                "obs_dim": self.config.obs_dim,
                "n_actions": self.config.n_actions,
                "hidden_dim": self.config.hidden_dim,
                "lr_policy": self.config.lr_policy,
                "lr_value": self.config.lr_value,
                "clip_epsilon": self.config.clip_epsilon,
                "entropy_coeff": self.config.entropy_coeff,
                "gamma": self.config.gamma,
            },
            "policy_net": self._policy_net.to_dict(),
            "value_net": self._value_net.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PPOAgent":
        cfg_d = d.get("config", {})
        cfg = PPOConfig(
            obs_dim=cfg_d.get("obs_dim", 24),
            n_actions=cfg_d.get("n_actions", 8),
            hidden_dim=cfg_d.get("hidden_dim", 64),
        )
        agent = cls(cfg)
        agent._version = d.get("version", agent._version)
        agent._train_steps = d.get("train_steps", 0)
        agent._policy_net = _LinearNet.from_dict(
            d["policy_net"], cfg.obs_dim, cfg.hidden_dim, cfg.n_actions
        )
        agent._value_net = _LinearNet.from_dict(
            d["value_net"], cfg.obs_dim, cfg.hidden_dim, 1
        )
        return agent

    def diagnostics(self) -> dict[str, Any]:
        return {
            "version": self._version,
            "train_steps": self._train_steps,
            "obs_dim": self.config.obs_dim,
            "n_actions": self.config.n_actions,
        }
