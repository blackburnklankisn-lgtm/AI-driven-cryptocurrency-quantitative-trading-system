"""
tests/test_optimize_phase1_params_full.py — optimize_phase1_params.py 完整测试

覆盖项：
1. load_data: 合成数据模式、CSV 文件加载、文件不存在报错、无数据无合成标志报错
2. _normalize_symbol_key: 清理 symbol 为 key
3. _write_runtime_aliases: 写出 threshold + params 别名文件
4. objective: Optuna trial 目标函数执行 + 失败降级
5. grid_search_fallback: 网格搜索执行
6. optimize_params_from_dataframe: 完整优化流程（低 n_trials）
7. main(): 通过 argparse 调用，含错误路径
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# 确保脚本根目录在 sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.optimize_phase1_params import (
    _normalize_symbol_key,
    _write_runtime_aliases,
    grid_search_fallback,
    load_data,
    objective,
    optimize_params_from_dataframe,
)


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

def _make_synthetic_df(n: int = 600) -> pd.DataFrame:
    """生成可用于优化流程的合成 OHLCV DataFrame。"""
    return load_data("BTCUSDT", allow_synthetic_data=True)


def _make_cal_result_mock(output_dir: Path) -> Any:
    """模拟 ThresholdCalibrator 返回的 CalibrationResult。"""
    mock = MagicMock()
    mock.recommended_buy_threshold = 0.60
    mock.recommended_sell_threshold = 0.40
    mock.aggregation_strategy = "median"
    # save() writes a JSON file
    def _save(path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"buy": 0.60, "sell": 0.40}',
            encoding="utf-8",
        )
    mock.save.side_effect = _save
    return mock


# ══════════════════════════════════════════════════════════════
# 1. load_data
# ══════════════════════════════════════════════════════════════

class TestLoadData:

    def test_synthetic_data_returns_dataframe(self):
        df = load_data("BTCUSDT", allow_synthetic_data=True)
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0
        assert "timestamp" in df.columns
        assert "close" in df.columns

    def test_synthetic_data_has_required_columns(self):
        df = load_data("ETHUSDT", allow_synthetic_data=True)
        for col in ("timestamp", "open", "high", "low", "close", "volume"):
            assert col in df.columns, f"Missing column: {col}"

    def test_synthetic_data_2000_rows(self):
        df = load_data("BTCUSDT", allow_synthetic_data=True)
        assert len(df) == 2000

    def test_csv_file_loads_correctly(self, tmp_path):
        csv_path = tmp_path / "data.csv"
        df = load_data("BTCUSDT", allow_synthetic_data=True)
        df.to_csv(csv_path, index=False)

        loaded = load_data("BTCUSDT", data_path=str(csv_path))
        assert len(loaded) == len(df)
        assert "close" in loaded.columns

    def test_csv_sorted_ascending(self, tmp_path):
        csv_path = tmp_path / "unsorted.csv"
        df = load_data("BTCUSDT", allow_synthetic_data=True)
        # Reverse the order
        df_rev = df.iloc[::-1].copy()
        df_rev.to_csv(csv_path, index=False)

        loaded = load_data("BTCUSDT", data_path=str(csv_path))
        # Should be sorted ascending
        ts = pd.to_datetime(loaded["timestamp"])
        assert ts.is_monotonic_increasing

    def test_nonexistent_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="不存在"):
            load_data("BTCUSDT", data_path=str(tmp_path / "ghost.csv"))

    def test_no_data_no_synthetic_raises_value_error(self):
        with pytest.raises(ValueError, match="真实 OHLCV 数据"):
            load_data("BTCUSDT", data_path=None, allow_synthetic_data=False)

    def test_no_path_no_synthetic_raises_value_error(self):
        with pytest.raises(ValueError):
            load_data("BTCUSDT")  # default allow_synthetic_data=False


# ══════════════════════════════════════════════════════════════
# 2. _normalize_symbol_key
# ══════════════════════════════════════════════════════════════

class TestNormalizeSymbolKey:

    def test_removes_slash(self):
        assert _normalize_symbol_key("BTC/USDT") == "btcusdt"

    def test_removes_hyphen(self):
        assert _normalize_symbol_key("BTC-USDT") == "btcusdt"

    def test_lowercases(self):
        assert _normalize_symbol_key("BTCUSDT") == "btcusdt"

    def test_already_clean(self):
        assert _normalize_symbol_key("btcusdt") == "btcusdt"

    def test_mixed_special_chars(self):
        result = _normalize_symbol_key("SOL_USDT.PERP")
        assert "sol" in result
        assert "usdt" in result
        assert "_" not in result
        assert "." not in result


# ══════════════════════════════════════════════════════════════
# 3. _write_runtime_aliases
# ══════════════════════════════════════════════════════════════

class TestWriteRuntimeAliases:

    def test_creates_threshold_alias_file(self, tmp_path):
        cal = _make_cal_result_mock(tmp_path)
        best_params_info = {"source": "test", "params": {"n_estimators": 100}}
        _write_runtime_aliases(tmp_path, "BTCUSDT", cal, best_params_info)
        alias = tmp_path / "btcusdt_threshold.json"
        assert alias.exists()

    def test_creates_params_alias_file(self, tmp_path):
        cal = _make_cal_result_mock(tmp_path)
        best_params_info = {"source": "test", "params": {"n_estimators": 100}}
        _write_runtime_aliases(tmp_path, "BTCUSDT", cal, best_params_info)
        alias = tmp_path / "btcusdt_best_params.json"
        assert alias.exists()
        with alias.open() as f:
            data = json.load(f)
        assert data["source"] == "test"

    def test_returns_both_paths(self, tmp_path):
        cal = _make_cal_result_mock(tmp_path)
        t_path, p_path = _write_runtime_aliases(tmp_path, "BTCUSDT", cal, {})
        assert Path(t_path).exists()
        assert Path(p_path).exists()

    def test_symbol_with_slash_normalized(self, tmp_path):
        cal = _make_cal_result_mock(tmp_path)
        _write_runtime_aliases(tmp_path, "BTC/USDT", cal, {})
        alias = tmp_path / "btcusdt_threshold.json"
        assert alias.exists()


# ══════════════════════════════════════════════════════════════
# 4. objective (Optuna 目标函数)
# ══════════════════════════════════════════════════════════════

class TestObjective:

    def test_objective_returns_float(self, tmp_path):
        df = _make_synthetic_df()
        trial = MagicMock()
        trial.number = 0
        trial.suggest_categorical.return_value = "rf"
        trial.suggest_int.side_effect = [50, 3, 5]  # n_estimators, max_depth, min_samples_leaf

        result = objective(trial, df, n_splits=2, test_size=100)
        assert isinstance(result, float)
        assert 0.0 <= result <= 1.0

    def test_objective_returns_zero_on_exception(self):
        """目标函数内部失败时应返回 0.0（不让 Optuna 崩溃）。"""
        trial = MagicMock()
        trial.number = 0
        trial.suggest_categorical.return_value = "rf"
        trial.suggest_int.side_effect = [1, 1, 1]  # invalid params

        # Pass an empty df to trigger failure
        result = objective(trial, pd.DataFrame(), n_splits=2, test_size=100)
        assert result == 0.0


# ══════════════════════════════════════════════════════════════
# 5. grid_search_fallback
# ══════════════════════════════════════════════════════════════

class TestGridSearchFallback:

    def test_grid_search_returns_best_params(self, tmp_path):
        df = _make_synthetic_df()
        result = grid_search_fallback(df, n_splits=2, test_size=100, output_dir=tmp_path)
        assert "best_params" in result
        assert "best_auc" in result
        assert isinstance(result["best_auc"], float)

    def test_grid_search_best_auc_between_0_and_1(self, tmp_path):
        df = _make_synthetic_df()
        result = grid_search_fallback(df, n_splits=2, test_size=100, output_dir=tmp_path)
        assert 0.0 <= result["best_auc"] <= 1.0


# ══════════════════════════════════════════════════════════════
# 6. optimize_params_from_dataframe
# ══════════════════════════════════════════════════════════════

class TestOptimizeParamsFromDataframe:

    def test_optimize_returns_required_keys(self, tmp_path):
        df = _make_synthetic_df()
        result = optimize_params_from_dataframe(
            df,
            symbol="BTCUSDT",
            output_dir=tmp_path,
            n_trials=2,
            n_splits=2,
            test_size=100,
        )
        required_keys = [
            "symbol", "timestamp", "best_params_info", "avg_metrics",
            "recommended_buy_threshold", "recommended_sell_threshold",
            "aggregation_strategy", "threshold_path", "params_path",
            "runtime_threshold_path", "runtime_params_path", "diag_path",
        ]
        for key in required_keys:
            assert key in result, f"Missing key: {key}"

    def test_optimize_creates_artifact_files(self, tmp_path):
        df = _make_synthetic_df()
        result = optimize_params_from_dataframe(
            df,
            symbol="BTCUSDT",
            output_dir=tmp_path,
            n_trials=2,
            n_splits=2,
            test_size=100,
        )
        assert Path(result["threshold_path"]).exists()
        assert Path(result["params_path"]).exists()
        assert Path(result["runtime_threshold_path"]).exists()
        assert Path(result["runtime_params_path"]).exists()

    def test_optimize_creates_study_json(self, tmp_path):
        df = _make_synthetic_df()
        optimize_params_from_dataframe(
            df,
            symbol="BTCUSDT",
            output_dir=tmp_path,
            n_trials=2,
            n_splits=2,
            test_size=100,
        )
        study_files = list(tmp_path.glob("optuna_study_*.json"))
        assert len(study_files) == 1
        with study_files[0].open() as f:
            study_data = json.load(f)
        assert "best_value" in study_data
        assert "n_trials" in study_data

    def test_optimize_creates_runtime_alias_files(self, tmp_path):
        df = _make_synthetic_df()
        optimize_params_from_dataframe(
            df,
            symbol="BTCUSDT",
            output_dir=tmp_path,
            n_trials=2,
            n_splits=2,
            test_size=100,
        )
        assert (tmp_path / "btcusdt_threshold.json").exists()
        assert (tmp_path / "btcusdt_best_params.json").exists()

    def test_optimize_thresholds_in_valid_range(self, tmp_path):
        df = _make_synthetic_df()
        result = optimize_params_from_dataframe(
            df,
            symbol="BTCUSDT",
            output_dir=tmp_path,
            n_trials=2,
            n_splits=2,
            test_size=100,
        )
        assert 0.0 <= result["recommended_buy_threshold"] <= 1.0
        assert 0.0 <= result["recommended_sell_threshold"] <= 1.0

    def test_optimize_best_params_info_has_source(self, tmp_path):
        df = _make_synthetic_df()
        result = optimize_params_from_dataframe(
            df,
            symbol="BTCUSDT",
            output_dir=tmp_path,
            n_trials=2,
            n_splits=2,
            test_size=100,
        )
        assert "source" in result["best_params_info"]
        assert result["best_params_info"]["source"] == "optuna"

    def test_optimize_creates_output_dir_if_not_exists(self, tmp_path):
        df = _make_synthetic_df()
        new_output_dir = tmp_path / "new" / "deep" / "output"
        assert not new_output_dir.exists()
        optimize_params_from_dataframe(
            df,
            symbol="BTCUSDT",
            output_dir=new_output_dir,
            n_trials=1,
            n_splits=2,
            test_size=80,
        )
        assert new_output_dir.exists()

    def test_optimize_diag_path_exists(self, tmp_path):
        df = _make_synthetic_df()
        result = optimize_params_from_dataframe(
            df,
            symbol="BTCUSDT",
            output_dir=tmp_path,
            n_trials=2,
            n_splits=2,
            test_size=100,
        )
        assert Path(result["diag_path"]).exists()

    def test_optimize_symbol_preserved_in_result(self, tmp_path):
        df = _make_synthetic_df()
        result = optimize_params_from_dataframe(
            df,
            symbol="ETHUSDT",
            output_dir=tmp_path,
            n_trials=1,
            n_splits=2,
            test_size=80,
        )
        assert result["symbol"] == "ETHUSDT"


# ══════════════════════════════════════════════════════════════
# 7. main() — 通过 subprocess 或 argparse 调用
# ══════════════════════════════════════════════════════════════

class TestMain:

    def test_main_exits_1_on_no_data(self, tmp_path):
        """main() 缺少数据时应以 exit code 1 退出。"""
        from scripts.optimize_phase1_params import main
        with pytest.raises(SystemExit) as exc_info:
            sys.argv = ["optimize_phase1_params.py", "--symbol", "BTCUSDT"]
            main()
        assert exc_info.value.code == 1

    def test_main_exits_1_on_missing_file(self, tmp_path):
        from scripts.optimize_phase1_params import main
        with pytest.raises(SystemExit) as exc_info:
            sys.argv = [
                "optimize_phase1_params.py",
                "--symbol", "BTCUSDT",
                "--data", str(tmp_path / "ghost.csv"),
            ]
            main()
        assert exc_info.value.code == 1

    def test_main_succeeds_with_synthetic_data(self, tmp_path, capsys):
        from scripts.optimize_phase1_params import main
        # Should not raise SystemExit
        sys.argv = [
            "optimize_phase1_params.py",
            "--symbol", "BTCUSDT",
            "--allow-synthetic-data",
            "--n-trials", "2",
            "--n-splits", "2",
            "--test-size", "100",
            "--output-dir", str(tmp_path),
        ]
        main()
        captured = capsys.readouterr()
        assert "Phase 1 优化结果摘要" in captured.out
