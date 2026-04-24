"""
modules/alpha/ml/model_registry.py — 模型版本管理器

设计说明：
- 轻量本地模型注册表：保存/加载/激活/回滚 SignalModel 版本
- 每个版本记录：版本号、路径、校准结果、OOS 指标、创建时间
- 支持 promote (最新 → 生产)、rollback (生产 → 上一版本)
- 基于本地文件系统（models/ 目录），不依赖外部服务
- 持久化格式：每个模型存为 pickle，注册表元数据存为 JSON

日志标签：[ModelRegistry]
"""

from __future__ import annotations

import json
import pickle
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger

log = get_logger(__name__)

# 进程级单调递增计数器，确保版本 ID 唯一（Windows 计时精度不足时使用）
_version_counter_lock = threading.Lock()
_version_counter = 0


def _next_version_id() -> str:
    global _version_counter
    with _version_counter_lock:
        _version_counter += 1
        seq = _version_counter
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"v_{ts}_{seq:04d}"


# ══════════════════════════════════════════════════════════════
# 版本元数据
# ══════════════════════════════════════════════════════════════

@dataclass
class ModelVersion:
    """单个模型版本的元数据记录。"""
    version_id: str             # 唯一版本 ID，格式：v_{yyyymmddTHHMMSS}
    model_path: str             # pickle 文件相对路径
    model_type: str             # "rf" | "lgbm" | "lr" 等
    created_at: str             # ISO 8601 时间戳
    is_active: bool             # 是否为当前生产版本

    # OOS 指标
    oos_auc: float = 0.0
    oos_accuracy: float = 0.0
    oos_f1: float = 0.0

    # 关联的阈值版本
    threshold_version: Optional[str] = None
    recommended_buy_threshold: float = 0.60
    recommended_sell_threshold: float = 0.40

    # 可选的附加元数据（自由格式）
    metadata: Dict[str, Any] = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════
# 注册表
# ══════════════════════════════════════════════════════════════

