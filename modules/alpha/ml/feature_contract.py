"""
modules/alpha/ml/feature_contract.py — 特征契约：训练/推理签名一致性保障

设计说明：
- 特征契约记录训练期生成的特征列名、数据类型和版本签名
- 推理期可以用契约验证特征矩阵是否与训练期一致
- 是防止训练/推理特征漂移（train-serve skew）的核心防线

契约版本格式：dk_v{major}_{yyyymm}（DataKitchen major version + 月份戳）

日志标签：[FeatureContract]
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class FeatureContract:
    """
    训练期产出的特征签名契约。

    包含三份特征视图的列名：
    - alpha_features: 传给策略/模型的主特征列
    - regime_features: 传给 MarketRegimeDetector 的列子集
    - diagnostic_features: 用于诊断和监控的辅助列

    Attributes:
        version:             契约版本号（格式：dk_v{major}_{yyyymm}）
        alpha_features:      主特征列名列表（顺序有效，训练/推理必须一致）
        regime_features:     Regime 子特征列名列表
        diagnostic_features: 诊断辅助列名列表
        dtype_map:           各特征列的 dtype 字符串（用于签名计算）
        pipeline_config:     生成本契约的 pipeline 配置快照（只读）
        signature:           基于特征列名的 SHA-256 前16位（用于快速比对）
    """

    version: str
    alpha_features: list[str] = field(default_factory=list)
    regime_features: list[str] = field(default_factory=list)
    diagnostic_features: list[str] = field(default_factory=list)
    dtype_map: dict[str, str] = field(default_factory=dict)
    pipeline_config: dict[str, Any] = field(default_factory=dict)
    signature: str = ""

    def __post_init__(self) -> None:
        if not self.signature:
            self.signature = self._compute_signature()

    def _compute_signature(self) -> str:
        """基于 alpha 特征列名的稳定哈希（顺序敏感）。"""
        blob = json.dumps(self.alpha_features, ensure_ascii=False, sort_keys=False)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    # ────────────────────────────────────────────────────────────
    # 验证接口
    # ────────────────────────────────────────────────────────────

    def validate(self, df_columns: list[str]) -> tuple[bool, list[str]]:
        """
        校验推理期特征列是否覆盖训练期所有 alpha_features。

        Returns:
            (ok, missing_cols)
            ok=True 表示所有必要特征都存在；missing_cols 为缺失列名列表。
        """
        present = set(df_columns)
        missing = [c for c in self.alpha_features if c not in present]
        ok = len(missing) == 0

        if not ok:
            log.error(
                "[FeatureContract] 签名不一致: version={} signature={} 缺少特征列={}",
                self.version, self.signature, missing,
            )
        else:
            log.debug(
                "[FeatureContract] 验证通过: version={} signature={} alpha_features={}",
                self.version, self.signature, len(self.alpha_features),
            )
        return ok, missing

    def extra_columns(self, df_columns: list[str]) -> list[str]:
        """返回推理期存在但训练期不知道的多余列（可用于诊断）。"""
        return [c for c in df_columns if c not in set(self.alpha_features)]

    # ────────────────────────────────────────────────────────────
    # 序列化接口
    # ────────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "alpha_features": self.alpha_features,
            "regime_features": self.regime_features,
            "diagnostic_features": self.diagnostic_features,
            "dtype_map": self.dtype_map,
            "pipeline_config": self.pipeline_config,
            "signature": self.signature,
        }

    def save(self, path: str | Path) -> None:
        """将契约序列化为 JSON 文件。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2))
        log.info(
            "[FeatureContract] 已保存: path={} version={} signature={}",
            p, self.version, self.signature,
        )

    @classmethod
    def load(cls, path: str | Path) -> "FeatureContract":
        """从 JSON 文件加载契约。"""
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        contract = cls(**data)
        log.info(
            "[FeatureContract] 已加载: path={} version={} signature={} alpha_features={}",
            p, contract.version, contract.signature, len(contract.alpha_features),
        )
        return contract
