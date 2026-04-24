"""
modules/evolution/candidate_registry.py — 策略/模型/Policy 候选清单管理

设计说明：
- 统一维护所有候选（model / strategy / policy / params）的注册、查询、状态变更
- 持久化到 storage/evolution/candidates.json（原子写入）
- 线程安全（RLock）
- 候选生命周期：candidate → shadow → paper → active → paused/retired
- 支持按 type / status / owner 多维查询

日志标签：[Evolution]
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.logger import get_logger
from modules.alpha.contracts.evolution_types import (
    CandidateSnapshot,
    CandidateStatus,
    CandidateType,
)

log = get_logger(__name__)

_DEFAULT_REGISTRY_PATH = "storage/evolution/candidates.json"


# ══════════════════════════════════════════════════════════════
# 内部可变记录（存于注册表）
# ══════════════════════════════════════════════════════════════

@dataclass
class _CandidateRecord:
    """内部可变记录，注册表持久化格式。"""

    candidate_id: str
    candidate_type: str
    owner: str
    version: str
    status: str
    created_at: str           # ISO string
    promoted_at: Optional[str] = None
    sharpe_30d: Optional[float] = None
    max_drawdown_30d: Optional[float] = None
    win_rate_30d: Optional[float] = None
    ab_lift: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_snapshot(self) -> CandidateSnapshot:
        return CandidateSnapshot(
            candidate_id=self.candidate_id,
            candidate_type=self.candidate_type,
            owner=self.owner,
            version=self.version,
            status=self.status,
            created_at=datetime.fromisoformat(self.created_at),
            promoted_at=(
                datetime.fromisoformat(self.promoted_at)
                if self.promoted_at else None
            ),
            sharpe_30d=self.sharpe_30d,
            max_drawdown_30d=self.max_drawdown_30d,
            win_rate_30d=self.win_rate_30d,
            ab_lift=self.ab_lift,
            metadata=self.metadata,
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "_CandidateRecord":
        return cls(
            candidate_id=d["candidate_id"],
            candidate_type=d["candidate_type"],
            owner=d["owner"],
            version=d["version"],
            status=d["status"],
            created_at=d["created_at"],
            promoted_at=d.get("promoted_at"),
            sharpe_30d=d.get("sharpe_30d"),
            max_drawdown_30d=d.get("max_drawdown_30d"),
            win_rate_30d=d.get("win_rate_30d"),
            ab_lift=d.get("ab_lift"),
            metadata=d.get("metadata", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ══════════════════════════════════════════════════════════════
# CandidateRegistry
# ══════════════════════════════════════════════════════════════

class CandidateRegistry:
    """
    候选注册表。

    提供候选生命周期管理、按条件查询、指标更新。
    所有写操作都触发持久化（原子写入）。
    """

    def __init__(self, registry_path: str = _DEFAULT_REGISTRY_PATH) -> None:
        self._path = registry_path
        self._lock = threading.RLock()
        self._records: dict[str, _CandidateRecord] = {}

        os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        self._load()
        log.info("[Evolution] CandidateRegistry 初始化: path={} records={}",
                 self._path, len(self._records))

    # ─────────────────────────────────────────────
    # 注册
    # ─────────────────────────────────────────────

    def register(
        self,
        candidate_type: CandidateType,
        owner: str,
        version: str,
        candidate_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> CandidateSnapshot:
        """
        注册一个新候选。

        Args:
            candidate_type: 候选类型
            owner:          所属模块名称
            version:        版本字符串
            candidate_id:   显式指定 ID（None = 自动生成）
            metadata:       附加信息

        Returns:
            新候选的 CandidateSnapshot
        """
        cid = candidate_id or f"{candidate_type.value}_{owner}_{version}_{uuid.uuid4().hex[:6]}"
        now = datetime.now(tz=timezone.utc).isoformat()

        with self._lock:
            record = _CandidateRecord(
                candidate_id=cid,
                candidate_type=candidate_type.value,
                owner=owner,
                version=version,
                status=CandidateStatus.CANDIDATE.value,
                created_at=now,
                metadata=metadata or {},
            )
            self._records[cid] = record
            self._save()

        snap = record.to_snapshot()
        log.info("[Evolution] 候选注册: id={} type={} owner={} version={}",
                 cid, candidate_type.value, owner, version)
        return snap

    # ─────────────────────────────────────────────
    # 状态变更
    # ─────────────────────────────────────────────

    def transition(
        self,
        candidate_id: str,
        new_status: CandidateStatus,
        reason: str = "",
        metadata_update: Optional[dict[str, Any]] = None,
    ) -> Optional[CandidateSnapshot]:
        """
        变更候选状态。

        Returns:
            更新后的 CandidateSnapshot，候选不存在时返回 None。
        """
        with self._lock:
            record = self._records.get(candidate_id)
            if record is None:
                log.warning("[Evolution] transition: 候选不存在: {}", candidate_id)
                return None

            old_status = record.status
            record.status = new_status.value
            record.promoted_at = datetime.now(tz=timezone.utc).isoformat()
            if metadata_update:
                record.metadata.update(metadata_update)
            self._save()

        log.info("[Evolution] 状态变更: id={} {} → {} reason={}",
                 candidate_id, old_status, new_status.value, reason)
        return record.to_snapshot()

    def update_metrics(
        self,
        candidate_id: str,
        sharpe_30d: Optional[float] = None,
        max_drawdown_30d: Optional[float] = None,
        win_rate_30d: Optional[float] = None,
        ab_lift: Optional[float] = None,
    ) -> Optional[CandidateSnapshot]:
        """更新候选评估指标。"""
        with self._lock:
            record = self._records.get(candidate_id)
            if record is None:
                return None
            if sharpe_30d is not None:
                record.sharpe_30d = sharpe_30d
            if max_drawdown_30d is not None:
                record.max_drawdown_30d = max_drawdown_30d
            if win_rate_30d is not None:
                record.win_rate_30d = win_rate_30d
            if ab_lift is not None:
                record.ab_lift = ab_lift
            self._save()

        log.debug("[Evolution] 指标更新: id={} sharpe={} maxdd={} wr={} ab_lift={}",
                  candidate_id, sharpe_30d, max_drawdown_30d, win_rate_30d, ab_lift)
        return record.to_snapshot()

    # ─────────────────────────────────────────────
    # 查询
    # ─────────────────────────────────────────────

    def get(self, candidate_id: str) -> Optional[CandidateSnapshot]:
        with self._lock:
            rec = self._records.get(candidate_id)
        return rec.to_snapshot() if rec else None

    def list_by_status(
        self,
        status: CandidateStatus,
        candidate_type: Optional[CandidateType] = None,
    ) -> list[CandidateSnapshot]:
        with self._lock:
            records = list(self._records.values())
        result = [
            r.to_snapshot() for r in records
            if r.status == status.value
            and (candidate_type is None or r.candidate_type == candidate_type.value)
        ]
        return result

    def list_active(self) -> list[CandidateSnapshot]:
        return self.list_by_status(CandidateStatus.ACTIVE)

    def list_all(self) -> list[CandidateSnapshot]:
        with self._lock:
            return [r.to_snapshot() for r in self._records.values()]

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            records = list(self._records.values())
        counts: dict[str, int] = {}
        for r in records:
            counts[r.status] = counts.get(r.status, 0) + 1
        return counts

    # ─────────────────────────────────────────────
    # 诊断
    # ─────────────────────────────────────────────

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            n = len(self._records)
        return {
            "total_candidates": n,
            "by_status": self.count_by_status(),
            "registry_path": self._path,
        }

    # ─────────────────────────────────────────────
    # 持久化（原子写入）
    # ─────────────────────────────────────────────

    def _save(self) -> None:
        """原子写入（tmp file → os.replace）。"""
        data = [r.to_dict() for r in self._records.values()]
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self._path)
        except Exception:
            log.exception("[Evolution] 注册表持久化失败: path={}", self._path)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                rec = _CandidateRecord.from_dict(item)
                self._records[rec.candidate_id] = rec
        except Exception:
            log.exception("[Evolution] 注册表加载失败: path={}", self._path)
