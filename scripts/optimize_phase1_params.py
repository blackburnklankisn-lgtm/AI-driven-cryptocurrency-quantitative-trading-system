"""
scripts/optimize_phase1_params.py — Phase 1 Walk-Forward + Optuna 参数优化脚本

用途：
  对 WalkForwardTrainer 的关键超参数做 Optuna 贝叶斯优化，
  自动搜索最优 buy/sell 阈值和模型参数，输出结构化结果。

运行方式：
  cd AI-driven-cryptocurrency-quantitative-trading-system
  python scripts/optimize_phase1_params.py --symbol BTCUSDT --n-trials 50

依赖：
  pip install optuna  (可选；无 optuna 时自动降级为网格搜索)

输出：
  models/optuna_study_<timestamp>.json  — Optuna study 结果
  models/threshold_<timestamp>.json     — 最优阈值
  models/best_params_<timestamp>.json   — 最优参数
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# 确保项目根目录在 sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np
import pandas as pd

from core.logger import get_logger
from modules.alpha.ml.diagnostics import MLDiagnostics
from modules.alpha.ml.feature_builder import FeatureConfig, MLFeatureBuilder
from modules.alpha.ml.labeler import ReturnLabeler
from modules.alpha.ml.threshold_calibrator import ThresholdCalibrator
from modules.alpha.ml.trainer import WalkForwardTrainer

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────
# 数据加载（示例：本地 CSV）
# ─────────────────────────────────────────────────────────────

def load_data(symbol: str, data_path: str | None = None) -> pd.DataFrame:
    """
    加载 OHLCV 数据。

    优先从 data_path 加载 CSV，若不存在则生成合成数据（用于开发/测试）。

    CSV 格式要求：timestamp, open, high, low, close, volume（可含其他列）
    """
    if data_path and Path(data_path).exists():
        log.info("[Optuna] 从 {} 加载数据", data_path)
        df = pd.read_csv(data_path, parse_dates=["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        log.info("[Optuna] 数据加载完成: rows={} cols={}", len(df), len(df.columns))
        return df

    log.warning("[Optuna] 数据文件不存在，生成合成数据（仅供开发测试）")
    rng = np.random.RandomState(42)
    n = 2000
    prices = 30000.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, n)))
    noise = rng.uniform(0.005, 0.02, n)
    ts = pd.date_range("2023-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "timestamp": ts,
        "symbol": symbol,
        "open": prices * (1 - noise / 4),
        "high": prices * (1 + noise / 2),
        "low": prices * (1 - noise / 2),
        "close": prices,
        "volume": rng.uniform(100, 500, n),
    })


# ─────────────────────────────────────────────────────────────
# 目标函数
# ─────────────────────────────────────────────────────────────

def objective(trial, df: pd.DataFrame, n_splits: int, test_size: int) -> float:
    """
    Optuna 目标函数：返回 Walk-Forward OOS AUC（最大化）。

    搜索空间：
    - model_type: rf | lgbm（若 lgbm 不可用则仅 rf）
    - n_estimators: 50~400
    - max_depth: 3~10
    - min_samples_leaf: 5~50（rf 专用）
    - buy_threshold: 0.50~0.70（固定阈值搜索，用于验证最优点）
    """
    model_type = trial.suggest_categorical("model_type", ["rf"])
    n_estimators = trial.suggest_int("n_estimators", 50, 300)
    max_depth = trial.suggest_int("max_depth", 3, 10)
    min_samples_leaf = trial.suggest_int("min_samples_leaf", 5, 40)

    try:
        import lightgbm
        model_type = trial.suggest_categorical("model_type_lgbm", ["rf", "lgbm"])
    except ImportError:
        pass

    model_params = {
        "n_estimators": n_estimators,
        "max_depth": max_depth,
        "min_samples_leaf": min_samples_leaf,
        "random_state": 42,
    }

    try:
        trainer = WalkForwardTrainer(
            model_type=model_type,
            model_params=model_params,
            calibrate=True,
            feature_selection=True,
        )
        result = trainer.train(
            df,
            n_splits=n_splits,
            test_size=test_size,
            min_train_size=300,
        )

        avg_auc = float(np.mean([
            r.auc for r in result.fold_results if not np.isnan(r.auc)
        ]))

        log.info(
            "[OptunaTrial] trial={} model={} n_est={} depth={} avg_auc={:.4f}",
            trial.number, model_type, n_estimators, max_depth, avg_auc,
        )
        return avg_auc

    except Exception as e:
        log.warning("[OptunaTrial] trial={} 失败: {}", trial.number, e)
        return 0.0


# ─────────────────────────────────────────────────────────────
# 网格搜索降级（无 optuna 时）
# ─────────────────────────────────────────────────────────────

def grid_search_fallback(
    df: pd.DataFrame,
    n_splits: int,
    test_size: int,
    output_dir: Path,
) -> dict:
    """简单网格搜索，作为无 Optuna 时的降级方案。"""
    log.info("[Optuna] 降级为网格搜索 (Optuna 未安装)")

    param_grid = [
        {"n_estimators": 100, "max_depth": 5},
        {"n_estimators": 200, "max_depth": 7},
        {"n_estimators": 150, "max_depth": 6},
    ]

    best_auc = -1.0
    best_params = param_grid[0]
    best_result = None

    for params in param_grid:
        trainer = WalkForwardTrainer(
            model_type="rf",
            model_params={**params, "random_state": 42},
        )
        try:
            result = trainer.train(df, n_splits=n_splits, test_size=test_size)
            aucs = [r.auc for r in result.fold_results if not np.isnan(r.auc)]
            avg_auc = float(np.mean(aucs)) if aucs else 0.0
            log.info("[GridSearch] params={} avg_auc={:.4f}", params, avg_auc)
            if avg_auc > best_auc:
                best_auc = avg_auc
                best_params = params
                best_result = result
        except Exception as e:
            log.warning("[GridSearch] params={} 失败: {}", params, e)

    return {"best_params": best_params, "best_auc": best_auc, "best_result": best_result}


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 1 参数优化脚本")
    parser.add_argument("--symbol", default="BTCUSDT", help="交易对（用于日志标识）")
    parser.add_argument("--data", default=None, help="OHLCV CSV 文件路径")
    parser.add_argument("--n-trials", type=int, default=30, help="Optuna trial 数量")
    parser.add_argument("--n-splits", type=int, default=4, help="Walk-Forward 折数")
    parser.add_argument("--test-size", type=int, default=150, help="每折测试集大小")
    parser.add_argument("--output-dir", default="./models", help="输出目录")
    parser.add_argument("--calibration-strategy", default="median",
                        choices=["mean", "median", "conservative"],
                        help="阈值聚合策略")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    diag = MLDiagnostics()

    log.info(
        "[Optuna] 优化启动: symbol={} n_trials={} n_splits={} test_size={}",
        args.symbol, args.n_trials, args.n_splits, args.test_size,
    )

    # 加载数据
    df = load_data(args.symbol, args.data)

    # ── 尝试使用 Optuna ────────────────────────────────────────
    best_result = None
    best_params_info = {}

    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)

        study = optuna.create_study(direction="maximize")
        study.optimize(
            lambda trial: objective(trial, df, args.n_splits, args.test_size),
            n_trials=args.n_trials,
            catch=(Exception,),
        )

        best_trial = study.best_trial
        log.info(
            "[Optuna] 优化完成: best_value={:.4f} best_params={}",
            best_trial.value, best_trial.params,
        )

        # 用最优参数训练最终模型
        final_params = {
            "n_estimators": best_trial.params.get("n_estimators", 100),
            "max_depth": best_trial.params.get("max_depth", 5),
            "min_samples_leaf": best_trial.params.get("min_samples_leaf", 10),
            "random_state": 42,
        }
        final_model_type = best_trial.params.get("model_type", "rf")

        trainer = WalkForwardTrainer(
            model_type=final_model_type,
            model_params=final_params,
            calibrate=True,
        )
        best_result = trainer.train(
            df, n_splits=args.n_splits, test_size=args.test_size
        )
        best_params_info = {
            "source": "optuna",
            "best_value": best_trial.value,
            "params": best_trial.params,
        }

        # 保存 Optuna study 摘要
        study_summary = {
            "n_trials": len(study.trials),
            "best_value": best_trial.value,
            "best_params": best_trial.params,
            "trials": [
                {"number": t.number, "value": t.value, "params": t.params}
                for t in study.trials
                if t.value is not None
            ],
        }
        study_path = output_dir / f"optuna_study_{timestamp}.json"
        with open(study_path, "w") as f:
            json.dump(study_summary, f, indent=2)
        log.info("[Optuna] Study 结果已保存: {}", study_path)

    except ImportError:
        log.warning("[Optuna] optuna 未安装，降级为网格搜索")
        fallback = grid_search_fallback(df, args.n_splits, args.test_size, output_dir)
        best_result = fallback.get("best_result")
        best_params_info = {
            "source": "grid_search",
            "best_auc": fallback.get("best_auc"),
            "params": fallback.get("best_params"),
        }

    if best_result is None:
        log.error("[Optuna] 优化失败，无有效结果")
        sys.exit(1)

    # ── Walk-Forward 诊断 ──────────────────────────────────────
    diag.report_walk_forward(best_result, tag=args.symbol)

    # ── 阈值校准 ──────────────────────────────────────────────
    calibrator = ThresholdCalibrator(aggregation_strategy=args.calibration_strategy)
    cal_result = calibrator.calibrate_from_wf_result(best_result)
    diag.report_calibration(cal_result, tag=args.symbol)

    # 保存阈值
    threshold_path = output_dir / f"threshold_{timestamp}.json"
    cal_result.save(threshold_path)

    # 保存最优参数
    params_path = output_dir / f"best_params_{timestamp}.json"
    with open(params_path, "w") as f:
        json.dump(best_params_info, f, indent=2)
    log.info("[Optuna] 最优参数已保存: {}", params_path)

    # 保存诊断报告
    diag_path = output_dir / f"ml_diag_{timestamp}.json"
    diag.save_report(diag_path)

    # ── 结果摘要 ──────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  Phase 1 优化结果摘要 [{args.symbol}]")
    print("=" * 60)
    avg = best_result.avg_metrics()
    print(f"  OOS Accuracy:     {avg.get('accuracy', 0):.4f}")
    print(f"  OOS F1:           {avg.get('f1', 0):.4f}")
    print(f"  Recommended Buy:  {cal_result.recommended_buy_threshold:.4f}")
    print(f"  Recommended Sell: {cal_result.recommended_sell_threshold:.4f}")
    print(f"  Strategy:         {cal_result.aggregation_strategy}")
    print(f"\n  阈值文件:  {threshold_path}")
    print(f"  参数文件:  {params_path}")
    print(f"  诊断报告:  {diag_path}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
