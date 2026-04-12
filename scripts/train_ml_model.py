"""
scripts/train_ml_model.py — ML 模型训练演示

演示完整的 Walk-Forward 训练流程：
1. 加载合成历史数据（或真实 Parquet 数据）
2. 构建特征矩阵（MLFeatureBuilder）
3. 生成前向收益率标签（ReturnLabeler）
4. 执行 5 折 Walk-Forward 验证训练
5. 打印各折 OOS 指标
6. 保存最终模型供实盘推理使用

运行方式：
    python scripts/train_ml_model.py

输出：
    - 控制台：各折指标、平均 OOS 精度、Top-10 特征重要性
    - models/btcusdt_rf_model.pkl（可供 MLPredictor 直接加载）
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.logger import setup_logging
from modules.alpha.ml.feature_builder import FeatureConfig, MLFeatureBuilder
from modules.alpha.ml.labeler import ReturnLabeler
from modules.alpha.ml.trainer import WalkForwardTrainer


def generate_synthetic_data(n: int = 2000, seed: int = 42) -> pd.DataFrame:
    """生成带趋势和噪声的合成价格数据（与回测演示一致）。"""
    random.seed(seed)
    np.random.seed(seed)

    timestamps = pd.date_range(start="2022-01-01", periods=n, freq="1h", tz="UTC")
    price = 25000.0
    records = []

    for i, ts in enumerate(timestamps):
        sinusoid = math.sin(i * 2 * math.pi / 168) * 0.005
        change = 0.0003 + sinusoid + np.random.normal(0, 0.005)
        price = max(price * (1 + change), 1.0)

        noise = random.uniform(0.005, 0.02)
        records.append({
            "timestamp": ts,
            "symbol": "BTC/USDT",
            "open":   price * (1 - noise / 4),
            "high":   price * (1 + noise / 2),
            "low":    price * (1 - noise / 2),
            "close":  price,
            "volume": random.uniform(50, 300),
        })

    return pd.DataFrame(records)


def main() -> None:
    setup_logging(log_level="INFO")

    print("\n" + "=" * 60)
    print("  AI 驱动加密货币量化交易系统 — ML 模型训练演示")
    print("=" * 60)

    # ── 1. 准备数据 ──────────────────────────────────────────────
    print("\n[1/5] 生成合成历史数据（2000 根 1h K 线）...")
    df = generate_synthetic_data(n=2000)
    print(f"      数据范围: {df['timestamp'].iloc[0]} ~ {df['timestamp'].iloc[-1]}")

    # ── 2. 配置特征构建器 ─────────────────────────────────────────
    print("\n[2/5] 配置特征工程...")
    feature_config = FeatureConfig(
        sma_windows=[10, 20, 50],
        ema_spans=[12, 26],
        rsi_window=14,
        atr_window=14,
        bb_window=20,
        lag_periods=[1, 2, 3, 5],
        rolling_windows=[5, 10],
        use_time_features=False,  # 合成数据无真实季节性，关闭
    )
    feature_builder = MLFeatureBuilder(feature_config)

    # 先 build 一次确认特征数量
    feat_preview = feature_builder.build(df)
    feature_names = feature_builder.get_feature_names()
    print(f"      特征矩阵: {len(feature_names)} 个特征列")

    # ── 3. 配置标签生成器 ─────────────────────────────────────────
    print("\n[3/5] 配置标签生成器...")
    labeler = ReturnLabeler(
        forward_bars=5,          # 未来 5 根 K 线的持有收益
        return_threshold=0.005,  # 0.5% 阈值（加密市场低阈值）
        use_log_return=True,
    )

    # 检查标签分布
    labels = labeler.label_binary(df)
    valid_labels = labels.dropna()
    buy_rate = (valid_labels == 1).mean()
    print(f"      标签分布: 买入={buy_rate:.1%} 非买入={1-buy_rate:.1%}")
    print(f"      有效样本: {len(valid_labels)}/{len(labels)}")

    # ── 4. Walk-Forward 训练 ──────────────────────────────────────
    print("\n[4/5] 执行 Walk-Forward 验证训练（5 折）...")
    trainer = WalkForwardTrainer(
        feature_builder=feature_builder,
        labeler=labeler,
        model_type="rf",   # 用 RandomForest（无需 lgbm）
        model_params={
            "n_estimators": 100,
            "max_depth": 6,
            "min_samples_leaf": 20,
            "random_state": 42,
        },
        expanding=True,
    )

    result = trainer.train(
        df=df,
        n_splits=5,
        test_size=150,
        min_train_size=300,
        val_size=0,      # RF 不需要 early stopping 验证集
        label_type="binary",
    )

    # ── 5. 输出报告 ──────────────────────────────────────────────
    print("\n[5/5] 训练结果:")
    print("\n各折 OOS 指标:")
    print(result.summary().to_string(index=False))

    avg = result.avg_metrics()
    print(f"\n平均 OOS: accuracy={avg['accuracy']:.3f}  f1={avg['f1']:.3f}")
    print(f"           precision={avg['precision']:.3f}  recall={avg['recall']:.3f}")

    if result.feature_importance_avg is not None:
        print("\nTop-10 特征重要性（多折平均）:")
        top10 = result.feature_importance_avg.head(10)
        for feat, imp in top10.items():
            print(f"  {feat:<35} {imp:.4f}")

    # ── 6. 保存模型 ──────────────────────────────────────────────
    if result.final_model is not None:
        model_dir = Path("./models")
        model_dir.mkdir(parents=True, exist_ok=True)
        model_path = model_dir / "btcusdt_rf_model.pkl"
        result.final_model.save(model_path)
        print(f"\n✅ 模型已保存: {model_path}")
        print("   实盘使用方式:")
        print("   >>> from modules.alpha.ml.predictor import MLStrategy")
        print(f"   >>> strategy = MLStrategy.from_model_path('{model_path}', 'BTC/USDT')")
    else:
        print("\n⚠️ 无可用折结果（数据量可能不足）")

    print("\n" + "=" * 60 + "\n")


if __name__ == "__main__":
    main()
