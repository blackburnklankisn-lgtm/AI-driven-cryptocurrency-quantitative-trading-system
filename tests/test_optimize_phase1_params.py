from __future__ import annotations

import pytest


class TestOptimizePhase1Params:
    def test_load_data_requires_real_input_by_default(self, tmp_path):
        from scripts.optimize_phase1_params import load_data

        missing_csv = tmp_path / "missing.csv"

        with pytest.raises(FileNotFoundError):
            load_data("BTC/USDT", str(missing_csv))

        with pytest.raises(ValueError):
            load_data("BTC/USDT")

    def test_load_data_allows_synthetic_only_when_explicitly_enabled(self):
        from scripts.optimize_phase1_params import load_data

        df = load_data("BTC/USDT", allow_synthetic_data=True)

        assert not df.empty
        assert {
            "timestamp",
            "symbol",
            "open",
            "high",
            "low",
            "close",
            "volume",
        }.issubset(df.columns)
        assert df["symbol"].nunique() == 1
        assert df["symbol"].iloc[0] == "BTC/USDT"