class ModelRegistry:
    """
    本地模型版本注册表。

    目录结构：
        models_dir/
            registry.json          ← 注册表元数据
            v_20260423T120000.pkl  ← 模型 pickle
            v_20260424T083000.pkl
            ...

    使用示例：
        registry = ModelRegistry(models_dir="./models")

        # 保存并注册新版本
        version_id = registry.register(
            model=trained_model,
            model_type="rf",
            oos_auc=0.72,
            oos_f1=0.65,
        )

        # 激活（推广为生产版本）
        registry.promote(version_id)

        # 加载当前生产模型
        model = registry.load_active()

        # 回滚到上一版本
        registry.rollback()

    Args:
        models_dir: 模型存储根目录
    """

    REGISTRY_FILE = "registry.json"

    def __init__(self, models_dir: str | Path = "./models") -> None:
        self._dir = Path(models_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._registry_path = self._dir / self.REGISTRY_FILE
        self._versions: List[ModelVersion] = self._load_registry()

        log.info(
            "[ModelRegistry] 初始化: dir={} versions={} active={}",
            self._dir, len(self._versions),
            self._active_version_id() or "None",
        )

    # ────────────────────────────────────────────────────────────
    # 写入
    # ────────────────────────────────────────────────────────────

    def register(
        self,
        model: Any,
        model_type: str = "rf",
        oos_auc: float = 0.0,
        oos_accuracy: float = 0.0,
        oos_f1: float = 0.0,
        threshold_version: Optional[str] = None,
        recommended_buy_threshold: float = 0.60,
        recommended_sell_threshold: float = 0.40,
        metadata: Optional[Dict[str, Any]] = None,
        auto_promote: bool = False,
    ) -> str:
        """
        保存模型并注册新版本。

        Args:
            model:           已训练的模型对象（需支持 pickle）
            model_type:      模型类型标识
            oos_auc:         样本外 AUC（用于版本比较）
            auto_promote:    是否立即激活为生产版本

        Returns:
            version_id（格式：v_{yyyymmddTHHMMSS}）
        """
        version_id = _next_version_id()
        model_filename = f"{version_id}.pkl"
        model_path = self._dir / model_filename

        # 保存 pickle
        with open(model_path, "wb") as f:
            pickle.dump(model, f)

        version = ModelVersion(
            version_id=version_id,
            model_path=model_filename,
            model_type=model_type,
            created_at=datetime.now(tz=timezone.utc).isoformat(),
            is_active=False,
            oos_auc=oos_auc,
            oos_accuracy=oos_accuracy,
            oos_f1=oos_f1,
            threshold_version=threshold_version,
            recommended_buy_threshold=recommended_buy_threshold,
            recommended_sell_threshold=recommended_sell_threshold,
            metadata=metadata or {},
        )
        self._versions.append(version)
        self._save_registry()

        log.info(
            "[ModelRegistry] 注册新版本: id={} type={} auc={:.4f} f1={:.4f}",
            version_id, model_type, oos_auc, oos_f1,
        )

        if auto_promote:
            self.promote(version_id)

        return version_id

    def promote(self, version_id: str) -> None:
        """将指定版本激活为生产版本（同时取消其他版本的激活状态）。"""
        found = False
        for v in self._versions:
            if v.version_id == version_id:
                v.is_active = True
                found = True
            else:
                v.is_active = False

        if not found:
            raise KeyError(f"版本 {version_id!r} 不存在")

        self._save_registry()
        log.info("[ModelRegistry] 激活版本: {}", version_id)

    def rollback(self) -> Optional[str]:
        """
        回滚到上一个版本（将 is_active 切换到倒数第二个版本）。

        Returns:
            回滚后激活的 version_id，若无法回滚则返回 None
        """
        if len(self._versions) < 2:
            log.warning("[ModelRegistry] 版本数量不足，无法回滚")
            return None

        # 找当前激活版本的索引
        active_idx = next(
            (i for i, v in enumerate(self._versions) if v.is_active), None
        )

        if active_idx is None or active_idx == 0:
            log.warning("[ModelRegistry] 无激活版本或已在最早版本，无法回滚")
            return None

        target_version = self._versions[active_idx - 1]
        self.promote(target_version.version_id)
        log.info("[ModelRegistry] 回滚完成: 激活版本={}", target_version.version_id)
        return target_version.version_id

    # ────────────────────────────────────────────────────────────
    # 读取
    # ────────────────────────────────────────────────────────────

    def load_active(self) -> Any:
        """加载当前激活版本的模型对象。"""
        active = self._get_active_version()
        if active is None:
            raise RuntimeError("没有激活的模型版本，请先调用 promote()")

        model_path = self._dir / active.model_path
        if not model_path.exists():
            raise FileNotFoundError(f"模型文件不存在: {model_path}")

        with open(model_path, "rb") as f:
            model = pickle.load(f)

        log.info(
            "[ModelRegistry] 加载激活模型: version={} type={} auc={:.4f}",
            active.version_id, active.model_type, active.oos_auc,
        )
        return model

    def load_version(self, version_id: str) -> Any:
        """加载指定版本的模型对象。"""
        version = self._get_version(version_id)
        if version is None:
            raise KeyError(f"版本 {version_id!r} 不存在")

        model_path = self._dir / version.model_path
        with open(model_path, "rb") as f:
            return pickle.load(f)

    @property
    def active_version(self) -> Optional[ModelVersion]:
        """当前激活版本的元数据（只读）。"""
        return self._get_active_version()

    @property
    def all_versions(self) -> List[ModelVersion]:
        """所有版本列表（按注册时间升序，只读副本）。"""
        return list(self._versions)

    def latest_version(self) -> Optional[ModelVersion]:
        """最新注册版本的元数据（不一定是激活版本）。"""
        return self._versions[-1] if self._versions else None

    # ────────────────────────────────────────────────────────────
    # 持久化
    # ────────────────────────────────────────────────────────────

    def _load_registry(self) -> List[ModelVersion]:
        if not self._registry_path.exists():
            return []
        with open(self._registry_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        versions = []
        for item in data.get("versions", []):
            versions.append(ModelVersion(**item))
        return versions

    def _save_registry(self) -> None:
        data = {
            "versions": [
                {
                    "version_id": v.version_id,
                    "model_path": v.model_path,
                    "model_type": v.model_type,
                    "created_at": v.created_at,
                    "is_active": v.is_active,
                    "oos_auc": v.oos_auc,
                    "oos_accuracy": v.oos_accuracy,
                    "oos_f1": v.oos_f1,
                    "threshold_version": v.threshold_version,
                    "recommended_buy_threshold": v.recommended_buy_threshold,
                    "recommended_sell_threshold": v.recommended_sell_threshold,
                    "metadata": v.metadata,
                }
                for v in self._versions
            ]
        }
        with open(self._registry_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    # ────────────────────────────────────────────────────────────
    # 辅助
    # ────────────────────────────────────────────────────────────

    def _get_active_version(self) -> Optional[ModelVersion]:
        return next((v for v in self._versions if v.is_active), None)

    def _active_version_id(self) -> Optional[str]:
        v = self._get_active_version()
        return v.version_id if v else None

    def _get_version(self, version_id: str) -> Optional[ModelVersion]:
        return next((v for v in self._versions if v.version_id == version_id), None)

    def diagnostics(self) -> dict:
        """返回注册表诊断快照。"""
        return {
            "models_dir": str(self._dir),
            "total_versions": len(self._versions),
            "active_version": self._active_version_id(),
            "versions": [
                {
                    "id": v.version_id,
                    "type": v.model_type,
                    "auc": v.oos_auc,
                    "active": v.is_active,
                }
                for v in self._versions
            ],
        }
