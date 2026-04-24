"""
modules/alpha/rl/rollout_store.py — 训练轨迹缓存

设计说明：
- 环形缓冲区存储 RolloutStep（FIFO，满了自动覆盖最老数据）
- 支持批量采样（用于 PPO mini-batch 更新）
- 计算 GAE (Generalized Advantage Estimation) 用于 PPO
- 线程安全（threading.Lock）
- 支持 episode 边界标记（done=True 时 GAE 截断）

日志标签：[RLEnv]
"""

from __future__ import annotations

import random
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from core.logger import get_logger
from modules.alpha.contracts.rl_types import RolloutStep

log = get_logger(__name__)


@dataclass
class RolloutStoreConfig:
    """
    RolloutStore 配置。

    Attributes:
        capacity:    环形缓冲区容量（最多保留多少步经验）
        gamma:       折扣因子（PPO GAE 计算）
        lambda_:     GAE lambda（平衡 bias-variance）
        seed:        随机采样种子（replay 可复现）
    """

    capacity: int = 10000
    gamma: float = 0.99
    lambda_: float = 0.95
    seed: Optional[int] = 42


class RolloutStore:
    """
    环形经验回放缓冲区。

    线程安全：所有写操作持 Lock。
    """

    def __init__(self, config: Optional[RolloutStoreConfig] = None) -> None:
        self.config = config or RolloutStoreConfig()
        self._lock = threading.Lock()
        self._buffer: deque[RolloutStep] = deque(maxlen=self.config.capacity)
        self._rng = random.Random(self.config.seed)
        self._total_added: int = 0
        self._episode_count: int = 0
        log.info(
            "[RLEnv] RolloutStore 初始化: capacity={} gamma={} lambda_={}",
            self.config.capacity, self.config.gamma, self.config.lambda_,
        )

    def add(self, step: RolloutStep) -> None:
        """
        添加单步经验到缓冲区。

        Args:
            step: RolloutStep（已计算 reward）
        """
        with self._lock:
            self._buffer.append(step)
            self._total_added += 1
            if step.done:
                self._episode_count += 1
            log.debug(
                "[RLEnv] 添加 rollout step: action={} reward={:.4f} done={} "
                "buffer_size={} episodes={}",
                step.action_type.value, step.reward, step.done,
                len(self._buffer), self._episode_count,
            )

    def sample(self, batch_size: int) -> list[RolloutStep]:
        """
        随机采样 batch_size 步经验（无替换）。

        Args:
            batch_size: 采样数量（若缓冲区小于 batch_size，返回全部）

        Returns:
            RolloutStep 列表
        """
        with self._lock:
            available = list(self._buffer)
            k = min(batch_size, len(available))
            if k == 0:
                return []
            return self._rng.sample(available, k)

    def get_last_episode(self) -> list[RolloutStep]:
        """
        获取最近一个完整 episode 的步骤（从最后一个 done=True 往前追溯）。

        Returns:
            按时间顺序的步骤列表（空列表 = 未找到完整 episode）
        """
        with self._lock:
            steps = list(self._buffer)
        if not steps:
            return []
        # 从后往前找最近的 done=True
        end = None
        for i in range(len(steps) - 1, -1, -1):
            if steps[i].done:
                end = i
                break
        if end is None:
            return []
        # 再往前找前一个 done=True（episode 起点）
        start = 0
        for i in range(end - 1, -1, -1):
            if steps[i].done:
                start = i + 1
                break
        return steps[start:end + 1]

    def compute_gae(self, steps: list[RolloutStep]) -> list[float]:
        """
        计算 Generalized Advantage Estimation (GAE)。

        GAE 公式：
            delta_t = r_t + gamma * V(s_{t+1}) * (1-done_t) - V(s_t)
            A_t = sum_{l=0..T} (gamma * lambda)^l * delta_t+l

        Args:
            steps: 时间顺序的 RolloutStep 列表

        Returns:
            与 steps 等长的 advantage 列表
        """
        n = len(steps)
        advantages = [0.0] * n
        gae = 0.0

        for i in range(n - 1, -1, -1):
            s = steps[i]
            next_value = 0.0
            if not s.done and i + 1 < n:
                next_value = steps[i + 1].value_est

            delta = s.reward + self.config.gamma * next_value * (1.0 - float(s.done)) - s.value_est
            gae = delta + self.config.gamma * self.config.lambda_ * (1.0 - float(s.done)) * gae
            advantages[i] = gae

        return advantages

    def clear(self) -> None:
        """清空缓冲区。"""
        with self._lock:
            self._buffer.clear()
            self._total_added = 0
            self._episode_count = 0
        log.info("[RLEnv] RolloutStore 已清空")

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "buffer_size": len(self._buffer),
                "capacity": self.config.capacity,
                "total_added": self._total_added,
                "episode_count": self._episode_count,
                "utilization": len(self._buffer) / max(self.config.capacity, 1),
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)
