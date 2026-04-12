"""
tests/test_ml_alpha.py — ML Alpha 层单元测试

覆盖项：
- MLFeatureBuilder: 输出维度、NaN 处理、防未来函数
- ReturnLabeler: 连续标签、分类标签、二分类标签
- ReturnLabeler: 最后 N 行强制 NaN
- ReturnLabeler: check_no_leak 正确检测隔离期不足
- SignalModel: fit + predict 流程（RandomForest，不依赖 lgbm）
- SignalModel: save/load pickle 往返
- WalkForwardTrainer: 完整流程（小数据量快速验证）
- WalkForwardTrainer: 各折时序正确（测试集不早于训练集）
- MLPredictor: 预热期不产生信号
- MLPredictor: 买入概率超阈值时产出买单
"""

from __future__ import annotations

import pickle
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd
import pytest

from core.event import EventType, KlineEvent
from core.exceptions import FutureLookAheadError
from modules.alpha.ml.feature_builder import FeatureConfig, MLFeatureBuilder
from modules.alpha.ml.labeler import ReturnLabeler
from modules.alpha.ml.model import SignalModel
from modules.alpha.ml.predictor import MLPredictor, PredictorConfig
from modules.alpha.ml.trainer import WalkForwardTrainer


# ─────────────────────────────────────────────────────────────
# 测试数据生成
# ─────────────────────────────────────────────────────────────

def make_ohlcv(n: int = 500, seed: int = 0) -> pd.DataFrame:
    """生成 n 条带趋势和噪声的合成 OHLCV 数据。"""
    rng = np.random.RandomState(seed)
    ts = pd.date_range("2023-01-01", periods=n, freq="1H", tz="UTC")
    prices = 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n)))
    noise = rng.uniform(0.005, 0.015, n)

    return pd.DataFrame({
        "timestamp": ts,
        "symbol":    "BTC/USDT",
        "open":      prices * (1 - noise / 4),
        "high":      prices * (1 + noise / 2),
        "low":       prices * (1 - noise / 2),
        "close":     prices,
        "volume":    rng.uniform(100, 500, n),
    })


def make_kline_event(close: float, ts_offset: int = 0) -> KlineEvent:
    return KlineEvent(
        event_type=EventType.KLINE_UPDATED,
        timestamp=datetime(2024, 1, 1, ts_offset % 24, tzinfo=timezone.utc),
        source="test",
        symbol="BTC/USDT",
        timeframe="1h",
        open=Decimal(str(close * 0.999)),
        high=Decimal(str(close * 1.01)),
        low=Decimal(str(close * 0.99)),
        close=Decimal(str(close)),
        volume=Decimal("1000"),
        is_closed=True,
    )


# ─────────────────────────────────────────────────────────────
# MLFeatureBuilder 测试
# ─────────────────────────────────────────────────────────────

