"""modules/alpha/ml/__init__.py"""
from modules.alpha.ml.feature_builder import MLFeatureBuilder, FeatureConfig
from modules.alpha.ml.labeler import ReturnLabeler
from modules.alpha.ml.model import SignalModel
from modules.alpha.ml.trainer import WalkForwardTrainer, WalkForwardResult
from modules.alpha.ml.predictor import MLPredictor, PredictorConfig
from modules.alpha.ml.predictor_v2 import MLPredictor as MLPredictorV2
from modules.alpha.ml.predictor_v2 import PredictorConfig as PredictorConfigV2
from modules.alpha.ml.continuous_learner import ContinuousLearner, ContinuousLearnerConfig
from modules.alpha.ml.feature_contract import FeatureContract
from modules.alpha.ml.feature_pipeline import FeaturePipeline, FeaturePipelineStage
from modules.alpha.ml.feature_selectors import VarianceFilter, Decorrelator, PCAReducer
from modules.alpha.ml.data_kitchen import DataKitchen, DataKitchenConfig, DataKitchenOutput
from modules.alpha.ml.threshold_calibrator import ThresholdCalibrator, CalibrationResult, FoldThreshold
from modules.alpha.ml.model_registry import ModelRegistry, ModelVersion
from modules.alpha.ml.diagnostics import MLDiagnostics
from modules.alpha.ml.ensemble import ModelEnsemble, EnsembleConfig
from modules.alpha.ml.meta_learner import MetaLearner, MetaLearnerConfig
# Phase 2 W14
from modules.alpha.ml.omni_signal_fusion import OmniSignalFusion, OmniSignalFusionConfig
from modules.alpha.ml.meta_learner_v2 import MetaLearnerV2, MetaLearnerV2Config

__all__ = [
    "MLFeatureBuilder",
    "FeatureConfig",
    "ReturnLabeler",
    "SignalModel",
    "WalkForwardTrainer",
    "WalkForwardResult",
    "MLPredictor",
    "MLPredictorV2",
    "PredictorConfig",
    "PredictorConfigV2",
    "ContinuousLearner",
    "ContinuousLearnerConfig",
    "FeatureContract",
    "FeaturePipeline",
    "FeaturePipelineStage",
    "VarianceFilter",
    "Decorrelator",
    "PCAReducer",
    "DataKitchen",
    "DataKitchenConfig",
    "DataKitchenOutput",
    "ThresholdCalibrator",
    "CalibrationResult",
    "FoldThreshold",
    "ModelRegistry",
    "ModelVersion",
    "MLDiagnostics",
    "ModelEnsemble",
    "EnsembleConfig",
    "MetaLearner",
    "MetaLearnerConfig",
    # Phase 2 W14
    "OmniSignalFusion",
    "OmniSignalFusionConfig",
    "MetaLearnerV2",
    "MetaLearnerV2Config",
]
