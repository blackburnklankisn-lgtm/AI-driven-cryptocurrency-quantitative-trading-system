"""
modules/alpha/ml/data_kitchen.py — DataKitchen 特征中台 v1

设计说明：
- DataKitchen 是训练/推理的统一特征入口，是 MLFeatureBuilder 的上游编排层
- 三个输出视图：alpha_features（主模型输入）、regime_features（Regime 检测输入）、diagnostic_features（诊断监控）
- 训练期：fit() → 产出 FeatureContract + 处理后的特征矩阵
- 推理期：transform(raw_df) → 经过同样处理的特征矩阵 + 契约验证
- 可选的 PCA/去相关/低方差过滤，通过 DataKitchenConfig 开关控制

与 freqtrade DataKitchen 的对应关系：
  我们的 VarianceFilter    ↔ datasieve low-var removal
  我们的 Decorrelator      ↔ datasieve high-correlation removal
  我们的 PCAReducer        ↔ freqai PCA compression
  我们的 FeatureContract   ↔ feature_parameters 训练签名

日志标签：[DataKitchen] [FeatureDiag]
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from core.logger import get_logger
from modules.alpha.ml.feature_builder import FeatureConfig, MLFeatureBuilder
from modules.alpha.ml.feature_contract import FeatureContract
from modules.alpha.ml.feature_pipeline import FeaturePipeline
from modules.alpha.ml.feature_selectors import (
    Decorrelator,
    DecorrelatorConfig,
    PCAConfig,
    PCAReducer,
    VarianceFilter,
    VarianceFilterConfig,
)

log = get_logger(__name__)

_REGIME_CORE_COLUMNS = [
    "ret_roll_mean_20",
    "ret_roll_std_20",
    "price_vs_sma_20",
    "price_vs_sma_50",
    "adx_14",
    "rsi_14",
    "atr_pct_14",
    "bb_width",
    "volume_ratio",
]


# ══════════════════════════════════════════════════════════════
# 输出容器
# ══════════════════════════════════════════════════════════════

@dataclass
class DataKitchenOutput:
    """DataKitchen 三视图输出容器（可选，方法也支持直接返回 dict）。"""
    alpha_features: pd.DataFrame
    regime_features: pd.DataFrame
    diagnostic_features: pd.DataFrame
    contract: FeatureContract | None = None


# ══════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════

@dataclass
class DataKitchenConfig:
    """DataKitchen 行为配置。"""

    # 版本号（格式：dk_v{major}_{yyyymm}）
    version: str = "dk_v1"

    # 特征构建器配置（对应 MLFeatureBuilder 的 FeatureConfig）
    feature_config: FeatureConfig = field(default_factory=FeatureConfig)

    # ── 处理开关 ──────────────────────────────────────────
    enable_variance_filter: bool = True   # 低方差过滤
    enable_decorrelation: bool = True     # 去相关
    enable_pca: bool = False              # PCA 压缩（默认关：保留可解释性）

    # ── 子配置 ────────────────────────────────────────────
    variance_filter_config: VarianceFilterConfig = field(default_factory=VarianceFilterConfig)
    decorrelator_config: DecorrelatorConfig = field(default_factory=DecorrelatorConfig)
    pca_config: PCAConfig = field(default_factory=PCAConfig)

    # ── 三个视图的列筛选规则 ──────────────────────────────
    # regime_features: 保留哪些前缀列（未指定时取所有列的子集）
    regime_feature_prefixes: list[str] = field(default_factory=lambda: [
        "close_return", "rsi_", "macd_", "atr_", "adx",
        "ret_roll_mean", "ret_roll_std", "bb_pctb", "bb_width",
        "price_vs_sma", "volume_ratio", "oc_", "st_",
    ])

    # diagnostic_features: 哪些列列入诊断视图
    diagnostic_feature_prefixes: list[str] = field(default_factory=lambda: [
        "close_return", "close_to_high", "close_to_low",
        "hl_range", "volume_ratio", "oc_", "st_",
    ])

    # 是否打印诊断日志（输入/输出维度变化、NaN 比例等）
    emit_diagnostics: bool = True


# ══════════════════════════════════════════════════════════════
# DataKitchen 主类
# ══════════════════════════════════════════════════════════════

class DataKitchen:
    """
    特征中台 v1：统一训练与推理的特征生成、筛选、验证入口。

    使用流程：
        # 训练期
        dk = DataKitchen(config=DataKitchenConfig())
        views, contract = dk.fit(ohlcv_df)
        X_alpha = views["alpha_features"]
        contract.save("./models/feature_contract.json")

        # 推理期（持续运行时）
        dk2 = DataKitchen(config=cfg)
        dk2.load_contract("./models/feature_contract.json")
        views2 = dk2.transform(latest_ohlcv_df, validate_contract=True)
        X_alpha_live = views2["alpha_features"]

    Args:
        config: DataKitchenConfig 实例
    """

    def __init__(self, config: DataKitchenConfig | None = None) -> None:
        self.config = config or DataKitchenConfig()
        self._feature_builder = MLFeatureBuilder(config=self.config.feature_config)
        self._pipeline = self._build_pipeline()
        self._contract: FeatureContract | None = None
        self._fitted = False

    # ────────────────────────────────────────────────────────────
    # 内部 pipeline 构建
    # ────────────────────────────────────────────────────────────

    def _build_pipeline(self) -> FeaturePipeline:
        cfg = self.config
        pipeline = FeaturePipeline(name="dk_pipeline")

        if cfg.enable_variance_filter:
            pipeline.add_stage(
                "variance_filter",
                VarianceFilter(cfg.variance_filter_config),
            )
        if cfg.enable_decorrelation:
            pipeline.add_stage(
                "decorrelator",
                Decorrelator(cfg.decorrelator_config),
            )
        if cfg.enable_pca:
            pipeline.add_stage(
                "pca_reducer",
                PCAReducer(cfg.pca_config),
            )

        log.info(
            "[DataKitchen] pipeline构建完成: stages={} [{}]",
            len(pipeline.stage_names),
            ", ".join(pipeline.stage_names),
        )
        return pipeline

    # ────────────────────────────────────────────────────────────
    # 训练期入口
    # ────────────────────────────────────────────────────────────

    def fit(
        self,
        raw_df: pd.DataFrame,
    ) -> tuple[dict[str, pd.DataFrame], FeatureContract]:
        """
        训练期：构建特征 → 执行 pipeline fit → 产出三视图 + FeatureContract。

        Args:
            raw_df: 原始 OHLCV DataFrame

        Returns:
            (views, contract)
            views["alpha_features"]:      主模型输入（行 × 特征列）
            views["regime_features"]:     Regime 检测输入
            views["diagnostic_features"]: 诊断监控视图
        """
        t_start = time.perf_counter()

        log.info(
            "[DataKitchen] fit开始: rows={} cols={} version={}",
            len(raw_df), len(raw_df.columns), self.config.version,
        )

        # Step 1: 特征构建（MLFeatureBuilder）
        t1 = time.perf_counter()
        feature_df = self._feature_builder.build(raw_df)
        feature_names_raw = self._feature_builder.get_feature_names()

        # 取纯特征列，剔除 NaN 行（预热期）
        X_raw = feature_df[feature_names_raw].dropna()
        log.debug(
            "[DataKitchen] Step1/特征构建: 行={} 特征列={} elapsed={:.1f}ms",
            len(X_raw), len(feature_names_raw), (time.perf_counter() - t1) * 1000,
        )

        if self.config.emit_diagnostics:
            self._log_nan_diagnostics(feature_df, feature_names_raw)

        # Step 2: Pipeline fit（去相关/低方差/PCA）
        t2 = time.perf_counter()
        X_processed = self._pipeline.fit_transform(X_raw)
        log.debug(
            "[DataKitchen] Step2/Pipeline: 输入列={} → 输出列={} elapsed={:.1f}ms",
            len(feature_names_raw), len(X_processed.columns),
            (time.perf_counter() - t2) * 1000,
        )

        # Step 3: 构建三个视图
        # alpha_features 走筛选后的矩阵；regime/diagnostic 保留原始可解释特征，
        # 避免规则引擎依赖列被 variance/decorrelation 提前剔除。
        views = self._build_views(X_processed, raw_feature_matrix=X_raw)

        # Step 4: 生成 FeatureContract
        version = self._versioned_name()
        contract = FeatureContract(
            version=version,
            alpha_features=list(views["alpha_features"].columns),
            regime_features=list(views["regime_features"].columns),
            diagnostic_features=list(views["diagnostic_features"].columns),
            dtype_map={
                c: str(views["alpha_features"][c].dtype)
                for c in views["alpha_features"].columns
            },
            pipeline_config=self._pipeline.diagnostics(),
        )
        self._contract = contract
        self._fitted = True

        elapsed = (time.perf_counter() - t_start) * 1000
        log.info(
            "[DataKitchen] fit完成: version={} signature={} "
            "alpha_features={} regime_features={} diag_features={} elapsed={:.1f}ms",
            version, contract.signature,
            len(contract.alpha_features),
            len(contract.regime_features),
            len(contract.diagnostic_features),
            elapsed,
        )

        return views, contract

    # ────────────────────────────────────────────────────────────
    # 推理期入口
    # ────────────────────────────────────────────────────────────

    def transform(
        self,
        raw_df: pd.DataFrame,
        validate_contract: bool = True,
    ) -> dict[str, pd.DataFrame]:
        """
        推理期：使用训练期 pipeline 参数处理新数据 → 产出三视图。

        Args:
            raw_df:            最新的 OHLCV DataFrame
            validate_contract: 是否验证特征列与训练契约一致

        Returns:
            views["alpha_features"], views["regime_features"], views["diagnostic_features"]
        """
        if not self._fitted:
            raise RuntimeError(
                "DataKitchen.transform() 必须先调用 fit() 或 load_contract()"
            )

        # Step 1: 特征构建
        feature_df = self._feature_builder.build(raw_df)
        feature_names_raw = self._feature_builder.get_feature_names()
        X_raw = feature_df[feature_names_raw].dropna()

        # Step 2: Pipeline transform（使用训练期参数）
        X_processed = self._pipeline.transform(X_raw)

        # Step 3: 契约验证
        if validate_contract and self._contract is not None:
            ok, missing = self._contract.validate(list(X_processed.columns))
            if not ok:
                log.warning(
                    "[DataKitchen] 契约验证失败: 缺少特征={} 推理结果可能不稳定",
                    missing,
                )

        # Step 4: 构建视图
        views = self._build_views(X_processed, raw_feature_matrix=X_raw)

        log.debug(
            "[DataKitchen] transform完成: alpha_rows={} alpha_cols={}",
            len(views["alpha_features"]),
            len(views["alpha_features"].columns),
        )
        return views

    # ────────────────────────────────────────────────────────────
    # 契约管理
    # ────────────────────────────────────────────────────────────

    def load_contract(self, path: str) -> None:
        """从磁盘加载 FeatureContract（推理期无需重新 fit）。"""
        self._contract = FeatureContract.load(path)
        self._fitted = True
        log.info(
            "[DataKitchen] 契约加载: version={} signature={} alpha_features={}",
            self._contract.version,
            self._contract.signature,
            len(self._contract.alpha_features),
        )

    @property
    def contract(self) -> FeatureContract | None:
        return self._contract

    # ────────────────────────────────────────────────────────────
    # 视图构建
    # ────────────────────────────────────────────────────────────

    def _build_views(
        self,
        X: pd.DataFrame,
        raw_feature_matrix: pd.DataFrame | None = None,
    ) -> dict[str, pd.DataFrame]:
        """从特征矩阵中，按前缀规则分出三个视图。"""
        cfg = self.config
        all_cols = list(X.columns)
        raw_matrix = raw_feature_matrix if raw_feature_matrix is not None else X
        raw_cols = list(raw_matrix.columns)

        # alpha_features: 全部处理后的特征
        alpha_cols = all_cols

        # regime_features: 使用原始特征矩阵，保留 detector 所需列的可解释性
        regime_cols = [
            c for c in raw_cols
            if any(c.startswith(pfx) for pfx in cfg.regime_feature_prefixes)
        ]
        if not regime_cols:
            # 降级：至少保留 5 列
            regime_cols = raw_cols[:min(5, len(raw_cols))]
            log.warning(
                "[DataKitchen] regime_features前缀匹配为空，降级为前{}列", len(regime_cols),
            )

        missing_regime_core = [
            col for col in _REGIME_CORE_COLUMNS if col not in regime_cols
        ]
        if missing_regime_core:
            log.warning(
                "[DataKitchen] regime_features 缺少 detector 核心列: {}",
                missing_regime_core,
            )

        # diagnostic_features: 使用原始特征矩阵，尽量保留诊断上下文
        diag_cols = [
            c for c in raw_cols
            if any(c.startswith(pfx) for pfx in cfg.diagnostic_feature_prefixes)
        ]
        if not diag_cols:
            diag_cols = raw_cols[:min(5, len(raw_cols))]

        return {
            "alpha_features": X[alpha_cols],
            "regime_features": raw_matrix[regime_cols],
            "diagnostic_features": raw_matrix[diag_cols],
        }

    # ────────────────────────────────────────────────────────────
    # 辅助
    # ────────────────────────────────────────────────────────────

    def _versioned_name(self) -> str:
        now = datetime.now(tz=timezone.utc)
        return f"{self.config.version}_{now.strftime('%Y%m')}"

    def _log_nan_diagnostics(
        self,
        feature_df: pd.DataFrame,
        feature_names: list[str],
    ) -> None:
        """打印特征矩阵 NaN 诊断（[FeatureDiag] 标签）。"""
        nan_ratio = feature_df[feature_names].isna().mean()
        high_nan = nan_ratio[nan_ratio > 0.1]
        if not high_nan.empty:
            log.debug(
                "[FeatureDiag] 高NaN比例列(>10%): {}",
                {c: f"{v:.2%}" for c, v in high_nan.items()},
            )
        log.debug(
            "[FeatureDiag] NaN诊断: 特征总列={} 全NaN列={} 有NaN列={}",
            len(feature_names),
            int((nan_ratio == 1.0).sum()),
            int((nan_ratio > 0).sum()),
        )

    def diagnostics(self) -> dict[str, Any]:
        """返回当前 DataKitchen 状态的诊断快照。"""
        return {
            "version": self.config.version,
            "fitted": self._fitted,
            "contract_version": self._contract.version if self._contract else None,
            "contract_signature": self._contract.signature if self._contract else None,
            "pipeline": self._pipeline.diagnostics(),
        }