class TestMLFeatureBuilder:
    @pytest.fixture
    def df(self) -> pd.DataFrame:
        return make_ohlcv(200)

    @pytest.fixture
    def builder(self) -> MLFeatureBuilder:
        cfg = FeatureConfig(
            sma_windows=[10, 20],
            ema_spans=[12],
            rsi_window=14,
            atr_window=14,
            bb_window=20,
            lag_periods=[1, 2, 3],
            rolling_windows=[5],
            use_time_features=False,
        )
        return MLFeatureBuilder(cfg)

    def test_build_returns_dataframe(self, builder: MLFeatureBuilder, df: pd.DataFrame) -> None:
        """build() 应返回 DataFrame，且行数不变。"""
        result = builder.build(df)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == len(df)

    def test_feature_count_gt_zero(self, builder: MLFeatureBuilder, df: pd.DataFrame) -> None:
        """应产生大量特征列（至少 20 个）。"""
        builder.build(df)
        assert len(builder.get_feature_names()) >= 20

    def test_no_negative_shift(self, builder: MLFeatureBuilder) -> None:
        """验证特征列无负向偏移（防未来函数）：修改末尾数据不影响前面的特征值。"""
        df_orig = make_ohlcv(100)
        result_orig = builder.build(df_orig).copy()

        df_modified = df_orig.copy()
        df_modified.loc[df_modified.index[-10:], "close"] = 999999.0
        result_mod = builder.build(df_modified)

        feature_names = builder.get_feature_names()
        # 前 70 行（远离尾部）的特征不应受末尾数据修改影响
        for col in feature_names[:5]:  # 检查前 5 个特征
            if col in result_orig.columns and col in result_mod.columns:
                orig_head = result_orig[col].iloc[:70]
                mod_head = result_mod[col].iloc[:70]
                pd.testing.assert_series_equal(orig_head, mod_head, check_names=False)

    def test_missing_column_raises(self, builder: MLFeatureBuilder) -> None:
        """缺少必要列时应抛出 ValueError。"""
        df = make_ohlcv(100).drop(columns=["volume"])
        with pytest.raises(ValueError, match="缺少必要列"):
            builder.build(df)

    def test_get_feature_names_before_build_empty(self) -> None:
        """build() 前调用 get_feature_names() 应返回空列表。"""
        builder = MLFeatureBuilder()
        assert builder.get_feature_names() == []

    def test_dropna_reduces_rows(self, builder: MLFeatureBuilder, df: pd.DataFrame) -> None:
        """get_feature_matrix() 应删除 NaN 行，输出行数 <= 输入行数。"""
        X = builder.get_feature_matrix(df)
        assert len(X) < len(df)
        assert not X.isnull().any().any()


# ─────────────────────────────────────────────────────────────
# ReturnLabeler 测试
# ─────────────────────────────────────────────────────────────

class TestReturnLabeler:
    @pytest.fixture
    def df(self) -> pd.DataFrame:
        return make_ohlcv(200)

    @pytest.fixture
    def labeler(self) -> ReturnLabeler:
        return ReturnLabeler(forward_bars=5, return_threshold=0.005)

    def test_continuous_label_has_nan_at_end(
        self, labeler: ReturnLabeler, df: pd.DataFrame
    ) -> None:
        """连续标签最后 N 行必须为 NaN。"""
        label = labeler.label_continuous(df)
        assert label.iloc[-5:].isna().all()

    def test_binary_label_only_0_and_1(
        self, labeler: ReturnLabeler, df: pd.DataFrame
    ) -> None:
        """二分类标签只应含 0.0、1.0 和 NaN。"""
        label = labeler.label_binary(df)
        valid = label.dropna()
        assert set(valid.unique()).issubset({0.0, 1.0})

    def test_classification_label_only_neg1_0_1(
        self, labeler: ReturnLabeler, df: pd.DataFrame
    ) -> None:
        """三分类标签只应含 -1、0、1 和 NaN。"""
        label = labeler.label_classification(df)
        valid = label.dropna()
        assert set(valid.unique()).issubset({-1.0, 0.0, 1.0})

    def test_label_length_equals_input(
        self, labeler: ReturnLabeler, df: pd.DataFrame
    ) -> None:
        """标签序列长度应与输入 DataFrame 相同。"""
        label = labeler.label_continuous(df)
        assert len(label) == len(df)

    def test_check_no_leak_passes_valid_split(
        self, labeler: ReturnLabeler
    ) -> None:
        """时序正确切分时不应抛出异常。"""
        train_idx = pd.RangeIndex(0, 100)
        test_idx = pd.RangeIndex(110, 150)  # 有 10 行 embargo
        labeler.check_no_leak(train_idx, test_idx, embargo_bars=5)

    def test_check_no_leak_raises_on_overlap(
        self, labeler: ReturnLabeler
    ) -> None:
        """训练集和测试集有重叠时应抛出 FutureLookAheadError。"""
        train_idx = pd.RangeIndex(0, 100)
        test_idx = pd.RangeIndex(95, 150)  # 有重叠
        with pytest.raises(FutureLookAheadError):
            labeler.check_no_leak(train_idx, test_idx)

    def test_check_no_leak_raises_insufficient_embargo(
        self, labeler: ReturnLabeler
    ) -> None:
        """隔离期不足 forward_bars 时应抛出 FutureLookAheadError。"""
        train_idx = pd.RangeIndex(0, 100)
        test_idx = pd.RangeIndex(102, 150)  # 只有 2 行 gap，但 forward_bars=5
        with pytest.raises(FutureLookAheadError, match="隔离期不足"):
            labeler.check_no_leak(train_idx, test_idx)

    def test_invalid_forward_bars_raises(self) -> None:
        """forward_bars <= 0 时初始化应失败。"""
        with pytest.raises(ValueError, match="forward_bars"):
            ReturnLabeler(forward_bars=0)

    def test_class_weights_all_classes_covered(
        self, labeler: ReturnLabeler, df: pd.DataFrame
    ) -> None:
        """class_weights 应覆盖所有出现的类别。"""
        labels = labeler.label_binary(df)
        weights = labeler.compute_class_weights(labels)
        for cls in labels.dropna().unique():
            assert cls in weights


