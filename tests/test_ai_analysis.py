from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from apps.trader.main import LiveTrader


def _make_trader() -> LiveTrader:
    trader = object.__new__(LiveTrader)
    trader._gemini_api_key = None
    trader._gemini_model_name = "gemini-3-flash-preview"
    trader._last_ai_analysis = "已配置 Gemini，正在等待行情数据稳定后自动生成本次启动后的 AI 解读。"
    trader._next_ai_analysis_at = None
    trader._pending_ai_analysis_refresh = False
    trader._preload_done = True
    trader._phase3_subscription_manager = None
    trader._kline_store = {
        "BTC/USDT": [
            {"close": 68000.0},
            {"close": 68120.5},
            {"close": 67980.2},
        ]
    }
    return trader


class _LegacyGenerativeModel:
    def __init__(self, model_name: str, recorder: dict[str, str]) -> None:
        recorder["model_name"] = model_name

    def generate_content(self, prompt: str) -> SimpleNamespace:
        return SimpleNamespace(text=f"legacy::{prompt}")


def test_resolve_gemini_api_key_prefers_gemini_env() -> None:
    trader = _make_trader()

    with patch.dict(
        "os.environ",
        {"GEMINI_API_KEY": "gemini-key", "GOOGLE_API_KEY": "legacy-key"},
        clear=True,
    ):
        assert trader._resolve_gemini_api_key() == "gemini-key"


def test_run_ai_analysis_uses_gemini_env_with_legacy_sdk() -> None:
    trader = _make_trader()
    recorder: dict[str, str] = {}

    legacy_module = SimpleNamespace(
        configure=lambda *, api_key: recorder.__setitem__("api_key", api_key),
        GenerativeModel=lambda model_name: _LegacyGenerativeModel(model_name, recorder),
    )

    with patch.dict("os.environ", {"GEMINI_API_KEY": "gemini-key"}, clear=True):
        with patch.object(LiveTrader, "_load_google_genai_module", return_value=None):
            with patch.object(LiveTrader, "_load_google_generativeai_module", return_value=legacy_module):
                trader._run_ai_analysis()

    assert recorder["api_key"] == "gemini-key"
    assert recorder["model_name"] == "gemini-3-flash-preview"
    assert trader._last_ai_analysis.startswith("legacy::你是一个专业的加密货币交易员")


def test_extract_gemini_response_text_supports_candidate_parts() -> None:
    trader = _make_trader()

    response = SimpleNamespace(
        text=None,
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(text="市场偏强"),
                        SimpleNamespace(text="注意波动放大风险"),
                    ]
                )
            )
        ],
    )

    assert trader._extract_gemini_response_text(response) == "市场偏强\n注意波动放大风险"


def test_build_ai_analysis_placeholder_distinguishes_missing_key() -> None:
    trader = _make_trader()

    with patch.dict("os.environ", {}, clear=True):
        assert trader._build_ai_analysis_placeholder() == "未配置 GEMINI_API_KEY 或 GOOGLE_API_KEY，AI 盘面解读不可用。"


def test_pending_ai_refresh_runs_once_after_startup_when_context_ready() -> None:
    trader = _make_trader()
    trader._gemini_api_key = "gemini-key"
    trader._pending_ai_analysis_refresh = True
    trader._run_ai_analysis = MagicMock()
    trader._next_ai_analysis_at = datetime(2026, 4, 28, 13, 0, 0, tzinfo=timezone.utc)

    trader._maybe_run_ai_analysis(datetime(2026, 4, 28, 12, 5, 0, tzinfo=timezone.utc))

    trader._run_ai_analysis.assert_called_once()
    assert trader._pending_ai_analysis_refresh is False
    assert trader._next_ai_analysis_at == datetime(2026, 4, 28, 13, 0, 0, tzinfo=timezone.utc)


def test_pending_ai_refresh_waits_for_context_when_data_not_ready() -> None:
    trader = _make_trader()
    trader._gemini_api_key = "gemini-key"
    trader._pending_ai_analysis_refresh = True
    trader._preload_done = False
    trader._kline_store = {"BTC/USDT": []}
    trader._run_ai_analysis = MagicMock()
    trader._next_ai_analysis_at = datetime(2026, 4, 28, 13, 0, 0, tzinfo=timezone.utc)

    trader._maybe_run_ai_analysis(datetime(2026, 4, 28, 12, 5, 0, tzinfo=timezone.utc))

    trader._run_ai_analysis.assert_not_called()
    assert trader._pending_ai_analysis_refresh is True
    assert trader._last_ai_analysis == "已配置 Gemini，正在等待行情数据稳定后自动生成本次启动后的 AI 解读。"


def test_maybe_run_ai_analysis_runs_once_when_hour_becomes_due() -> None:
    trader = _make_trader()
    trader._run_ai_analysis = MagicMock()
    trader._next_ai_analysis_at = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)

    trader._maybe_run_ai_analysis(datetime(2026, 4, 28, 12, 0, 5, tzinfo=timezone.utc))
    trader._maybe_run_ai_analysis(datetime(2026, 4, 28, 12, 30, 0, tzinfo=timezone.utc))

    trader._run_ai_analysis.assert_called_once()
    assert trader._next_ai_analysis_at == datetime(2026, 4, 28, 13, 0, 0, tzinfo=timezone.utc)


def test_maybe_run_ai_analysis_catches_up_after_missed_hour_without_duplicate_runs() -> None:
    trader = _make_trader()
    trader._run_ai_analysis = MagicMock()
    trader._next_ai_analysis_at = datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)

    trader._maybe_run_ai_analysis(datetime(2026, 4, 28, 14, 15, 0, tzinfo=timezone.utc))

    trader._run_ai_analysis.assert_called_once()
    assert trader._next_ai_analysis_at == datetime(2026, 4, 28, 15, 0, 0, tzinfo=timezone.utc)