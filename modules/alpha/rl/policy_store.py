"""
modules/alpha/rl/policy_store.py — Policy 版本管理与持久化

设计说明：
- 保存 / 加载 PPO policy 版本（原子写入，同 W12/W17 cache 风格）
- 维护 policy 状态机：candidate → shadow → paper → active → paused/retired
- 支持回滚（rollback to previous active）
- 支持按状态列举 policies
- 线程安全（threading.RLock）
- 元数据存于 index JSON，权重存于独立的 weight JSON

目录结构：
    storage/policies/
        index.json                   # 所有 policy 的元数据索引
        {policy_id}/
            {version}.json           # policy 权重 + 元数据
            latest.json → 最新活跃版本的软链接数据

日志标签：[RLPolicy]
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.alpha.contracts.rl_types import EvalResult, PolicyStatus
from modules.alpha.rl.ppo_agent import PPOAgent

log = get_logger(__name__)

_DEFAULT_STORE_DIR = "storage/policies"


@dataclass
class PolicyRecord:
    """
    单个 policy 版本的元数据记录（存于 index.json）。

    Attributes:
        policy_id:     policy 唯一标识（如 "btcusdt_ppo_v1"）
        version:       版本字符串
        status:        PolicyStatus
        created_at:    创建时间（ISO string）
        updated_at:    最后更新时间（ISO string）
        eval_sharpe:   最近一次评估 Sharpe（None = 未评估）
        eval_drawdown: 最近一次评估最大回撤
        eval_mode:     最近评估类型（oos/paper/shadow）
        promote_reason: 晋升/降级原因
        weight_path:   权重文件路径（相对 store_dir）
    """

    policy_id: str
    version: str
    status: str   # PolicyStatus.value
    created_at: str
    updated_at: str
    eval_sharpe: Optional[float] = None
    eval_drawdown: Optional[float] = None
    eval_mode: Optional[str] = None
    promote_reason: str = ""
    weight_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class PolicyStore:
    """
    Policy 版本注册表 + 持久化存储。

    线程安全：所有写操作持 RLock。
    """

    def __init__(self, store_dir: str = _DEFAULT_STORE_DIR) -> None:
        self._dir = store_dir
        self._lock = threading.RLock()
        os.makedirs(store_dir, exist_ok=True)
        self._index: dict[str, PolicyRecord] = {}
        self._load_index()
        log.info("[RLPolicy] PolicyStore 初始化: store_dir={}", store_dir)

    # ──────────────────────────────────────────────────────────
    # 保存 / 加载
    # ──────────────────────────────────────────────────────────

    def save(
        self,
        agent: PPOAgent,
        policy_id: str,
        status: PolicyStatus = PolicyStatus.CANDIDATE,
        eval_result: Optional[EvalResult] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> PolicyRecord:
        """
        保存 policy 权重并更新索引。

        原子写入：tmp → os.replace

        Args:
            agent:       PPO Agent
            policy_id:   policy 逻辑 ID（如 "btcusdt_ppo"）
            status:      初始状态
            eval_result: 评估结果（可选）
            metadata:    附加元数据

        Returns:
            新创建的 PolicyRecord
        """
        with self._lock:
            version = agent.version()
            policy_dir = os.path.join(self._dir, policy_id)
            os.makedirs(policy_dir, exist_ok=True)

            weight_file = f"{version}.json"
            weight_path = os.path.join(policy_dir, weight_file)
            tmp_path = weight_path + ".tmp"

            # 写入权重
            data = agent.to_dict()
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, weight_path)

            now = datetime.now(tz=timezone.utc).isoformat()
            record = PolicyRecord(
                policy_id=policy_id,
                version=version,
                status=status.value,
                created_at=now,
                updated_at=now,
                eval_sharpe=eval_result.sharpe if eval_result else None,
                eval_drawdown=eval_result.max_drawdown if eval_result else None,
                eval_mode=eval_result.eval_mode if eval_result else None,
                weight_path=os.path.join(policy_id, weight_file),
                metadata=metadata or {},
            )
            key = f"{policy_id}::{version}"
            self._index[key] = record
            self._save_index()

            log.info(
                "[RLPolicy] Policy 已保存: policy_id={} version={} status={}",
                policy_id, version, status.value,
            )
            return record

    def load(self, policy_id: str, version: Optional[str] = None) -> Optional[PPOAgent]:
        """
        载入 policy 权重。

        Args:
            policy_id: policy 逻辑 ID
            version:   版本字符串（None = 载入最新 ACTIVE 版本）

        Returns:
            PPOAgent 或 None（未找到时）
        """
        with self._lock:
            record = self._find_record(policy_id, version)
            if record is None:
                log.warning("[RLPolicy] Policy 未找到: policy_id={} version={}", policy_id, version)
                return None

            weight_path = os.path.join(self._dir, record.weight_path)
            if not os.path.exists(weight_path):
                log.error("[RLPolicy] 权重文件不存在: path={}", weight_path)
                return None

            try:
                with open(weight_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                agent = PPOAgent.from_dict(data)
                log.info(
                    "[RLPolicy] Policy 已加载: policy_id={} version={} status={}",
                    policy_id, record.version, record.status,
                )
                return agent
            except Exception:
                log.exception("[RLPolicy] Policy 加载失败: path={}", weight_path)
                return None

    # ──────────────────────────────────────────────────────────
    # 状态管理
    # ──────────────────────────────────────────────────────────

    def promote(
        self,
        policy_id: str,
        version: str,
        new_status: PolicyStatus,
        reason: str = "",
    ) -> bool:
        """
        更新 policy 状态（晋升 / 降级 / 暂停 / 退役）。

        Returns:
            True = 成功，False = 记录未找到
        """
        with self._lock:
            key = f"{policy_id}::{version}"
            if key not in self._index:
                log.warning("[RLPolicy] promote 失败，记录不存在: key={}", key)
                return False

            record = self._index[key]
            old_status = record.status
            record.status = new_status.value
            record.promote_reason = reason
            record.updated_at = datetime.now(tz=timezone.utc).isoformat()
            self._save_index()

            log.info(
                "[RLPolicy] Policy 状态变更: policy_id={} version={} {} → {} reason={}",
                policy_id, version, old_status, new_status.value, reason,
            )
            return True

    def get_active(self, policy_id: str) -> Optional[PolicyRecord]:
        """获取指定 policy_id 的当前 ACTIVE 版本记录。"""
        with self._lock:
            actives = [
                r for r in self._index.values()
                if r.policy_id == policy_id and r.status == PolicyStatus.ACTIVE.value
            ]
            if not actives:
                return None
            # 返回最新的
            return sorted(actives, key=lambda r: r.created_at)[-1]

    def list_by_status(
        self,
        status: PolicyStatus,
        policy_id: Optional[str] = None,
    ) -> list[PolicyRecord]:
        """列举指定状态的 policy 记录。"""
        with self._lock:
            result = [
                r for r in self._index.values()
                if r.status == status.value
                and (policy_id is None or r.policy_id == policy_id)
            ]
            return sorted(result, key=lambda r: r.created_at)

    def rollback(self, policy_id: str) -> Optional[PolicyRecord]:
        """
        回滚到上一个 ACTIVE 版本。

        Rules:
        1. 将当前 ACTIVE 降为 PAUSED
        2. 找最近的 PAUSED/SHADOW 版本提升为 ACTIVE

        Returns:
            新的 ACTIVE PolicyRecord，或 None（无可回滚版本）
        """
        with self._lock:
            all_records = sorted(
                [r for r in self._index.values() if r.policy_id == policy_id],
                key=lambda r: r.created_at,
                reverse=True,
            )
            # 暂停当前 ACTIVE
            for r in all_records:
                if r.status == PolicyStatus.ACTIVE.value:
                    r.status = PolicyStatus.PAUSED.value
                    r.promote_reason = "ROLLBACK_TRIGGERED"
                    r.updated_at = datetime.now(tz=timezone.utc).isoformat()
                    break

            # 找上一个可用版本
            prev = None
            for r in all_records:
                if r.status in (PolicyStatus.PAUSED.value, PolicyStatus.SHADOW.value,
                                PolicyStatus.PAPER.value):
                    prev = r
                    break

            if prev is None:
                log.warning("[RLPolicy] 无可回滚版本: policy_id={}", policy_id)
                self._save_index()
                return None

            prev.status = PolicyStatus.ACTIVE.value
            prev.promote_reason = "ROLLBACK_RESTORED"
            prev.updated_at = datetime.now(tz=timezone.utc).isoformat()
            self._save_index()

            log.info(
                "[RLPolicy] 已回滚: policy_id={} restored_version={}",
                policy_id, prev.version,
            )
            return prev

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            by_status: dict[str, int] = {}
            for r in self._index.values():
                by_status[r.status] = by_status.get(r.status, 0) + 1
            return {
                "total_records": len(self._index),
                "store_dir": self._dir,
                "by_status": by_status,
            }

    # ──────────────────────────────────────────────────────────
    # 内部辅助
    # ──────────────────────────────────────────────────────────

    def _index_path(self) -> str:
        return os.path.join(self._dir, "index.json")

    def _save_index(self) -> None:
        path = self._index_path()
        tmp = path + ".tmp"
        data = {k: vars(v) for k, v in self._index.items()}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    def _load_index(self) -> None:
        path = self._index_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                self._index[k] = PolicyRecord(**v)
        except Exception:
            log.exception("[RLPolicy] 索引加载失败: path={}", path)

    def _find_record(
        self,
        policy_id: str,
        version: Optional[str] = None,
    ) -> Optional[PolicyRecord]:
        if version:
            return self._index.get(f"{policy_id}::{version}")
        # 找最新 ACTIVE
        actives = [
            r for r in self._index.values()
            if r.policy_id == policy_id and r.status == PolicyStatus.ACTIVE.value
        ]
        if actives:
            return sorted(actives, key=lambda r: r.created_at)[-1]
        # fallback: 最新任意状态
        all_r = sorted(
            [r for r in self._index.values() if r.policy_id == policy_id],
            key=lambda r: r.created_at,
        )
        return all_r[-1] if all_r else None