# ─────────────────────────────────────────────────────────────
# SignalModel 测试
# ─────────────────────────────────────────────────────────────

class TestSignalModel:
    @pytest.fixture
    def xy(self) -> tuple:
        """生成简单的训练数据集。"""
        rng = np.random.RandomState(42)
        n = 200
        X = pd.DataFrame(
            rng.randn(n, 10),
            columns=[f"feat_{i}" for i in range(10)]
        )
        y = pd.Series(rng.choice([0, 1], n))
        return X, y

    def test_rf_fit_and_predict(self, xy: tuple) -> None:
        """RandomForest 模型应能正常训练和预测。"""
        X, y = xy
        model = SignalModel(model_type="rf")
        model.fit(X[:150], y[:150])
        preds = model.predict(X[150:])
        assert len(preds) == 50
        assert set(np.unique(preds)).issubset({0, 1})

    def test_lr_fit_and_predict(self, xy: tuple) -> None:
        """LogisticRegression 模型应能正常训练和预测。"""
        X, y = xy
        model = SignalModel(model_type="lr")
        model.fit(X[:150], y[:150])
        preds = model.predict(X[150:])
        assert len(preds) == 50

    def test_predict_proba_shape(self, xy: tuple) -> None:
        """predict_proba 输出形状应为 (n_samples, n_classes)。"""
        X, y = xy
        model = SignalModel(model_type="rf")
        model.fit(X[:150], y[:150])
        proba = model.predict_proba(X[150:])
        assert proba.shape == (50, 2)

    def test_predict_before_fit_raises(self, xy: tuple) -> None:
        """未训练时调用 predict 应抛出 RuntimeError。"""
        X, _ = xy
        model = SignalModel(model_type="rf")
        with pytest.raises(RuntimeError, match="尚未训练"):
            model.predict(X)

    def test_feature_importance_returns_series(self, xy: tuple) -> None:
        """feature_importance 应返回 Series（降序）。"""
        X, y = xy
        model = SignalModel(model_type="rf")
        model.fit(X, y)
        imp = model.get_feature_importance()
        assert isinstance(imp, pd.Series)
        assert len(imp) == 10
        assert imp.is_monotonic_decreasing

    def test_save_and_load_roundtrip(self, xy: tuple, tmp_path: Path) -> None:
        """保存后加载的模型应产出相同的预测结果。"""
        X, y = xy
        model = SignalModel(model_type="rf")
        model.fit(X[:150], y[:150])

        preds_before = model.predict(X[150:])

        path = tmp_path / "test_model.pkl"
        model.save(path)

        loaded = SignalModel.load(path)
        preds_after = loaded.predict(X[150:])

        np.testing.assert_array_equal(preds_before, preds_after)

    def test_feature_mismatch_raises(self, xy: tuple) -> None:
        """推理时缺少训练特征应抛出 ValueError。"""
        X, y = xy
        model = SignalModel(model_type="rf")
        model.fit(X[:150], y[:150])

        X_wrong = X.drop(columns=["feat_0"])
        with pytest.raises(ValueError, match="缺少"):
            model.predict(X_wrong[150:])

    def test_invalid_model_type_raises(self) -> None:
        """无效 model_type 应抛出 ValueError。"""
        with pytest.raises(ValueError, match="lgbm/rf/lr"):
            SignalModel(model_type="xgboost_plus")


