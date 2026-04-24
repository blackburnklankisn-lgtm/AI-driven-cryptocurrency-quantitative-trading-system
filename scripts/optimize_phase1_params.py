"""
scripts/optimize_phase1_params.py — Phase 1 Walk-Forward + Optuna 参数优化脚本

用途：
  对 WalkForwardTrainer 的关键超参数做 Optuna 贝叶斯优化，
  自动搜索最优 buy/sell 阈值和模型参数，输出结构化结果。

运行方式：
  cd AI-driven-cryptocurrency-quantitative-trading-system
    python scripts/optimize_phase1_params.py --symbol BTCUSDT --data ./data/btcusdt_1h.csv --n-trials 50

依赖：
    optuna 已由项目依赖托管；请先同步环境后再运行

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
from typing import Any, Dict

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

def load_data(
    symbol: str,
    data_path: str | None = None,
    *,
    allow_synthetic_data: bool = False,
) -> pd.DataFrame:
    """
    加载 OHLCV 数据。

    优先从 data_path 加载 CSV；CLI 默认要求真实数据。
    仅在显式 allow_synthetic_data=True 时生成合成数据，供开发验证使用。

    CSV 格式要求：timestamp, open, high, low, close, volume（可含其他列）
    """
    if data_path and Path(data_path).exists():
        log.info("[Optuna] 从 {} 加载数据", data_path)
        df = pd.read_csv(data_path, parse_dates=["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        log.info("[Optuna] 数据加载完成: rows={} cols={}", len(df), len(df.columns))
        return df

    if data_path and not Path(data_path).exists():
        raise FileNotFoundError(f"优化数据文件不存在: {data_path}")

    if not allow_synthetic_data:
        raise ValueError(
            "缺少真实 OHLCV 数据。请传入 --data，或仅在开发验证时显式使用 --allow-synthetic-data。"
        )

    log.warning("[Optuna] 使用显式合成数据模式（仅供开发测试）")
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
# 显式调试工具：网格搜索
# ─────────────────────────────────────────────────────────────

def grid_search_fallback(
    df: pd.DataFrame,
    n_splits: int,
    test_size: int,
    output_dir: Path,
) -> dict:
    """简单网格搜索，保留为显式调试工具，不再作为默认 fallback。"""
    log.info("[Optuna] 使用显式网格搜索调试路径")

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


def _normalize_symbol_key(symbol: str) -> str:
    return "".join(ch for ch in symbol.lower() if ch.isalnum())


def _write_runtime_aliases(
    output_dir: Path,
    symbol: str,
    cal_result,
    best_params_info: dict,
) -> tuple[Path, Path]:
    """写入 runtime 可直接消费的稳定别名文件。"""
    symbol_safe = _normalize_symbol_key(symbol)
    threshold_alias = output_dir / f"{symbol_safe}_threshold.json"
    params_alias = output_dir / f"{symbol_safe}_best_params.json"

    cal_result.save(threshold_alias)
    with open(params_alias, "w", encoding="utf-8") as f:
        json.dump(best_params_info, f, indent=2)

    log.info(
        "[Optuna] runtime 工件已更新: symbol={} threshold={} params={}",
        symbol,
        threshold_alias,
        params_alias,
    )
    return threshold_alias, params_alias


def optimize_params_from_dataframe(
    df: pd.DataFrame,
    *,
    symbol: str,
    output_dir: Path,
    n_trials: int = 30,
    n_splits: int = 4,
    test_size: int = 150,
    calibration_strategy: str = "median",
) -> Dict[str, Any]:
    """用给定的真实/离线 OHLCV DataFrame 执行完整参数优化并写出 runtime 工件。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    diag = MLDiagnostics()

    log.info(
        "[Optuna] 优化启动: symbol={} n_trials={} n_splits={} test_size={} rows={}",
        symbol,
        n_trials,
        n_splits,
        test_size,
        len(df),
    )

    best_result = None
    best_params_info: Dict[str, Any] = {}

    try:
        import optuna
    except ImportError as exc:
        raise RuntimeError(
            "optuna 未安装，但该能力现已是项目托管依赖。请先同步项目依赖后再运行优化。"
        ) from exc

    optuna.logging.set_verbosity(optuna.logging.WARNING)

    study = optuna.create_study(direction="maximize")
    study.optimize(
        lambda trial: objective(trial, df, n_splits, test_size),
        n_trials=n_trials,
        catch=(Exception,),
    )

    best_trial = study.best_trial
    log.info(
        "[Optuna] 优化完成: best_value={:.4f} best_params={}",
        best_trial.value,
        best_trial.params,
    )

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
    best_result = trainer.train(df, n_splits=n_splits, test_size=test_size)
    best_params_info = {
        "source": "optuna",
        "best_value": best_trial.value,
        "params": best_trial.params,
    }

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
    with open(study_path, "w", encoding="utf-8") as f:
        json.dump(study_summary, f, indent=2)
    log.info("[Optuna] Study 结果已保存: {}", study_path)

    if best_result is None:
        raise RuntimeError("参数优化失败，无有效结果")

    diag.report_walk_forward(best_result, tag=symbol)

    calibrator = ThresholdCalibrator(aggregation_strategy=calibration_strategy)
    cal_result = calibrator.calibrate_from_wf_result(best_result)
    diag.report_calibration(cal_result, tag=symbol)

    threshold_path = output_dir / f"threshold_{timestamp}.json"
    cal_result.save(threshold_path)

    params_path = output_dir / f"best_params_{timestamp}.json"
    with open(params_path, "w", encoding="utf-8") as f:
        json.dump(best_params_info, f, indent=2)
    log.info("[Optuna] 最优参数已保存: {}", params_path)

    runtime_threshold_path, runtime_params_path = _write_runtime_aliases(
        output_dir,
        symbol,
        cal_result,
        best_params_info,
    )

    diag_path = output_dir / f"ml_diag_{timestamp}.json"
    diag.save_report(diag_path)

    avg_metrics = best_result.avg_metrics()
    return {
        "symbol": symbol,
        "timestamp": timestamp,
        "best_params_info": best_params_info,
        "avg_metrics": avg_metrics,
        "recommended_buy_threshold": cal_result.recommended_buy_threshold,
        "recommended_sell_threshold": cal_result.recommended_sell_threshold,
        "aggregation_strategy": cal_result.aggregation_strategy,
        "threshold_path": threshold_path,
        "params_path": params_path,
        "runtime_threshold_path": runtime_threshold_path,
        "runtime_params_path": runtime_params_path,
        "diag_path": diag_path,
    }


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 1 参数优化脚本")
    parser.add_argument("--symbol", default="BTCUSDT", help="交易对（用于日志标识）")
    parser.add_argument("--data", default=None, help="OHLCV CSV 文件路径")
    parser.add_argument(
        "--allow-synthetic-data",
        action="store_true",
        help="仅开发验证时允许在缺少真实数据时生成合成 OHLCV 数据",
    )
    parser.add_argument("--n-trials", type=int, default=30, help="Optuna trial 数量")
    parser.add_argument("--n-splits", type=int, default=4, help="Walk-Forward 折数")
    parser.add_argument("--test-size", type=int, default=150, help="每折测试集大小")
    parser.add_argument("--output-dir", default="./models", help="输出目录")
    parser.add_argument("--calibration-strategy", default="median",
                        choices=["mean", "median", "conservative"],
                        help="阈值聚合策略")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    try:
        df = load_data(
            args.symbol,
            args.data,
            allow_synthetic_data=args.allow_synthetic_data,
        )
        result = optimize_params_from_dataframe(
            df,
            symbol=args.symbol,
            output_dir=output_dir,
            n_trials=args.n_trials,
            n_splits=args.n_splits,
            test_size=args.test_size,
            calibration_strategy=args.calibration_strategy,
        )
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        log.error("[Optuna] {}", exc)
        sys.exit(1)

    print("\n" + "=" * 60)
    print(f"  Phase 1 优化结果摘要 [{args.symbol}]")
    print("=" * 60)
    avg = result["avg_metrics"]
    print(f"  OOS Accuracy:     {avg.get('accuracy', 0):.4f}")
    print(f"  OOS F1:           {avg.get('f1', 0):.4f}")
    print(f"  Recommended Buy:  {result['recommended_buy_threshold']:.4f}")
    print(f"  Recommended Sell: {result['recommended_sell_threshold']:.4f}")
    print(f"  Strategy:         {result['aggregation_strategy']}")
    print(f"\n  阈值文件:  {result['threshold_path']}")
    print(f"  参数文件:  {result['params_path']}")
    print(f"  Runtime阈值: {result['runtime_threshold_path']}")
    print(f"  Runtime参数: {result['runtime_params_path']}")
    print(f"  诊断报告:  {result['diag_path']}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