# ─────────────────────────────────────────────────────────────
# WalkForwardTrainer 测试
# ─────────────────────────────────────────────────────────────

class TestWalkForwardTrainer:
    @pytest.fixture
    def df(self) -> pd.DataFrame:
        return make_ohlcv(800, seed=1)

    @pytest.fixture
    def trainer(self) -> WalkForwardTrainer:
        cfg = FeatureConfig(
            sma_windows=[10, 20],
            lag_periods=[1, 2],
            rolling_windows=[5],
        )
        return WalkForwardTrainer(
            feature_builder=MLFeatureBuilder(cfg),
            labeler=ReturnLabeler(forward_bars=3, return_threshold=0.005),
            model_type="rf",
            model_params={"n_estimators": 30, "random_state": 42},
        )

    def test_walk_forward_produces_results(
        self, trainer: WalkForwardTrainer, df: pd.DataFrame
    ) -> None:
        """Walk-Forward 训练应产出含多折结果的 WalkForwardResult。"""
        result = trainer.train(
            df, n_splits=3, test_size=80, min_train_size=150, val_size=0
        )
        assert len(result.fold_results) > 0
        assert result.final_model is not None
        assert result.oos_predictions is not None

    def test_test_always_after_train(
        self, trainer: WalkForwardTrainer, df: pd.DataFrame
    ) -> None:
        """每折的测试集起始时间必须在训练集结束时间之后。"""
        result = trainer.train(
            df, n_splits=4, test_size=60, min_train_size=150, val_size=0
        )
        for fold in result.fold_results:
            assert fold.train_end < fold.test_start, (
                f"Fold {fold.fold_id}: train_end={fold.train_end} >= test_start={fold.test_start}"
            )

    def test_all_metrics_in_result(
        self, trainer: WalkForwardTrainer, df: pd.DataFrame
    ) -> None:
        """每折结果应包含所有必需指标。"""
        result = trainer.train(
            df, n_splits=2, test_size=80, min_train_size=150, val_size=0
        )
        required_fields = {"accuracy", "f1", "precision", "recall", "auc"}
        for fold in result.fold_results:
            for field in required_fields:
                assert hasattr(fold, field), f"缺少指标: {field}"

    def test_insufficient_data_raises(
        self, trainer: WalkForwardTrainer
    ) -> None:
        """数据量不足时应抛出 ValueError。"""
        small_df = make_ohlcv(100)
        with pytest.raises(ValueError, match="有效样本不足|数据量不足"):
            trainer.train(
                small_df, n_splits=5, test_size=100, min_train_size=300
            )

    def test_feature_importance_available(
        self, trainer: WalkForwardTrainer, df: pd.DataFrame
    ) -> None:
        """Walk-Forward 结果应含多折平均后的特征重要性。"""
        result = trainer.train(
            df, n_splits=2, test_size=80, min_train_size=150, val_size=0
        )
        if result.feature_importance_avg is not None:
            assert len(result.feature_importance_avg) > 0
            assert result.feature_importance_avg.is_monotonic_decreasing


# ─────────────────────────────────────────────────────────────
# MLPredictor 测试
# ─────────────────────────────────────────────────────────────

class TestMLPredictor:
    def _make_predictor(self, buy_threshold: float = 0.99) -> MLPredictor:
        """创建预热后的 MLPredictor（使用 mock model）。"""
        from unittest.mock import MagicMock
        import numpy as np

        # 构建简单的 feature builder + 真实的已训练模型
        cfg = FeatureConfig(sma_windows=[10, 20], lag_periods=[1, 2], rolling_windows=[5])
        builder = MLFeatureBuilder(cfg)

        # 先创建并训练一个真实模型（使用小数据集）
        df = make_ohlcv(500, seed=2)
        labeler = ReturnLabeler(forward_bars=3, return_threshold=0.005)
        trainer = WalkForwardTrainer(
            feature_builder=builder,
            labeler=labeler,
            model_type="rf",
            model_params={"n_estimators": 20, "random_state": 0},
        )
        result = trainer.train(df, n_splits=2, test_size=80, min_train_size=150, val_size=0)
        model = result.final_model

        config = PredictorConfig(
            buy_threshold=buy_threshold,
            sell_threshold=0.3,
            order_qty=0.01,
            cooling_bars=2,
            min_buffer_size=50,   # 测试用小缓冲区
        )
        return MLPredictor(
            model=model,
            symbol="BTC/USDT",
            config=config,
            feature_builder=builder,
            timeframe="1h",
        )

    def test_warmup_period_no_signal(self) -> None:
        """预热期（缓冲区未满）不应发出任何信号。"""
        predictor = self._make_predictor()

        # 喂入少于 min_buffer_size 根 K 线
        count = predictor.cfg.min_buffer_size - 5
        orders = []
        for i in range(count):
            orders.extend(predictor.on_kline(make_kline_event(100.0 + i, i)))

        assert all(len(o) == 0 for o in [orders])
        assert len(orders) == 0

    def test_non_closed_kline_ignored(self) -> None:
        """未收线的 K 线（is_closed=False）不应被处理。"""
        predictor = self._make_predictor()
        event = KlineEvent(
            event_type=EventType.KLINE_UPDATED,
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            source="test",
            symbol="BTC/USDT",
            timeframe="1h",
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("95"),
            close=Decimal("102"),
            volume=Decimal("1000"),
            is_closed=False,   # ← 未收线
        )
        orders = predictor.on_kline(event)
        assert orders == []

    def test_wrong_symbol_ignored(self) -> None:
        """非目标 symbol 的 K 线应被忽略。"""
        predictor = self._make_predictor()
        event = make_kline_event(100.0)
        event = KlineEvent(
            event_type=event.event_type,
            timestamp=event.timestamp,
            source=event.source,
            symbol="ETH/USDT",   # ← 不同 symbol
            timeframe=event.timeframe,
            open=event.open,
            high=event.high,
            low=event.low,
            close=event.close,
            volume=event.volume,
            is_closed=True,
        )
        orders = predictor.on_kline(event)
        assert orders == []

    def test_low_threshold_generates_buy(self) -> None:
        """调低阈值后（buy_threshold=0.0），应在预热后产生买入信号。"""
        predictor = self._make_predictor(buy_threshold=0.0)

        df = make_ohlcv(300, seed=3)
        generated_orders = []
        for i, row in df.iterrows():
            event = KlineEvent(
                event_type=EventType.KLINE_UPDATED,
                timestamp=row["timestamp"].to_pydatetime(),
                source="test",
                symbol="BTC/USDT",
                timeframe="1h",
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=Decimal(str(row["volume"])),
                is_closed=True,
            )
            generated_orders.extend(predictor.on_kline(event))

        # 至少应有一些信号产生（阈值为 0，任何时候都会触发）
        buy_orders = [o for o in generated_orders if o.side == "buy"]
        assert len(buy_orders) >= 1, "阈值为 0 时应产生至少一个买入信号"
