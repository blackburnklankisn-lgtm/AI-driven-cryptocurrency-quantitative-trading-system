"""
tests/test_phase3_trace_and_main.py — Phase 3 可观测性 & main.py 装配测试

覆盖范围：
1. TestTraceModule         — modules/monitoring/trace.py 单元测试
2. TestMMTraceInjection    — MarketMakingStrategy.tick() trace_id 注入验证
3. TestRLTraceInjection    — PPOAgent.predict() trace_id 注入验证
4. TestEvolutionTraceInjection — SelfEvolutionEngine.run_cycle() trace_id 注入验证
5. TestPhase3MainWiring    — LiveTrader Phase 3 shadow-only 生命周期装配验证
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ─────────────────────────────────────────────────────────────
# 辅助：禁用 JSONL 写入（避免测试产生文件）
# ─────────────────────────────────────────────────────────────

def _disable_recorder():
    from modules.monitoring.trace import get_recorder
    get_recorder().disable()


def _enable_recorder():
    from modules.monitoring.trace import get_recorder
    get_recorder().enable()


# ══════════════════════════════════════════════════════════════
# 1. TestTraceModule — trace.py 单元测试
# ══════════════════════════════════════════════════════════════

class TestTraceModule:
    """modules/monitoring/trace.py 核心功能测试。"""

    def test_generate_trace_id_format_mm(self):
        from modules.monitoring.trace import generate_trace_id
        tid = generate_trace_id("mm", "BTC/USDT")
        # 格式: mm-BTCUSDT-{TS}-{SEQ}
        parts = tid.split("-")
        assert parts[0] == "mm"
        assert parts[1] == "BTCUSDT"   # 斜杠已剥离
        assert len(parts) == 4

    def test_generate_trace_id_format_no_qualifier(self):
        from modules.monitoring.trace import generate_trace_id
        tid = generate_trace_id("rl")
        parts = tid.split("-")
        assert parts[0] == "rl"
        assert len(parts) == 3   # rl-{TS}-{SEQ}

    def test_generate_trace_id_sequential(self):
        """同一 domain 的序列号严格单调递增。"""
        from modules.monitoring.trace import generate_trace_id
        ids = [generate_trace_id("ev") for _ in range(5)]
        seqs = [int(tid.split("-")[-1]) for tid in ids]
        for i in range(1, len(seqs)):
            assert seqs[i] > seqs[i - 1]

    def test_generate_trace_id_independent_domains(self):
        """不同 domain 的序列号互不干扰。"""
        from modules.monitoring.trace import generate_trace_id
        mm1 = generate_trace_id("mm_test_a")
        rl1 = generate_trace_id("rl_test_a")
        # seq 由 domain 独立维护，两者不相互影响
        assert mm1.split("-")[0] == "mm_test_a"
        assert rl1.split("-")[0] == "rl_test_a"

    def test_generate_trace_id_strips_slashes(self):
        from modules.monitoring.trace import generate_trace_id
        tid = generate_trace_id("mm", "ETH/USDT")
        assert "/" not in tid

    def test_generate_trace_id_strips_colons(self):
        from modules.monitoring.trace import generate_trace_id
        tid = generate_trace_id("ev", "ev:test")
        assert ":" not in tid

    def test_recorder_record_writes_jsonl(self):
        """record() 应将 JSON 行写入指定文件。"""
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            tmp_path = f.name
        try:
            from modules.monitoring.trace import Phase3TraceRecorder, generate_trace_id
            rec = Phase3TraceRecorder(tmp_path)
            tid = generate_trace_id("mm_rec_test")
            rec.record(tid, "mm", "TICK_END", {"bid": 50000.0, "ask": 50001.0})
            lines = Path(tmp_path).read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 1
            entry = json.loads(lines[0])
            assert entry["trace_id"] == tid
            assert entry["domain"] == "mm"
            assert entry["event_type"] == "TICK_END"
            assert entry["bid"] == pytest.approx(50000.0)
        finally:
            os.unlink(tmp_path)

    def test_recorder_disable_skips_write(self):
        """disable() 后 record() 不写入文件。"""
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            tmp_path = f.name
        try:
            from modules.monitoring.trace import Phase3TraceRecorder, generate_trace_id
            rec = Phase3TraceRecorder(tmp_path)
            rec.disable()
            rec.record(generate_trace_id("noop"), "mm", "NOOP", {})
            content = Path(tmp_path).read_text(encoding="utf-8")
            assert content == ""
        finally:
            os.unlink(tmp_path)

    def test_recorder_enable_after_disable(self):
        """enable() 后恢复写入。"""
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            tmp_path = f.name
        try:
            from modules.monitoring.trace import Phase3TraceRecorder, generate_trace_id
            rec = Phase3TraceRecorder(tmp_path)
            rec.disable()
            rec.record(generate_trace_id("x_test"), "mm", "SKIP", {})
            rec.enable()
            rec.record(generate_trace_id("x_test"), "mm", "WRITTEN", {})
            lines = Path(tmp_path).read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 1
            assert json.loads(lines[0])["event_type"] == "WRITTEN"
        finally:
            os.unlink(tmp_path)

    def test_init_recorder_replaces_default(self):
        """init_recorder() 替换全局默认 recorder。"""
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            tmp_path = f.name
        try:
            from modules.monitoring import trace as trace_mod
            trace_mod.init_recorder(tmp_path)
            rec = trace_mod.get_recorder()
            assert str(rec.path) == tmp_path
        finally:
            os.unlink(tmp_path)
            # 重置全局 recorder 以不影响后续测试
            trace_mod._default_recorder = None

    def test_get_recorder_lazy_init(self):
        """get_recorder() 在未 init 时应自动创建（不抛异常）。"""
        from modules.monitoring import trace as trace_mod
        old = trace_mod._default_recorder
        trace_mod._default_recorder = None
        try:
            rec = trace_mod.get_recorder()
            assert rec is not None
        finally:
            trace_mod._default_recorder = old

    def test_recorder_record_no_payload(self):
        """record() 不传 payload 时正常工作。"""
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            tmp_path = f.name
        try:
            from modules.monitoring.trace import Phase3TraceRecorder, generate_trace_id
            rec = Phase3TraceRecorder(tmp_path)
            rec.record(generate_trace_id("bare_test"), "ev", "CYCLE_START")
            lines = Path(tmp_path).read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 1
            entry = json.loads(lines[0])
            assert entry["domain"] == "ev"
        finally:
            os.unlink(tmp_path)

    def test_recorder_thread_safety(self):
        """并发 record() 不产生数据竞争（行数正确）。"""
        import threading
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            tmp_path = f.name
        try:
            from modules.monitoring.trace import Phase3TraceRecorder, generate_trace_id
            rec = Phase3TraceRecorder(tmp_path)
            n = 20
            errors: list = []

            def _write():
                try:
                    rec.record(generate_trace_id("thr_test"), "mm", "CONCURRENT", {})
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=_write) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors
            lines = Path(tmp_path).read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == n
        finally:
            os.unlink(tmp_path)


# ══════════════════════════════════════════════════════════════
# 2. TestMMTraceInjection — tick() trace 注入
# ══════════════════════════════════════════════════════════════

class TestMMTraceInjection:
    """验证 MarketMakingStrategy.tick() 中 trace_id 正确注入。"""

    @pytest.fixture(autouse=True)
    def _disable_io(self):
        _disable_recorder()
        yield
        _enable_recorder()

    def _make_mm(self):
        from modules.alpha.market_making.strategy import (
            MarketMakingStrategy,
            MarketMakingStrategyConfig,
        )
        from modules.alpha.market_making.inventory_manager import InventoryConfig

        cfg = MarketMakingStrategyConfig(
            symbol="BTC/USDT",
            paper_mode=True,
            save_every_n=0,
            inventory=InventoryConfig(
                initial_base_qty=0.1,
                initial_quote_value=5000.0,
                target_inventory_pct=0.5,
                max_inventory_pct=0.5,
                halt_inventory_pct=0.9,
            ),
        )
        return MarketMakingStrategy(cfg)

    def _make_healthy_snap(self, mid: float = 50000.0):
        from modules.data.realtime.orderbook_types import OrderBookSnapshot
        return OrderBookSnapshot.create_mock(
            symbol="BTC/USDT",
            mid_price=mid,
            spread_bps=5.0,
            sequence_id=1,
        )

    def _make_risk(self, circuit_broken: bool = False):
        from modules.risk.snapshot import RiskSnapshot
        return RiskSnapshot(
            current_drawdown=0.0,
            daily_loss_pct=0.0,
            consecutive_losses=0,
            circuit_broken=circuit_broken,
            kill_switch_active=False,
            budget_remaining_pct=1.0,
        )

    def test_tick_runs_without_error(self):
        mm = self._make_mm()
        snap = self._make_healthy_snap()
        risk = self._make_risk()
        decision = mm.tick(snap, risk, elapsed_sec=1.0)
        assert decision is not None

    def test_tick_halt_risk_blocked_no_crash(self):
        mm = self._make_mm()
        snap = self._make_healthy_snap()
        risk = self._make_risk(circuit_broken=True)
        decision = mm.tick(snap, risk)
        assert decision is not None
        assert decision.bid_price is None
        assert decision.ask_price is None

    def test_trace_id_injected_in_log(self):
        """验证 tick() 经过任意决策路径（tick 或 halt）都有 [MarketMaking] 日志输出。"""
        mm = self._make_mm()
        snap = self._make_healthy_snap()
        risk = self._make_risk()
        captured: list[str] = []
        from loguru import logger

        # 同时监听 tick debug 和 halt warning 日志
        handler_id = logger.add(
            lambda msg: captured.append(msg),
            level="DEBUG",
            format="{message}",
            filter=lambda r: "[MarketMaking]" in str(r["message"]),
        )
        try:
            mm.tick(snap, risk, elapsed_sec=1.0)
        finally:
            logger.remove(handler_id)

        # tick 或 halt 路径至少有一条 [MarketMaking] 日志
        assert any("[MarketMaking]" in m for m in captured)

    def test_recorder_receives_tick_event(self):
        """tick() 完成后 recorder 应收到 TICK_END 或 TICK_HALT 事件。"""
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            tmp_path = f.name
        try:
            from modules.monitoring.trace import Phase3TraceRecorder
            import modules.monitoring.trace as _trace_mod
            old_rec = _trace_mod._default_recorder
            _trace_mod._default_recorder = Phase3TraceRecorder(tmp_path)

            mm = self._make_mm()
            snap = self._make_healthy_snap()
            risk = self._make_risk()
            mm.tick(snap, risk, elapsed_sec=1.0)

            lines = Path(tmp_path).read_text(encoding="utf-8").strip().splitlines()
            events = [json.loads(l)["event_type"] for l in lines if l]
            # 主路径输出 TICK_END，进入 halt 输出 TICK_HALT，两者皆合法
            assert "TICK_END" in events or "TICK_HALT" in events
        finally:
            _trace_mod._default_recorder = old_rec
            os.unlink(tmp_path)


# ══════════════════════════════════════════════════════════════
# 3. TestRLTraceInjection — predict() trace 注入
# ══════════════════════════════════════════════════════════════

class TestRLTraceInjection:
    """验证 PPOAgent.predict() 中 trace_id 正确注入。"""

    @pytest.fixture(autouse=True)
    def _disable_io(self):
        _disable_recorder()
        yield
        _enable_recorder()

    def _make_agent(self):
        from modules.alpha.rl.ppo_agent import PPOAgent, PPOConfig
        return PPOAgent(PPOConfig(obs_dim=24, n_actions=8))

    def test_predict_returns_correct_structure(self):
        agent = self._make_agent()
        obs = [0.0] * 24
        result = agent.predict(obs, deterministic=True)
        assert len(result) == 4
        action_idx, action_value, confidence, log_prob = result
        assert 0 <= action_idx < 8
        assert 0.0 < confidence <= 1.0

    def test_predict_trace_id_in_log(self):
        agent = self._make_agent()
        obs = [0.0] * 24
        captured: list[str] = []
        from loguru import logger

        handler_id = logger.add(
            lambda msg: captured.append(msg),
            level="DEBUG",
            format="{message}",
            filter=lambda r: "[RLPolicy] predict:" in str(r["message"]),
        )
        try:
            agent.predict(obs, deterministic=True)
        finally:
            logger.remove(handler_id)

        assert any("trace_id=" in m for m in captured)

    def test_recorder_receives_rl_predict(self):
        """predict() 完成后 recorder 应收到 RL_PREDICT 事件。"""
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            tmp_path = f.name
        try:
            from modules.monitoring.trace import Phase3TraceRecorder
            import modules.monitoring.trace as _trace_mod
            old_rec = _trace_mod._default_recorder
            _trace_mod._default_recorder = Phase3TraceRecorder(tmp_path)

            agent = self._make_agent()
            agent.predict([0.0] * 24, deterministic=False)

            lines = Path(tmp_path).read_text(encoding="utf-8").strip().splitlines()
            events = [json.loads(l)["event_type"] for l in lines if l]
            assert "RL_PREDICT" in events
        finally:
            _trace_mod._default_recorder = old_rec
            os.unlink(tmp_path)

    def test_predict_stochastic_returns_valid(self):
        agent = self._make_agent()
        obs = [0.0] * 24
        idx, val, conf, lp = agent.predict(obs, deterministic=False)
        assert 0 <= idx < 8
        assert lp < 0  # log prob < 0

    def test_predict_confidence_sums_not_exceed_1(self):
        """各动作概率应合法（单个 confidence <= 1）。"""
        agent = self._make_agent()
        for _ in range(10):
            obs = [0.1] * 24
            _, _, conf, _ = agent.predict(obs, deterministic=False)
            assert 0.0 < conf <= 1.0


# ══════════════════════════════════════════════════════════════
# 4. TestEvolutionTraceInjection — run_cycle() trace 注入
# ══════════════════════════════════════════════════════════════

class TestEvolutionTraceInjection:
    """验证 SelfEvolutionEngine.run_cycle() 中 trace_id 正确注入。"""

    @pytest.fixture(autouse=True)
    def _disable_io(self):
        _disable_recorder()
        yield
        _enable_recorder()

    def _make_engine(self):
        from modules.evolution.self_evolution_engine import (
            SelfEvolutionEngine,
            SelfEvolutionConfig,
        )
        return SelfEvolutionEngine(
            SelfEvolutionConfig(state_dir="storage/test_evo_trace")
        )

    def test_run_cycle_no_op_returns_none_when_not_due(self):
        """调度器认为未到期时返回 None（不强制执行）。"""
        eng = self._make_engine()
        # First call may or may not run; after that, due to cooldown it returns None
        eng.run_cycle(force=True)   # consume the first run
        result = eng.run_cycle(force=False)
        assert result is None

    def test_run_cycle_forced_returns_report(self):
        eng = self._make_engine()
        result = eng.run_cycle(force=True)
        assert result is not None
        assert hasattr(result, "summary")

    def test_trace_id_in_log_on_forced_cycle(self):
        eng = self._make_engine()
        captured: list[str] = []
        from loguru import logger

        handler_id = logger.add(
            lambda msg: captured.append(msg),
            level="INFO",
            format="{message}",
            filter=lambda r: "[Evolution] 演进周期" in str(r["message"]),
        )
        try:
            eng.run_cycle(force=True)
        finally:
            logger.remove(handler_id)

        # 至少 "开始" 和 "完成" 日志各有 trace_id=
        assert any("trace_id=" in m for m in captured)

    def test_recorder_receives_cycle_start_and_end(self):
        """run_cycle() 应写入 CYCLE_START 和 CYCLE_END 两条 trace 事件。"""
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            tmp_path = f.name
        try:
            from modules.monitoring.trace import Phase3TraceRecorder
            import modules.monitoring.trace as _trace_mod
            old_rec = _trace_mod._default_recorder
            _trace_mod._default_recorder = Phase3TraceRecorder(tmp_path)

            eng = self._make_engine()
            eng.run_cycle(force=True)

            lines = Path(tmp_path).read_text(encoding="utf-8").strip().splitlines()
            events = [json.loads(l)["event_type"] for l in lines if l]
            assert "CYCLE_START" in events
            assert "CYCLE_END" in events
        finally:
            _trace_mod._default_recorder = old_rec
            os.unlink(tmp_path)

    def test_cycle_start_and_end_share_same_trace_id(self):
        """CYCLE_START 和 CYCLE_END 应共享同一个 trace_id（同一次 cycle）。"""
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            tmp_path = f.name
        try:
            from modules.monitoring.trace import Phase3TraceRecorder
            import modules.monitoring.trace as _trace_mod
            old_rec = _trace_mod._default_recorder
            _trace_mod._default_recorder = Phase3TraceRecorder(tmp_path)

            eng = self._make_engine()
            eng.run_cycle(force=True)

            lines = Path(tmp_path).read_text(encoding="utf-8").strip().splitlines()
            entries = [json.loads(l) for l in lines if l]
            start_ids = [e["trace_id"] for e in entries if e["event_type"] == "CYCLE_START"]
            end_ids   = [e["trace_id"] for e in entries if e["event_type"] == "CYCLE_END"]
            assert start_ids and end_ids
            assert start_ids[0] == end_ids[0]
        finally:
            _trace_mod._default_recorder = old_rec
            os.unlink(tmp_path)


# ══════════════════════════════════════════════════════════════
# 5. TestPhase3MainWiring — LiveTrader Phase 3 shadow 装配
# ══════════════════════════════════════════════════════════════

class TestPhase3MainWiring:
    """
    验证 LiveTrader 能够正确初始化 Phase 3 shadow 组件，
    以及 _step_phase3_shadow() 在正常和异常情况下都不崩溃。

    使用最轻量的 Mock，避免真实 CCXT 连接。
    """

    @pytest.fixture()
    def sys_cfg(self):
        """构造最小化的 SystemConfig mock，包含 phase3.enabled=True。"""
        from core.config import load_config

        cfg = load_config()
        # phase3 已在 system.yaml 中启用
        return cfg

    def test_phase3_config_enabled_by_default(self, sys_cfg):
        """确认 system.yaml 中 phase3.enabled=True。"""
        assert sys_cfg.phase3.enabled is True

    def test_init_phase3_components_does_not_raise(self):
        """_init_phase3_components() 能成功初始化，无未捕获异常。"""
        from modules.monitoring.trace import get_recorder
        get_recorder().disable()

        p3_cfg = _load_p3_cfg()

        # 构造最小 trader mock（不需要真实 CCXT）
        trader = _make_minimal_trader()
        trader._init_phase3_components(p3_cfg)

        assert trader._phase3_enabled is True
        # 至少有一个组件成功初始化
        assert (
            trader._phase3_mm is not None
            or trader._phase3_ppo is not None
            or trader._phase3_evolution is not None
        )
        get_recorder().enable()

    def test_init_phase3_components_enables_realtime_feed_by_default_in_paper_mode(self):
        """paper 模式下默认应启动 realtime paper feed。"""
        trader = _make_minimal_trader()
        trader._init_phase3_components(_load_p3_cfg())

        assert trader._phase3_realtime_enabled is True
        assert trader._phase3_ws_client is not None
        assert trader._phase3_subscription_manager is not None
        assert trader._phase3_depth_registry is not None
        assert trader._phase3_trade_registry is not None
        trader._phase3_subscription_manager.stop()

    def test_init_phase3_components_respects_explicit_realtime_disable(self):
        """即使 paper 模式默认开启，显式关闭配置仍应生效。"""
        p3_cfg = _load_p3_cfg().model_copy(update={"realtime_feed_enabled": False})

        trader = _make_minimal_trader()
        trader._init_phase3_components(p3_cfg)

        assert trader._phase3_realtime_enabled is False
        assert trader._phase3_ws_client is None
        assert trader._phase3_subscription_manager is None

    def test_create_phase3_ws_client_uses_htx_provider(self):
        """provider=htx 时，应构建真实 HTX WebSocket client。"""
        from apps.trader.main import LiveTrader
        from core.config import Phase3RealtimeFeedConfig
        from modules.data.realtime.ws_client import HtxMarketWsClient, WsClientConfig

        trader = _make_minimal_trader()
        client = LiveTrader._create_phase3_ws_client(
            trader,
            Phase3RealtimeFeedConfig(provider="htx"),
            WsClientConfig(exchange="htx"),
        )

        assert isinstance(client, HtxMarketWsClient)

    def test_create_phase3_ws_client_unknown_provider_falls_back_to_mock(self):
        """未知 provider 时，应回退 MockWsClient。"""
        from apps.trader.main import LiveTrader
        from core.config import Phase3RealtimeFeedConfig
        from modules.data.realtime.ws_client import MockWsClient, WsClientConfig

        trader = _make_minimal_trader()
        client = LiveTrader._create_phase3_ws_client(
            trader,
            Phase3RealtimeFeedConfig(provider="unknown"),
            WsClientConfig(exchange="htx"),
        )

        assert isinstance(client, MockWsClient)

    def test_step_phase3_shadow_disabled_is_noop(self):
        """_phase3_enabled=False 时 shadow step 应该什么都不做（无异常）。"""
        trader = _make_minimal_trader()
        trader._phase3_enabled = False
        # 不抛异常即为通过
        trader._step_phase3_shadow("BTC/USDT", 50000.0, seq=1)

    def test_step_phase3_shadow_enabled_no_crash(self):
        """_phase3_enabled=True + 有效组件时 shadow step 不崩溃。"""
        from modules.monitoring.trace import get_recorder
        get_recorder().disable()

        trader = _make_minimal_trader()
        p3_cfg = _load_p3_cfg()
        trader._init_phase3_components(p3_cfg)

        # 确保 risk_manager 有 is_circuit_broken 方法
        trader.risk_manager = MagicMock()
        trader.risk_manager.is_circuit_broken.return_value = False

        trader._step_phase3_shadow("BTC/USDT", 50000.0, seq=1)
        get_recorder().enable()

    def test_step_phase3_shadow_symbol_mismatch_skips_mm(self):
        """当 symbol 与 MM 配置的 symbol 不匹配时，MM shadow tick 应跳过。"""
        from modules.monitoring.trace import get_recorder
        get_recorder().disable()

        trader = _make_minimal_trader()
        trader._phase3_enabled = True

        # 配置 MM 为 BTC/USDT，但传入 ETH/USDT
        mm_mock = MagicMock()
        mm_mock.config.symbol = "BTC/USDT"
        mm_mock.tick.side_effect = AssertionError("Should not call tick for ETH/USDT")
        trader._phase3_mm = mm_mock
        trader._phase3_ppo = None
        trader._phase3_evolution = None

        # 不应抛出异常（symbol mismatch → 跳过 MM）
        trader._step_phase3_shadow("ETH/USDT", 3000.0, seq=1)
        mm_mock.tick.assert_not_called()
        get_recorder().enable()

    def test_step_phase3_shadow_ppo_none_no_crash(self):
        """ppo=None 时 RL shadow 步骤应跳过，不崩溃。"""
        trader = _make_minimal_trader()
        trader._phase3_enabled = True
        trader._phase3_mm = None
        trader._phase3_ppo = None
        trader._phase3_evolution = None
        trader._step_phase3_shadow("BTC/USDT", 50000.0, seq=1)

    def test_step_phase3_shadow_evolution_none_no_crash(self):
        """evolution=None 时演进步骤应跳过，不崩溃。"""
        trader = _make_minimal_trader()
        trader._phase3_enabled = True
        trader._phase3_mm = None
        trader._phase3_ppo = None
        trader._phase3_evolution = None
        trader._step_phase3_shadow("BTC/USDT", 50000.0, seq=5)

    def test_step_phase3_shadow_mm_exception_not_propagated(self):
        """MM shadow tick 抛出异常时，异常不向上传播。"""
        trader = _make_minimal_trader()
        trader._phase3_enabled = True

        mm_mock = MagicMock()
        mm_mock.config.symbol = "BTC/USDT"
        mm_mock.tick.side_effect = RuntimeError("simulated MM error")
        trader._phase3_mm = mm_mock
        trader._phase3_ppo = None
        trader._phase3_evolution = None

        # 不抛异常
        trader._step_phase3_shadow("BTC/USDT", 50000.0, seq=1)

    def test_pump_phase3_realtime_feed_populates_registries(self):
        """mock realtime feed pump 后，DepthCache 和 TradeCache 都应有真实数据。"""
        from apps.trader.main import LiveTrader
        from modules.data.realtime.depth_cache import DepthCacheRegistry
        from modules.data.realtime.subscription_manager import (
            SubscriptionManager,
            SubscriptionManagerConfig,
        )
        from modules.data.realtime.trade_cache import TradeCacheRegistry
        from modules.data.realtime.ws_client import (
            MockWsClient,
            MockWsClientConfig,
            WsClientConfig,
        )

        trader = _make_minimal_trader()
        trader._phase3_realtime_enabled = True
        trader._phase3_depth_registry = DepthCacheRegistry()
        trader._phase3_trade_registry = TradeCacheRegistry()
        trader._phase3_ws_client = MockWsClient(
            MockWsClientConfig(
                base_config=WsClientConfig(exchange=trader.sys_config.exchange.exchange_id),
                seed=7,
            )
        )
        trader._phase3_subscription_manager = SubscriptionManager(
            trader._phase3_ws_client,
            trader._phase3_depth_registry,
            trader._phase3_trade_registry,
            SubscriptionManagerConfig(health_check_interval_sec=60.0),
        )
        trader._phase3_subscription_manager.start(["BTC/USDT"])

        LiveTrader._pump_phase3_realtime_feed(trader, "BTC/USDT")

        cache = trader._phase3_depth_registry.get(
            "BTC/USDT",
            trader.sys_config.exchange.exchange_id,
        )
        stats = trader._phase3_trade_registry.get_stats(
            "BTC/USDT",
            trader.sys_config.exchange.exchange_id,
        )

        assert cache is not None
        assert cache.get_snapshot() is not None
        assert stats is not None
        assert stats.trade_count == 1
        trader._phase3_subscription_manager.stop()

    def test_step_phase3_shadow_prefers_registry_snapshot(self):
        """存在 realtime registry 快照时，_step_phase3_shadow() 应优先消费该快照。"""
        from modules.data.realtime.depth_cache import DepthCacheRegistry
        from modules.data.realtime.orderbook_types import DepthLevel, OrderBookDelta

        trader = _make_minimal_trader()
        trader._phase3_enabled = True
        trader._phase3_realtime_enabled = True
        trader._phase3_depth_registry = DepthCacheRegistry()

        delta = OrderBookDelta(
            symbol="BTC/USDT",
            exchange=trader.sys_config.exchange.exchange_id,
            sequence_id=11,
            prev_sequence_id=10,
            bid_updates=[DepthLevel(price=50999.0, size=1.2)],
            ask_updates=[DepthLevel(price=51001.0, size=1.1)],
            received_at=datetime.now(tz=timezone.utc),
            is_snapshot=True,
        )
        trader._phase3_depth_registry.apply_delta(delta)

        mm_mock = MagicMock()
        mm_mock.config.symbol = "BTC/USDT"
        trader._phase3_mm = mm_mock
        trader._phase3_ppo = None
        trader._phase3_evolution = None

        trader._step_phase3_shadow("BTC/USDT", 50000.0, seq=7)

        used_snapshot = mm_mock.tick.call_args.args[0]
        assert used_snapshot.sequence_id == 11
        assert used_snapshot.mid_price == pytest.approx(51000.0)

    def test_shutdown_clears_phase3_state(self):
        """_shutdown() 后 Phase 3 组件引用应被清空。"""
        from modules.monitoring.trace import get_recorder
        get_recorder().disable()

        trader = _make_minimal_trader()
        p3_cfg = _load_p3_cfg()
        trader._init_phase3_components(p3_cfg)
        assert trader._phase3_enabled is True

        # Mock 掉 _shutdown 中的网关和 order_manager 调用
        trader.gateway = MagicMock()
        trader.order_manager = MagicMock()
        trader.order_manager.get_open_orders.return_value = []
        trader._current_equity = 0.0
        trader._save_state = MagicMock()

        trader._shutdown()

        assert trader._phase3_enabled is False
        assert trader._phase3_mm is None
        assert trader._phase3_ppo is None
        assert trader._phase3_evolution is None
        get_recorder().enable()


class TestMainPhaseIntegration:
    """验证 main.py 中 Phase 1/2 真实接线。"""

    def test_register_ml_strategies_loads_runtime_artifacts(self, monkeypatch, tmp_path):
        from apps.trader.main import _register_ml_strategies
        from modules.alpha.ml.threshold_calibrator import CalibrationResult

        trader = _make_minimal_trader()
        trader._continuous_learners = {}
        trader.add_strategy = lambda strategy: trader._strategies.append(strategy)
        trader.sys_config.continuous_learning.enabled = True

        model_dir = tmp_path / "models"
        model_dir.mkdir(parents=True)
        (model_dir / "btcusdt_rf_model.pkl").write_bytes(b"model")

        calibration = CalibrationResult(
            version="cal_test",
            fold_thresholds=[],
            buy_threshold_mean=0.61,
            buy_threshold_median=0.64,
            buy_threshold_conservative=0.66,
            sell_threshold_mean=0.39,
            sell_threshold_median=0.36,
            sell_threshold_conservative=0.34,
            avg_auc=0.71,
            avg_j_statistic=0.11,
            recommended_buy_threshold=0.64,
            recommended_sell_threshold=0.36,
            aggregation_strategy="median",
        )
        calibration.save(model_dir / "btcusdt_threshold.json")
        (model_dir / "btcusdt_best_params.json").write_text(
            json.dumps(
                {
                    "source": "optuna",
                    "params": {
                        "model_type": "lgbm",
                        "n_estimators": 222,
                        "max_depth": 7,
                        "min_samples_leaf": 9,
                    },
                }
            ),
            encoding="utf-8",
        )

        fake_model = SimpleNamespace(model_type="rf", params={"n_estimators": 10})

        class FakePredictor:
            def __init__(self, model, symbol, config, timeframe):
                self.model = model
                self.symbol = symbol
                self.config = config
                self.timeframe = timeframe
                self.strategy_id = f"ml_{symbol.replace('/', '').lower()}"
                self.symbols = [symbol]

        class FakeLearner:
            instances: list[Any] = []

            def __init__(self, trainer, feature_builder, labeler, config):
                self.trainer = trainer
                self.feature_builder = feature_builder
                self.labeler = labeler
                self.config = config
                self._versions = []
                self._active_version = None
                type(self).instances.append(self)

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "modules.alpha.ml.model.SignalModel.load",
            lambda path: fake_model,
        )
        monkeypatch.setattr(
            "modules.alpha.ml.predictor_v2.MLPredictor",
            FakePredictor,
        )
        monkeypatch.setattr(
            "modules.alpha.ml.continuous_learner.ContinuousLearner",
            FakeLearner,
        )

        _register_ml_strategies(trader, ["BTC/USDT"])

        assert len(trader._strategies) == 1
        predictor = trader._strategies[0]
        assert predictor.config.buy_threshold == pytest.approx(0.64)
        assert predictor.config.sell_threshold == pytest.approx(0.36)
        assert len(FakeLearner.instances) == 1
        learner = FakeLearner.instances[0]
        assert learner.trainer.model_type == "lgbm"
        assert learner.trainer.model_params["n_estimators"] == 222
        assert learner.trainer.model_params["max_depth"] == 7

    def test_get_phase1_feature_views_merges_external_source_features(self):
        from apps.trader.main import LiveTrader
        from modules.data.fusion.source_contract import (
            FreshnessStatus,
            SourceFrame,
            SourceFreshness,
        )
        from modules.data.onchain.providers import OnChainRecord
        from modules.data.sentiment.providers import SentimentRecord

        trader = _make_minimal_trader()
        trader._phase2_external_enabled = True

        base_ts = datetime.now(tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
        bars = []
        for idx in range(80):
            ts = base_ts - timedelta(hours=79 - idx)
            price = 50000.0 + idx * 10.0
            bars.append(
                {
                    "time": int(ts.timestamp()),
                    "open": price - 5.0,
                    "high": price + 15.0,
                    "low": price - 15.0,
                    "close": price,
                    "volume": 10.0 + idx,
                }
            )
        trader._kline_store = {"BTC/USDT": bars}

        source_ts = pd.Timestamp(base_ts - timedelta(hours=30))
        fetched_at = datetime.now(tz=timezone.utc)
        freshness = SourceFreshness(
            source_name="external",
            status=FreshnessStatus.FRESH,
            lag_sec=0.0,
            ttl_sec=3600,
            collected_at=fetched_at,
        )

        trader._onchain_collector = MagicMock()
        trader._onchain_collector.collect.return_value = OnChainRecord(
            fetched_at=fetched_at,
            fields={
                "active_addresses_change": 0.12,
                "exchange_inflow_ratio": 0.22,
                "whale_tx_count_ratio": 0.05,
                "stablecoin_supply_ratio": 0.18,
                "miner_reserve_change": -0.01,
                "nvt_proxy": 1.4,
            },
            source_name="public",
        )
        trader._onchain_feature_builder = MagicMock()
        trader._onchain_feature_builder.build.return_value = SourceFrame(
            source_name="onchain_btc",
            frame=pd.DataFrame(
                [{"oc_active_addr_chg": 0.12, "oc_exchange_inflow": 0.22}],
                index=pd.DatetimeIndex([source_ts], tz="UTC"),
            ),
            freshness=freshness,
        )

        trader._sentiment_collector = MagicMock()
        trader._sentiment_collector.collect.return_value = SentimentRecord(
            fetched_at=fetched_at,
            fields={
                "fear_greed_index": 55.0,
                "funding_rate_zscore": 0.5,
                "long_short_ratio_change": 0.03,
                "open_interest_change": 0.02,
                "liquidation_imbalance": -0.1,
                "sentiment_score_ema": 0.62,
            },
            source_name="htx",
        )
        trader._sentiment_feature_builder = MagicMock()
        trader._sentiment_feature_builder.build.return_value = SourceFrame(
            source_name="sentiment_btc",
            frame=pd.DataFrame(
                [{"st_fear_greed": 0.55, "st_sentiment_ema": 0.62}],
                index=pd.DatetimeIndex([source_ts], tz="UTC"),
            ),
            freshness=freshness,
        )

        views = LiveTrader._get_phase1_feature_views(trader, "BTC/USDT")

        assert views is not None
        assert "oc_active_addr_chg" in views["alpha_features"].columns
        assert "st_fear_greed" in views["alpha_features"].columns
        assert "oc_active_addr_chg" in views["regime_features"].columns
        assert "st_sentiment_ema" in views["diagnostic_features"].columns

    def test_build_risk_snapshot_merges_runtime_guards(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader._current_equity = 900.0
        trader.risk_manager.get_state_summary.return_value = {
            "circuit_broken": False,
            "circuit_reason": "",
            "daily_pnl": -45.0,
            "consecutive_losses": 2,
            "peak_equity": 1000.0,
            "daily_start_equity": 1000.0,
        }
        trader._budget_checker.snapshot.return_value = {
            "remaining_budget_pct": 0.35,
            "deployed_pct": 0.55,
        }
        expiry = datetime.now(tz=timezone.utc) + timedelta(minutes=15)
        trader._adaptive_risk.health_snapshot.return_value = {
            "cooldown": {
                "active_symbols": {
                    "BTC/USDT": {
                        "expires_at": expiry.isoformat(),
                        "remaining_min": 15.0,
                    }
                }
            }
        }

        snapshot = LiveTrader._build_risk_snapshot(trader)

        assert snapshot.current_drawdown == pytest.approx(0.10)
        assert snapshot.daily_loss_pct == pytest.approx(0.045)
        assert snapshot.budget_remaining_pct == pytest.approx(0.35)
        assert snapshot.consecutive_losses == 2
        assert snapshot.symbol_in_cooldown("BTC/USDT") is True

    def test_process_order_request_budget_blocks_before_submit(self):
        from apps.trader.main import LiveTrader
        from core.event import EventType, OrderRequestEvent
        from modules.risk.snapshot import RiskPlan, RiskSnapshot

        trader = _make_minimal_trader()
        trader._current_equity = 10000.0
        trader._latest_prices = {"BTC/USDT": 50000.0}
        trader._phase1_feature_views = {
            "BTC/USDT": {
                "alpha_features": __import__("pandas").DataFrame(
                    [{"atr_pct_14": 0.02}]
                ),
                "regime_features": __import__("pandas").DataFrame(),
                "diagnostic_features": __import__("pandas").DataFrame(),
            }
        }
        trader._adaptive_risk.evaluate.return_value = RiskPlan(
            allow_entry=True,
            position_scalar=0.5,
            stop_loss_pct=0.03,
            trailing_trigger_pct=0.04,
            trailing_callback_pct=0.015,
            take_profit_ladder=[0.03],
            dca_levels=[],
            cooldown_minutes=30,
            block_reasons=[],
        )
        trader._budget_checker.check.return_value = (False, "budget blocked", 0.95)
        trader.risk_manager.check.return_value = (True, "通过")

        req = OrderRequestEvent(
            event_type=EventType.ORDER_REQUESTED,
            timestamp=datetime.now(tz=timezone.utc),
            source="test",
            symbol="BTC/USDT",
            side="buy",
            order_type="market",
            quantity=Decimal("0.01"),
            price=None,
            strategy_id="test_strategy",
            request_id="req-1",
        )

        LiveTrader._process_order_request(
            trader,
            req,
            trader._current_equity,
            risk_snapshot=RiskSnapshot.make_default(),
            signal_confidence=0.8,
            strategy_weight=1.0,
            phase_source="test",
        )

        trader._budget_checker.check.assert_called_once()
        trader.order_manager.submit.assert_not_called()
        trader.metrics.record_order_rejected.assert_called_once()

    def test_process_order_request_budget_block_records_evolution_risk_violation(self):
        from apps.trader.main import LiveTrader
        from core.event import EventType, OrderRequestEvent
        from modules.risk.snapshot import RiskPlan, RiskSnapshot

        trader = _make_minimal_trader()
        trader._current_equity = 10000.0
        trader._latest_prices = {"BTC/USDT": 50000.0}
        trader._phase1_feature_views = {
            "BTC/USDT": {
                "alpha_features": __import__("pandas").DataFrame(
                    [{"atr_pct_14": 0.02}]
                ),
                "regime_features": __import__("pandas").DataFrame(),
                "diagnostic_features": __import__("pandas").DataFrame(),
            }
        }
        trader._adaptive_risk.evaluate.return_value = RiskPlan(
            allow_entry=True,
            position_scalar=0.5,
            stop_loss_pct=0.03,
            trailing_trigger_pct=0.04,
            trailing_callback_pct=0.015,
            take_profit_ladder=[0.03],
            dca_levels=[],
            cooldown_minutes=30,
            block_reasons=[],
        )
        trader._budget_checker.check.return_value = (False, "budget blocked", 0.95)
        trader.risk_manager.check.return_value = (True, "通过")
        trader._phase3_evolution = MagicMock()
        trader._phase3_strategy_candidates = {"test_strategy": "cand-test"}

        req = OrderRequestEvent(
            event_type=EventType.ORDER_REQUESTED,
            timestamp=datetime.now(tz=timezone.utc),
            source="test",
            symbol="BTC/USDT",
            side="buy",
            order_type="market",
            quantity=Decimal("0.01"),
            price=None,
            strategy_id="test_strategy",
            request_id="req-1",
        )

        LiveTrader._process_order_request(
            trader,
            req,
            trader._current_equity,
            risk_snapshot=RiskSnapshot.make_default(),
            signal_confidence=0.8,
            strategy_weight=1.0,
            phase_source="test",
        )

        trader._phase3_evolution.record_risk_violation.assert_called_once_with(
            "cand-test",
            n=1,
        )

    def test_process_kline_event_routes_through_orchestrator(self):
        from apps.trader.main import LiveTrader
        from core.event import EventType, KlineEvent, OrderRequestEvent
        from modules.alpha.contracts.strategy_result import StrategyResult
        from modules.alpha.contracts import RegimeState
        from modules.alpha.orchestration.strategy_orchestrator import OrchestrationDecision
        from modules.alpha.orchestration.gating import GatingAction, GatingDecision
        from modules.risk.snapshot import RiskSnapshot

        trader = _make_minimal_trader()
        trader._current_equity = 10000.0
        trader._latest_prices = {"BTC/USDT": 50000.0}
        trader._get_phase1_feature_views = MagicMock(return_value=None)
        detector = MagicMock()
        detector.update.return_value = trader._current_regime
        detector.is_stable = True
        trader._get_or_create_regime_detector = MagicMock(return_value=detector)
        trader._build_symbol_ohlcv_frame = MagicMock(return_value=None)
        trader._build_risk_snapshot = MagicMock(return_value=RiskSnapshot.make_default())

        order_req = OrderRequestEvent(
            event_type=EventType.ORDER_REQUESTED,
            timestamp=datetime.now(tz=timezone.utc),
            source="test",
            symbol="BTC/USDT",
            side="buy",
            order_type="market",
            quantity=Decimal("0.01"),
            price=None,
            strategy_id="alpha_a",
            request_id="req-alpha",
        )
        strategy_result = StrategyResult(
            strategy_id="alpha_a",
            symbol="BTC/USDT",
            action="BUY",
            confidence=0.72,
            order_requests=[order_req],
        )
        trader._alpha_runtime.loop_seq = 0
        trader._alpha_runtime.process_bar.return_value = (
            MagicMock(trace_id="trace-1", loop_seq=1),
            [strategy_result],
        )
        trader._phase1_orchestrator.orchestrate.return_value = OrchestrationDecision(
            selected_results=[strategy_result],
            weights={"alpha_a": 1.0},
            block_reasons=[],
            gating=GatingDecision(action=GatingAction.ALLOW, reduce_factor=1.0, triggered_rules=[]),
            debug_payload={},
        )
        trader._process_order_request = MagicMock()

        event = KlineEvent(
            event_type=EventType.KLINE_UPDATED,
            timestamp=datetime.now(tz=timezone.utc),
            source="live_feed",
            symbol="BTC/USDT",
            timeframe="1h",
            open=Decimal("50000"),
            high=Decimal("50500"),
            low=Decimal("49500"),
            close=Decimal("50200"),
            volume=Decimal("12"),
            is_closed=True,
        )

        LiveTrader._process_kline_event(trader, event)

        trader._phase1_orchestrator.orchestrate.assert_called_once()
        trader._process_order_request.assert_called_once()
        _, kwargs = trader._process_order_request.call_args
        assert kwargs["phase_source"] == "phase1"
        assert kwargs["strategy_weight"] == pytest.approx(1.0)
        assert kwargs["signal_confidence"] == pytest.approx(0.72)

    def test_main_loop_step_feeds_portfolio_runtime_and_rebalancer(self):
        from apps.trader.main import LiveTrader
        from core.event import EventType, KlineEvent
        from modules.portfolio.rebalancer import RebalanceOrder

        trader = _make_minimal_trader()
        trader._preload_done = True
        trader._portfolio_enabled = True
        trader._current_equity = 10000.0
        trader.allocator = MagicMock()
        trader.allocator.is_warm.return_value = True
        trader.allocator.compute_weights.return_value = {"BTC/USDT": 1.0}
        trader.rebalancer = MagicMock()
        trader.rebalancer._bar_count = 7
        trader.rebalancer._last_rebalance_bar = 6
        rebal_order = RebalanceOrder(
            symbol="BTC/USDT",
            side="buy",
            quantity=Decimal("0.02"),
            notional=1000.0,
            reason="scheduled",
            current_weight=0.20,
            target_weight=0.30,
        )
        trader.rebalancer.on_bar_close.return_value = [rebal_order]
        trader.attributor = MagicMock()
        trader._continuous_learners = {}
        trader._prev_closes = {"BTC/USDT": 50000.0}
        trader._fetch_latest_klines = MagicMock(
            return_value=[
                KlineEvent(
                    event_type=EventType.KLINE_UPDATED,
                    timestamp=datetime.now(tz=timezone.utc),
                    source="live_feed",
                    symbol="BTC/USDT",
                    timeframe="1h",
                    open=Decimal("50000"),
                    high=Decimal("51200"),
                    low=Decimal("49800"),
                    close=Decimal("51000"),
                    volume=Decimal("15"),
                    is_closed=True,
                )
            ]
        )
        trader._cache_kline_event = MagicMock()
        trader._process_kline_event = MagicMock()
        trader._process_rebalance_orders = MagicMock()
        trader._run_ai_analysis = MagicMock()
        trader.order_manager.poll_fills.return_value = []
        trader.order_manager.cancel_timed_out_orders.return_value = 0
        trader._update_account_snapshot = MagicMock()
        trader._check_stop_loss = MagicMock()
        trader._check_daily_reset = MagicMock()

        LiveTrader._main_loop_step(trader, 1)

        update_args = trader.allocator.update_return.call_args.args
        assert update_args[0] == "BTC/USDT"
        assert update_args[1] == pytest.approx(0.02)
        trader.attributor.record_price.assert_called_once()
        trader.rebalancer.on_bar_close.assert_called_once()
        trader._process_rebalance_orders.assert_called_once_with([rebal_order])

    def test_main_loop_step_feeds_continuous_learner_and_hot_swap_callback(self):
        from apps.trader.main import LiveTrader
        from core.event import EventType, KlineEvent

        trader = _make_minimal_trader()
        trader._preload_done = True
        trader._portfolio_enabled = False
        trader.allocator = None
        trader.rebalancer = None
        trader.attributor = MagicMock()

        learner = MagicMock()
        learner._ohlcv_buffer = [object()] * 12
        learner._bars_since_retrain = 3
        learner.config = SimpleNamespace(min_bars_for_retrain=100, max_buffer_size=500)
        new_model = object()
        learner.on_new_bar.return_value = new_model
        trader._continuous_learners = {"ml_BTC_USDT_1h": learner}
        trader._prev_closes = {}
        event = KlineEvent(
            event_type=EventType.KLINE_UPDATED,
            timestamp=datetime.now(tz=timezone.utc),
            source="live_feed",
            symbol="BTC/USDT",
            timeframe="1h",
            open=Decimal("50000"),
            high=Decimal("50300"),
            low=Decimal("49750"),
            close=Decimal("50200"),
            volume=Decimal("12"),
            is_closed=True,
        )
        trader._fetch_latest_klines = MagicMock(return_value=[event])
        trader._cache_kline_event = MagicMock()
        trader._process_kline_event = MagicMock()
        trader._on_model_updated = MagicMock()
        trader._run_ai_analysis = MagicMock()
        trader.order_manager.poll_fills.return_value = []
        trader.order_manager.cancel_timed_out_orders.return_value = 0
        trader._update_account_snapshot = MagicMock()
        trader._check_stop_loss = MagicMock()
        trader._check_daily_reset = MagicMock()

        LiveTrader._main_loop_step(trader, 2)

        learner.on_new_bar.assert_called_once()
        row = learner.on_new_bar.call_args.args[0]
        assert row["timestamp"] == event.timestamp
        assert row["open"] == pytest.approx(50000.0)
        assert row["high"] == pytest.approx(50300.0)
        assert row["low"] == pytest.approx(49750.0)
        assert row["close"] == pytest.approx(50200.0)
        assert row["volume"] == pytest.approx(12.0)
        trader._on_model_updated.assert_called_once_with("ml_BTC_USDT_1h", learner, new_model)

    def test_process_rebalance_orders_submits_with_portfolio_strategy_id(self):
        from apps.trader.main import LiveTrader
        from modules.portfolio.rebalancer import RebalanceOrder

        trader = _make_minimal_trader()
        trader._current_equity = 10000.0
        trader.rebalancer = MagicMock()
        trader.rebalancer._consecutive_drift_noop = 4

        order = RebalanceOrder(
            symbol="BTC/USDT",
            side="buy",
            quantity=Decimal("0.02"),
            notional=1000.0,
            reason="drift",
            current_weight=0.10,
            target_weight=0.20,
        )

        LiveTrader._process_rebalance_orders(trader, [order])

        trader.order_manager.submit.assert_called_once_with(
            symbol="BTC/USDT",
            side="buy",
            order_type="market",
            quantity=Decimal("0.02"),
            price=None,
            strategy_id="portfolio_rebalancer",
        )
        assert trader.rebalancer._consecutive_drift_noop == 0

    def test_on_fill_records_trade_for_performance_attribution(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader.gateway = MagicMock()
        trader.gateway.paper_cash = 4800.0
        trader.attributor = MagicMock()
        trader._current_equity = 5000.0

        fill = SimpleNamespace(
            avg_price=Decimal("50000"),
            new_filled_qty=Decimal("0.01"),
            order_record=SimpleNamespace(
                strategy_id="alpha_a",
                symbol="BTC/USDT",
                side="buy",
                exchange_id="paper-order-1",
            ),
        )

        with patch("apps.trader.main.trade_log"):
            LiveTrader._on_fill(trader, fill)

        trader.attributor.record_trade.assert_called_once()
        kwargs = trader.attributor.record_trade.call_args.kwargs
        assert kwargs["symbol"] == "BTC/USDT"
        assert kwargs["strategy_id"] == "alpha_a"
        assert kwargs["side"] == "buy"
        assert kwargs["quantity"] == pytest.approx(0.01)
        assert kwargs["price"] == pytest.approx(50000.0)

    def test_on_model_updated_registers_evolution_candidate(self):
        from apps.trader.main import LiveTrader
        from modules.alpha.contracts.evolution_types import CandidateStatus

        trader = _make_minimal_trader()
        trader._phase3_evolution = MagicMock()
        trader._phase3_evolution.register_candidate.return_value = SimpleNamespace(
            candidate_id="cand-new-model"
        )
        trader.attributor = MagicMock()
        trader.attributor.get_strategy_evolution_metrics.return_value = {}
        trader.attributor.get_strategy_realized_trade_pnls.return_value = [1.0, 1.2, 0.8]
        trader._phase3_evolution.list_active.return_value = [
            SimpleNamespace(
                candidate_id="cand-base",
                metadata={
                    "family_key": "ml_BTC_USDT_1h",
                    "strategy_id": "ml_BTC_USDT_1h",
                },
                status=CandidateStatus.ACTIVE.value,
            )
        ]
        trader._phase3_evolution.create_ab_experiment.return_value = "ab-ml-1"
        trader._phase3_evolution.config = SimpleNamespace(
            ab_test=SimpleNamespace(min_samples=3)
        )

        strategy = SimpleNamespace(
            strategy_id="ml_BTC_USDT_1h",
            model=SimpleNamespace(model_type="rf"),
            set_thresholds=MagicMock(),
        )
        trader._strategies = [strategy]

        learner = SimpleNamespace(
            _active_version=SimpleNamespace(
                version_id="v_cl_002",
                trained_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                train_bars=500,
                oos_accuracy=0.61,
                oos_f1=0.57,
                model_path="models/model_v2.pkl",
            ),
            get_optimal_thresholds=lambda: (0.66, 0.34),
        )
        new_model = SimpleNamespace(model_type="lgbm")

        LiveTrader._on_model_updated(trader, "ml_BTC_USDT_1h", learner, new_model)

        trader._phase3_evolution.register_candidate.assert_called_once()
        args = trader._phase3_evolution.register_candidate.call_args.args
        kwargs = trader._phase3_evolution.register_candidate.call_args.kwargs
        assert args[0].value == "model"
        assert kwargs["owner"] == "ml/lgbm"
        assert kwargs["version"] == "v_cl_002"
        assert kwargs["metadata"]["strategy_id"] == "ml_BTC_USDT_1h"
        assert trader._phase3_strategy_candidates["ml_BTC_USDT_1h"] == "cand-new-model"
        assert trader._phase3_candidate_runtime_state["cand-new-model"]["model"] is new_model
        assert trader._phase3_candidate_experiments["cand-new-model"] == "ab-ml-1"
        trader._phase3_evolution.create_ab_experiment.assert_called_once_with(
            "cand-base",
            "cand-new-model",
        )
        assert trader._phase3_evolution.record_ab_step.call_count == 3
        strategy.set_thresholds.assert_called_once_with(0.66, 0.34)

    def test_register_evolution_params_candidate_keeps_model_metric_owner(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader._phase3_evolution = MagicMock()
        trader._phase3_evolution.register_candidate.return_value = SimpleNamespace(
            candidate_id="cand-params-v1"
        )
        trader._phase3_evolution.list_active.return_value = []
        trader._phase3_strategy_candidates = {"ml_BTC_USDT_1h": "cand-model-v1"}
        trader._phase3_strategy_candidate_bindings = {
            "ml_BTC_USDT_1h": {"model": "cand-model-v1"}
        }
        trader._phase3_strategy_metric_bindings = {"ml_BTC_USDT_1h": "model"}
        trader._strategies = [
            SimpleNamespace(
                strategy_id="ml_BTC_USDT_1h",
                model=SimpleNamespace(model_type="rf"),
                cfg=SimpleNamespace(buy_threshold=0.64, sell_threshold=0.36),
                set_thresholds=MagicMock(),
            )
        ]
        learner = SimpleNamespace(
            trainer=SimpleNamespace(
                model_type="lgbm",
                model_params={"num_leaves": 31, "learning_rate": 0.05},
            )
        )
        trader._continuous_learners = {"ml_BTC_USDT_1h": learner}

        candidate_id = LiveTrader._register_evolution_params_candidate(
            trader,
            "ml_BTC_USDT_1h",
            learner,
            {
                "buy_threshold": 0.64,
                "sell_threshold": 0.36,
                "trainer_model_type": "lgbm",
                "trainer_model_params": {"num_leaves": 31, "learning_rate": 0.05},
                "params_source": "btcusdt_best_params.json",
                "threshold_source": "btcusdt_threshold.json",
            },
            source="initial_load",
            set_metric_owner=False,
        )

        assert candidate_id == "cand-params-v1"
        args = trader._phase3_evolution.register_candidate.call_args.args
        kwargs = trader._phase3_evolution.register_candidate.call_args.kwargs
        assert args[0].value == "params"
        assert kwargs["metadata"]["binding_slot"] == "params"
        assert kwargs["metadata"]["family_key"] == "ml_BTC_USDT_1h/params"
        assert trader._phase3_strategy_candidates["ml_BTC_USDT_1h"] == "cand-model-v1"
        assert (
            trader._phase3_strategy_candidate_bindings["ml_BTC_USDT_1h"]["params"]
            == "cand-params-v1"
        )
        state = trader._phase3_candidate_runtime_state["cand-params-v1"]
        assert state["runtime_kind"] == "ml_params"
        assert state["buy_threshold"] == pytest.approx(0.64)
        assert state["sell_threshold"] == pytest.approx(0.36)
        assert state["trainer_model_type"] == "lgbm"
        assert state["trainer_model_params"]["num_leaves"] == 31

    def test_maybe_rollout_updated_ml_params_candidates_rolls_out_changed_artifacts(self):
        from apps.trader.main import LiveTrader

        class DummyStrategy:
            def __init__(self):
                self.strategy_id = "ml_BTC_USDT_1h"
                self.symbol = "BTC/USDT"
                self.model = SimpleNamespace(model_type="rf")
                self.cfg = SimpleNamespace(buy_threshold=0.72, sell_threshold=0.28)

            def set_thresholds(self, buy_threshold, sell_threshold):
                self.cfg.buy_threshold = buy_threshold
                self.cfg.sell_threshold = sell_threshold

        trader = _make_minimal_trader()
        trader._phase3_evolution = MagicMock()
        trader._phase3_evolution.register_candidate.return_value = SimpleNamespace(
            candidate_id="cand-params-rollout"
        )
        trader._phase3_evolution.list_active.return_value = []
        trader.attributor = MagicMock()
        trader.attributor.get_strategy_evolution_metrics.return_value = {
            "sharpe_30d": 1.4,
            "max_drawdown_30d": 0.03,
            "win_rate_30d": 0.63,
        }
        strategy = DummyStrategy()
        learner = SimpleNamespace(
            trainer=SimpleNamespace(model_type="rf", model_params={"n_estimators": 64}),
            _optimal_buy_threshold=0.72,
            _optimal_sell_threshold=0.28,
        )
        trader._strategies = [strategy]
        trader._continuous_learners = {"ml_BTC_USDT_1h": learner}
        trader._phase3_strategy_candidates = {"ml_BTC_USDT_1h": "cand-model"}
        trader._phase3_strategy_candidate_bindings = {
            "ml_BTC_USDT_1h": {"model": "cand-model"}
        }
        trader._phase3_strategy_metric_bindings = {"ml_BTC_USDT_1h": "model"}
        trader._phase3_params_artifact_signatures = {"ml_BTC_USDT_1h": "old-signature"}
        trader._load_strategy_ml_runtime_artifacts = MagicMock(
            return_value={
                "buy_threshold": 0.64,
                "sell_threshold": 0.36,
                "trainer_model_type": "lgbm",
                "trainer_model_params": {"num_leaves": 31, "learning_rate": 0.05},
                "params_source": "btcusdt_best_params.json",
                "threshold_source": "btcusdt_threshold.json",
            }
        )

        LiveTrader._maybe_rollout_updated_ml_params_candidates(trader)

        assert trader._phase3_strategy_candidates["ml_BTC_USDT_1h"] == "cand-params-rollout"
        assert trader._phase3_strategy_candidate_bindings["ml_BTC_USDT_1h"]["params"] == "cand-params-rollout"
        assert trader._phase3_strategy_metric_bindings["ml_BTC_USDT_1h"] == "params"
        assert strategy.cfg.buy_threshold == pytest.approx(0.64)
        assert strategy.cfg.sell_threshold == pytest.approx(0.36)
        assert learner._optimal_buy_threshold == pytest.approx(0.64)
        assert learner._optimal_sell_threshold == pytest.approx(0.36)
        assert learner.trainer.model_type == "lgbm"
        assert learner.trainer.model_params == {"num_leaves": 31, "learning_rate": 0.05}
        trader._phase3_evolution.update_metrics.assert_called_once_with(
            "cand-params-rollout",
            sharpe_30d=1.4,
            max_drawdown_30d=0.03,
            win_rate_30d=0.63,
        )

    def test_maybe_rollout_updated_ml_params_candidates_skips_unchanged_artifacts(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader._phase3_evolution = MagicMock()
        strategy = SimpleNamespace(
            strategy_id="ml_BTC_USDT_1h",
            symbol="BTC/USDT",
            model=SimpleNamespace(model_type="rf"),
        )
        trader._strategies = [strategy]
        runtime_artifacts = {
            "buy_threshold": 0.64,
            "sell_threshold": 0.36,
            "trainer_model_type": "lgbm",
            "trainer_model_params": {"num_leaves": 31},
            "params_source": "btcusdt_best_params.json",
            "threshold_source": "btcusdt_threshold.json",
        }
        trader._load_strategy_ml_runtime_artifacts = MagicMock(return_value=runtime_artifacts)
        trader._phase3_params_artifact_signatures = {
            "ml_BTC_USDT_1h": LiveTrader._ml_params_artifact_signature(trader, runtime_artifacts)
        }
        trader._register_evolution_params_candidate = MagicMock()

        LiveTrader._maybe_rollout_updated_ml_params_candidates(trader)

        trader._register_evolution_params_candidate.assert_not_called()

    def test_main_loop_step_triggers_params_rollout_watcher(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        event = SimpleNamespace(
            symbol="BTC/USDT",
            close=Decimal("50000"),
            open=Decimal("49500"),
            high=Decimal("50100"),
            low=Decimal("49400"),
            volume=Decimal("100"),
            timestamp=now,
            source="live_feed",
        )
        trader._fetch_latest_klines = MagicMock(return_value=[event])
        trader._cache_kline_event = MagicMock()
        trader._process_kline_event = MagicMock()
        trader._maybe_rollout_updated_ml_params_candidates = MagicMock()
        trader._maybe_start_weekly_ml_params_optimization = MagicMock()
        trader._update_account_snapshot = MagicMock()
        trader._check_stop_loss = MagicMock()
        trader._check_daily_reset = MagicMock()
        trader._run_ai_analysis = MagicMock()
        trader._preload_done = True
        trader._prev_closes = {}
        trader.order_manager.poll_fills.return_value = []
        trader.order_manager.cancel_timed_out_orders.return_value = 0
        trader.attributor = MagicMock()

        LiveTrader._main_loop_step(trader, 1)

        trader._maybe_rollout_updated_ml_params_candidates.assert_called_once_with()
        trader._maybe_start_weekly_ml_params_optimization.assert_called_once()

    def test_maybe_start_weekly_ml_params_optimization_runs_once_per_slot(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader.sys_config.phase3.evolution.weekly_optimization_cron = "0 3 * * 0"
        trader._phase3_params_optimizer_state_store = MagicMock()

        def _start(slot_id):
            trader._phase3_params_optimizer_running = True
            return True

        trader._start_weekly_ml_params_optimization = MagicMock(side_effect=_start)
        slot_time = datetime(2024, 1, 7, 3, 0, tzinfo=timezone.utc)

        LiveTrader._maybe_start_weekly_ml_params_optimization(trader, slot_time)
        LiveTrader._maybe_start_weekly_ml_params_optimization(trader, slot_time)

        expected_slot = slot_time.replace(second=0, microsecond=0).isoformat()
        trader._start_weekly_ml_params_optimization.assert_called_once_with(expected_slot)
        assert trader._phase3_params_optimizer_state["last_attempted_slot"] == expected_slot
        assert trader._phase3_params_optimizer_state["status"] == "running"
        trader._phase3_params_optimizer_state_store.save_scheduler_state.assert_called_once()

    def test_run_weekly_ml_params_optimization_uses_real_ohlcv_dataframe(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader._phase3_params_optimizer_state_store = MagicMock()
        trader.gateway = MagicMock()
        trader._strategies = [
            SimpleNamespace(
                strategy_id="ml_BTC_USDT_1h",
                symbol="BTC/USDT",
                model=SimpleNamespace(model_type="rf"),
            )
        ]

        base_ts = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        trader.gateway.fetch_ohlcv.return_value = [
            [base_ts + idx * 3600_000, 100 + idx, 101 + idx, 99 + idx, 100.5 + idx, 10 + idx]
            for idx in range(901)
        ]

        captured: dict[str, Any] = {}

        def _fake_optimize(df, *, symbol, output_dir, **kwargs):
            captured["df"] = df.copy()
            captured["symbol"] = symbol
            captured["output_dir"] = output_dir
            return {
                "runtime_threshold_path": output_dir / "btcusdt_threshold.json",
                "runtime_params_path": output_dir / "btcusdt_best_params.json",
            }

        with patch(
            "scripts.optimize_phase1_params.optimize_params_from_dataframe",
            side_effect=_fake_optimize,
        ):
            LiveTrader._run_weekly_ml_params_optimization(
                trader,
                "2024-01-07T03:00:00+00:00",
            )

        assert captured["symbol"] == "BTC/USDT"
        assert captured["output_dir"] == Path("models")
        assert len(captured["df"]) == 900
        assert list(captured["df"].columns) == [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "symbol",
        ]
        assert captured["df"]["symbol"].nunique() == 1
        assert captured["df"]["symbol"].iloc[0] == "BTC/USDT"
        assert trader._phase3_params_optimizer_state["last_successful_slot"] == "2024-01-07T03:00:00+00:00"
        assert trader._phase3_params_optimizer_state["status"] == "success"
        assert trader._phase3_params_optimizer_running is False
        trader.gateway.fetch_ohlcv.assert_called_once_with(
            "BTC/USDT",
            timeframe=trader.sys_config.data.default_timeframe,
            limit=1200,
        )

    def test_run_weekly_ml_params_optimization_registers_candidate_directly(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader._phase3_params_optimizer_state_store = MagicMock()
        trader.gateway = MagicMock()
        trader._strategies = [
            SimpleNamespace(
                strategy_id="ml_BTC_USDT_1h",
                symbol="BTC/USDT",
                model=SimpleNamespace(model_type="rf"),
            )
        ]
        trader._continuous_learners = {
            "ml_BTC_USDT_1h": SimpleNamespace(
                trainer=SimpleNamespace(model_type="rf", model_params={"n_estimators": 64})
            )
        }

        base_ts = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        trader.gateway.fetch_ohlcv.return_value = [
            [base_ts + idx * 3600_000, 100 + idx, 101 + idx, 99 + idx, 100.5 + idx, 10 + idx]
            for idx in range(901)
        ]
        trader._load_strategy_ml_runtime_artifacts = MagicMock(
            return_value={
                "buy_threshold": 0.64,
                "sell_threshold": 0.36,
                "trainer_model_type": "lgbm",
                "trainer_model_params": {"num_leaves": 31},
                "params_source": "btcusdt_best_params.json",
                "threshold_source": "btcusdt_threshold.json",
            }
        )
        trader._register_evolution_params_candidate = MagicMock(
            return_value="cand-weekly-params"
        )

        with patch(
            "scripts.optimize_phase1_params.optimize_params_from_dataframe",
            return_value={
                "runtime_threshold_path": Path("models") / "btcusdt_threshold.json",
                "runtime_params_path": Path("models") / "btcusdt_best_params.json",
            },
        ):
            LiveTrader._run_weekly_ml_params_optimization(
                trader,
                "2024-01-07T03:00:00+00:00",
            )

        trader._register_evolution_params_candidate.assert_called_once_with(
            "ml_BTC_USDT_1h",
            trader._continuous_learners["ml_BTC_USDT_1h"],
            trader._load_strategy_ml_runtime_artifacts.return_value,
            source="weekly_optimization",
            set_metric_owner=False,
        )
        optimized_symbols = trader._phase3_params_optimizer_state["optimized_symbols"]
        assert optimized_symbols[0]["candidate_id"] == "cand-weekly-params"

    def test_on_fill_updates_evolution_metrics_for_registered_candidate(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader.gateway = MagicMock()
        trader.gateway.paper_cash = 4800.0
        trader.attributor = MagicMock()
        trader.attributor.get_strategy_evolution_metrics.return_value = {
            "sharpe_30d": 1.25,
            "max_drawdown_30d": 0.04,
            "win_rate_30d": 0.6,
        }
        trader._phase3_evolution = MagicMock()
        trader._phase3_strategy_candidates = {"alpha_a": "cand-alpha-a"}
        trader._current_equity = 5000.0

        fill = SimpleNamespace(
            avg_price=Decimal("50000"),
            new_filled_qty=Decimal("0.01"),
            order_record=SimpleNamespace(
                strategy_id="alpha_a",
                symbol="BTC/USDT",
                side="buy",
                exchange_id="paper-order-1",
            ),
        )

        with patch("apps.trader.main.trade_log"):
            LiveTrader._on_fill(trader, fill)

        trader._phase3_evolution.update_metrics.assert_called_once_with(
            "cand-alpha-a",
            sharpe_30d=1.25,
            max_drawdown_30d=0.04,
            win_rate_30d=0.6,
        )

    def test_on_fill_routes_metrics_to_model_owner_when_params_candidate_also_bound(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader.gateway = MagicMock()
        trader.gateway.paper_cash = 4800.0
        trader.attributor = MagicMock()
        trader.attributor.get_strategy_evolution_metrics.return_value = {
            "sharpe_30d": 1.25,
            "max_drawdown_30d": 0.04,
            "win_rate_30d": 0.6,
        }
        trader._phase3_evolution = MagicMock()
        trader._phase3_strategy_candidates = {"ml_BTC_USDT_1h": "cand-model"}
        trader._phase3_strategy_candidate_bindings = {
            "ml_BTC_USDT_1h": {"model": "cand-model", "params": "cand-params"}
        }
        trader._phase3_strategy_metric_bindings = {"ml_BTC_USDT_1h": "model"}
        trader._current_equity = 5000.0

        fill = SimpleNamespace(
            avg_price=Decimal("50000"),
            new_filled_qty=Decimal("0.01"),
            order_record=SimpleNamespace(
                strategy_id="ml_BTC_USDT_1h",
                symbol="BTC/USDT",
                side="buy",
                exchange_id="paper-order-owner-model",
            ),
        )

        with patch("apps.trader.main.trade_log"):
            LiveTrader._on_fill(trader, fill)

        trader._phase3_evolution.update_metrics.assert_called_once_with(
            "cand-model",
            sharpe_30d=1.25,
            max_drawdown_30d=0.04,
            win_rate_30d=0.6,
        )

    def test_on_fill_routes_metrics_to_params_owner_when_params_slot_is_active(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader.gateway = MagicMock()
        trader.gateway.paper_cash = 4800.0
        trader.attributor = MagicMock()
        trader.attributor.get_strategy_evolution_metrics.return_value = {
            "sharpe_30d": 1.1,
            "max_drawdown_30d": 0.05,
            "win_rate_30d": 0.58,
        }
        trader._phase3_evolution = MagicMock()
        trader._phase3_strategy_candidates = {"ml_BTC_USDT_1h": "cand-params"}
        trader._phase3_strategy_candidate_bindings = {
            "ml_BTC_USDT_1h": {"model": "cand-model", "params": "cand-params"}
        }
        trader._phase3_strategy_metric_bindings = {"ml_BTC_USDT_1h": "params"}
        trader._current_equity = 5000.0

        fill = SimpleNamespace(
            avg_price=Decimal("50000"),
            new_filled_qty=Decimal("0.01"),
            order_record=SimpleNamespace(
                strategy_id="ml_BTC_USDT_1h",
                symbol="BTC/USDT",
                side="buy",
                exchange_id="paper-order-owner-params",
            ),
        )

        with patch("apps.trader.main.trade_log"):
            LiveTrader._on_fill(trader, fill)

        trader._phase3_evolution.update_metrics.assert_called_once_with(
            "cand-params",
            sharpe_30d=1.1,
            max_drawdown_30d=0.05,
            win_rate_30d=0.58,
        )

    def test_register_evolution_policy_candidate_promotes_initial_baseline(self):
        from apps.trader.main import LiveTrader
        from modules.alpha.contracts.evolution_types import CandidateStatus

        trader = _make_minimal_trader()
        trader._phase3_evolution = MagicMock()
        trader._phase3_evolution.register_candidate.return_value = SimpleNamespace(
            candidate_id="cand-ppo-v1"
        )
        trader._phase3_evolution.list_active.return_value = []
        trader.attributor = MagicMock()
        trader.attributor.get_strategy_evolution_metrics.return_value = {}
        trader._phase3_rl_policy_mode = "paper"

        policy = SimpleNamespace(
            version=lambda: "ppo_v1_test",
            config=SimpleNamespace(obs_dim=24, n_actions=8),
        )

        candidate_id = LiveTrader._register_evolution_policy_candidate(
            trader,
            policy,
            source="phase3_init",
        )

        assert candidate_id == "cand-ppo-v1"
        trader._phase3_evolution.force_promote.assert_called_once_with(
            "cand-ppo-v1",
            CandidateStatus.ACTIVE,
            reason="INITIAL_RUNTIME_BASELINE",
        )

    def test_register_evolution_policy_candidate_binds_phase3_strategy_id(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader._phase3_evolution = MagicMock()
        trader._phase3_evolution.register_candidate.return_value = SimpleNamespace(
            candidate_id="cand-ppo-v1"
        )
        trader.attributor = MagicMock()
        trader.attributor.get_strategy_evolution_metrics.return_value = {}
        trader._phase3_evolution.list_active.return_value = []
        trader._phase3_rl_policy_mode = "paper"

        policy = SimpleNamespace(
            version=lambda: "ppo_v1_test",
            config=SimpleNamespace(obs_dim=24, n_actions=8),
        )

        candidate_id = LiveTrader._register_evolution_policy_candidate(
            trader,
            policy,
            source="phase3_init",
        )

        assert candidate_id == "cand-ppo-v1"
        trader._phase3_evolution.register_candidate.assert_called_once()
        args = trader._phase3_evolution.register_candidate.call_args.args
        kwargs = trader._phase3_evolution.register_candidate.call_args.kwargs
        assert args[0].value == "policy"
        assert kwargs["owner"] == "rl/ppo"
        assert kwargs["version"] == "ppo_v1_test"
        assert kwargs["metadata"]["strategy_id"] == "phase3_rl_ppo_v1_test"
        assert trader._phase3_strategy_candidates["phase3_rl_ppo_v1_test"] == "cand-ppo-v1"
        assert trader._phase3_candidate_runtime_state["cand-ppo-v1"]["policy"] is policy

    def test_register_evolution_market_making_candidate_binds_phase3_strategy_id(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader._phase3_evolution = MagicMock()
        trader._phase3_evolution.register_candidate.return_value = SimpleNamespace(
            candidate_id="cand-mm-v1"
        )
        trader._phase3_evolution.list_active.return_value = []
        mm_strategy = SimpleNamespace(
            config=SimpleNamespace(
                symbol="BTC/USDT",
                exchange="htx",
                paper_mode=True,
                avellaneda=SimpleNamespace(gamma=0.12),
                inventory=SimpleNamespace(max_inventory_pct=0.20),
            )
        )

        candidate_id = LiveTrader._register_evolution_market_making_candidate(
            trader,
            mm_strategy,
            source="phase3_init",
        )

        assert candidate_id == "cand-mm-v1"
        trader._phase3_evolution.register_candidate.assert_called_once()
        args = trader._phase3_evolution.register_candidate.call_args.args
        kwargs = trader._phase3_evolution.register_candidate.call_args.kwargs
        assert args[0].value == "strategy"
        assert kwargs["owner"] == "market_making/avellaneda"
        assert kwargs["metadata"]["strategy_id"] == "phase3_mm_btcusdt"
        assert trader._phase3_strategy_candidates["phase3_mm_btcusdt"] == "cand-mm-v1"
        assert trader._phase3_candidate_runtime_state["cand-mm-v1"]["strategy"] is mm_strategy

    def test_bootstrap_evolution_ab_experiment_uses_market_making_realized_pnl_history(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader._phase3_evolution = MagicMock()
        trader._phase3_evolution.config = SimpleNamespace(
            ab_test=SimpleNamespace(min_samples=3)
        )
        trader._phase3_evolution.create_ab_experiment.return_value = "ab-mm-1"
        ts0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        trader._phase3_mm_realized_trade_records = {
            "phase3_mm_btcusdt": [
                {"timestamp": ts0, "pnl": 1.0},
                {"timestamp": ts0 + timedelta(minutes=1), "pnl": -0.5},
                {"timestamp": ts0 + timedelta(minutes=2), "pnl": 0.75},
            ]
        }
        control_candidate = SimpleNamespace(
            candidate_id="cand-mm-base",
            metadata={"strategy_id": "phase3_mm_btcusdt"},
        )

        experiment_id = LiveTrader._bootstrap_evolution_ab_experiment(
            trader,
            control_candidate=control_candidate,
            test_candidate_id="cand-mm-new",
            test_strategy_id="phase3_mm_btcusdt_v2",
        )

        assert experiment_id == "ab-mm-1"
        trader._phase3_evolution.create_ab_experiment.assert_called_once_with(
            "cand-mm-base",
            "cand-mm-new",
        )
        assert trader._phase3_evolution.record_ab_step.call_count == 3
        assert trader._phase3_candidate_experiments["cand-mm-new"] == "ab-mm-1"

    def test_record_market_making_evolution_feedback_updates_metrics_and_ab_step(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader._phase3_evolution = MagicMock()
        trader._phase3_evolution.ab_experiment_status.return_value = {
            "has_sufficient_samples": False
        }
        trader._phase3_mm = MagicMock()
        trader._phase3_mm.diagnostics.return_value = {
            "inventory": {
                "realized_pnl": 8.0,
                "inventory_pct": 0.42,
                "total_trades": 6,
            }
        }
        ts0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        trader._phase3_strategy_candidates = {"phase3_mm_btcusdt": "cand-mm"}
        trader._phase3_candidate_experiments = {"cand-mm": "ab-mm-1"}
        trader._phase3_mm_last_realized_pnl = {"phase3_mm_btcusdt": 5.0}
        trader._phase3_mm_realized_trade_records = {
            "phase3_mm_btcusdt": [
                {"timestamp": ts0, "pnl": 1.0},
                {"timestamp": ts0 + timedelta(minutes=1), "pnl": -0.5},
                {"timestamp": ts0 + timedelta(minutes=2), "pnl": 0.75},
                {"timestamp": ts0 + timedelta(minutes=3), "pnl": 0.25},
            ]
        }

        LiveTrader._record_market_making_evolution_feedback(
            trader,
            "phase3_mm_btcusdt",
            SimpleNamespace(reason_codes=[]),
        )

        trader._phase3_evolution.update_metrics.assert_called_once()
        update_args = trader._phase3_evolution.update_metrics.call_args.args
        update_kwargs = trader._phase3_evolution.update_metrics.call_args.kwargs
        assert update_args == ("cand-mm",)
        assert update_kwargs["sharpe_30d"] is not None
        assert update_kwargs["max_drawdown_30d"] >= 0.0
        assert 0.0 <= update_kwargs["win_rate_30d"] <= 1.0
        trader._phase3_evolution.record_ab_step.assert_called_once()
        ab_kwargs = trader._phase3_evolution.record_ab_step.call_args.kwargs
        assert ab_kwargs["is_test"] is True
        assert ab_kwargs["step_pnl"] == pytest.approx(3.0)
        assert trader._phase3_mm_last_realized_pnl["phase3_mm_btcusdt"] == pytest.approx(8.0)
        assert len(trader._phase3_mm_realized_trade_records["phase3_mm_btcusdt"]) == 5

    def test_step_phase3_shadow_mm_halt_records_single_evolution_risk_violation(self):
        trader = _make_minimal_trader()
        trader._phase3_enabled = True
        trader._phase3_ppo = None
        trader._phase3_evolution = MagicMock()
        trader._phase3_strategy_candidates = {"phase3_mm_btcusdt": "cand-mm"}

        mm_mock = MagicMock()
        mm_mock.config.symbol = "BTC/USDT"
        mm_mock.tick.return_value = SimpleNamespace(
            bid_price=None,
            ask_price=None,
            allow_post_bid=False,
            allow_post_ask=False,
            reason_codes=["INVENTORY_HALT"],
        )
        mm_mock.diagnostics.return_value = {"inventory": {"realized_pnl": 0.0}}
        trader._phase3_mm = mm_mock

        trader._step_phase3_shadow("BTC/USDT", 50000.0, seq=1)
        trader._step_phase3_shadow("BTC/USDT", 50000.0, seq=2)

        trader._phase3_evolution.record_risk_violation.assert_called_once_with("cand-mm", n=1)

    def test_on_fill_sell_records_ab_step_and_concludes_experiment(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader.gateway = MagicMock()
        trader.gateway.paper_cash = 4800.0
        trader.attributor = MagicMock()
        trader.attributor.get_strategy_evolution_metrics.return_value = {
            "sharpe_30d": 1.25,
            "max_drawdown_30d": 0.04,
            "win_rate_30d": 0.6,
        }
        trader._phase3_evolution = MagicMock()
        trader._phase3_evolution.ab_experiment_status.return_value = {
            "has_sufficient_samples": True
        }
        trader._phase3_evolution.conclude_ab_experiment.return_value = SimpleNamespace(
            lift=3.5
        )
        trader._phase3_strategy_candidates = {"alpha_a": "cand-alpha-a"}
        trader._phase3_candidate_experiments = {"cand-alpha-a": "ab-alpha-a"}
        trader._current_equity = 5000.0
        trader._entry_prices = {"BTC/USDT": 49000.0}
        trader._positions = {"BTC/USDT": Decimal("0.01")}

        fill = SimpleNamespace(
            avg_price=Decimal("50000"),
            new_filled_qty=Decimal("0.01"),
            order_record=SimpleNamespace(
                strategy_id="alpha_a",
                symbol="BTC/USDT",
                side="sell",
                exchange_id="paper-order-2",
            ),
        )

        with patch("apps.trader.main.trade_log"):
            LiveTrader._on_fill(trader, fill)

        trader._phase3_evolution.record_ab_step.assert_called_once()
        kwargs = trader._phase3_evolution.record_ab_step.call_args.kwargs
        assert kwargs["is_test"] is True
        assert kwargs["step_pnl"] == pytest.approx(9.5)
        trader._phase3_evolution.conclude_ab_experiment.assert_called_once_with("ab-alpha-a")
        assert "cand-alpha-a" not in trader._phase3_candidate_experiments

    def test_apply_evolution_runtime_state_restores_active_ml_candidate(self):
        from apps.trader.main import LiveTrader

        class DummyStrategy:
            def __init__(self, strategy_id, model, buy_threshold, sell_threshold):
                self.strategy_id = strategy_id
                self.model = model
                self.cfg = SimpleNamespace(
                    buy_threshold=buy_threshold,
                    sell_threshold=sell_threshold,
                )

            def set_thresholds(self, buy_threshold, sell_threshold):
                self.cfg.buy_threshold = buy_threshold
                self.cfg.sell_threshold = sell_threshold

        trader = _make_minimal_trader()
        base_model = SimpleNamespace(model_type="rf")
        challenger_model = SimpleNamespace(model_type="lgbm")
        strategy = DummyStrategy("ml_BTC_USDT_1h", challenger_model, 0.66, 0.34)
        trader._strategies = [strategy]
        trader._phase3_strategy_candidates = {"ml_BTC_USDT_1h": "cand-new"}
        trader._phase3_candidate_runtime_state = {
            "cand-base": {
                "strategy_id": "ml_BTC_USDT_1h",
                "model": base_model,
                "buy_threshold": 0.61,
                "sell_threshold": 0.39,
            },
            "cand-new": {
                "strategy_id": "ml_BTC_USDT_1h",
                "model": challenger_model,
                "buy_threshold": 0.66,
                "sell_threshold": 0.34,
            },
        }
        report = SimpleNamespace(
            active_snapshot=[
                SimpleNamespace(
                    candidate_id="cand-base",
                    metadata={"strategy_id": "ml_BTC_USDT_1h"},
                )
            ]
        )

        LiveTrader._apply_evolution_runtime_state(trader, report)

        assert trader._phase3_strategy_candidates["ml_BTC_USDT_1h"] == "cand-base"
        assert strategy.model is base_model
        assert strategy.cfg.buy_threshold == pytest.approx(0.61)
        assert strategy.cfg.sell_threshold == pytest.approx(0.39)

    def test_apply_evolution_runtime_state_restores_active_params_candidate(self):
        from apps.trader.main import LiveTrader

        class DummyStrategy:
            def __init__(self, strategy_id, model, buy_threshold, sell_threshold):
                self.strategy_id = strategy_id
                self.model = model
                self.cfg = SimpleNamespace(
                    buy_threshold=buy_threshold,
                    sell_threshold=sell_threshold,
                )

            def set_thresholds(self, buy_threshold, sell_threshold):
                self.cfg.buy_threshold = buy_threshold
                self.cfg.sell_threshold = sell_threshold

        trader = _make_minimal_trader()
        strategy = DummyStrategy(
            "ml_BTC_USDT_1h",
            SimpleNamespace(model_type="rf"),
            0.72,
            0.28,
        )
        learner = SimpleNamespace(
            trainer=SimpleNamespace(model_type="rf", model_params={"n_estimators": 64}),
            _optimal_buy_threshold=0.72,
            _optimal_sell_threshold=0.28,
        )
        trader._strategies = [strategy]
        trader._continuous_learners = {"ml_BTC_USDT_1h": learner}
        trader._phase3_strategy_candidates = {"ml_BTC_USDT_1h": "cand-params-new"}
        trader._phase3_strategy_candidate_bindings = {
            "ml_BTC_USDT_1h": {
                "model": "cand-model-new",
                "params": "cand-params-new",
            }
        }
        trader._phase3_strategy_metric_bindings = {"ml_BTC_USDT_1h": "params"}
        trader._phase3_candidate_runtime_state = {
            "cand-params-base": {
                "strategy_id": "ml_BTC_USDT_1h",
                "runtime_kind": "ml_params",
                "binding_slot": "params",
                "buy_threshold": 0.61,
                "sell_threshold": 0.39,
                "trainer_model_type": "lgbm",
                "trainer_model_params": {"num_leaves": 15},
            },
            "cand-params-new": {
                "strategy_id": "ml_BTC_USDT_1h",
                "runtime_kind": "ml_params",
                "binding_slot": "params",
                "buy_threshold": 0.72,
                "sell_threshold": 0.28,
                "trainer_model_type": "rf",
                "trainer_model_params": {"n_estimators": 64},
            },
        }
        report = SimpleNamespace(
            active_snapshot=[
                SimpleNamespace(
                    candidate_id="cand-params-base",
                    metadata={
                        "strategy_id": "ml_BTC_USDT_1h",
                        "binding_slot": "params",
                    },
                )
            ]
        )

        LiveTrader._apply_evolution_runtime_state(trader, report)

        assert trader._phase3_strategy_candidates["ml_BTC_USDT_1h"] == "cand-params-base"
        assert (
            trader._phase3_strategy_candidate_bindings["ml_BTC_USDT_1h"]["model"]
            == "cand-model-new"
        )
        assert (
            trader._phase3_strategy_candidate_bindings["ml_BTC_USDT_1h"]["params"]
            == "cand-params-base"
        )
        assert strategy.cfg.buy_threshold == pytest.approx(0.61)
        assert strategy.cfg.sell_threshold == pytest.approx(0.39)
        assert learner._optimal_buy_threshold == pytest.approx(0.61)
        assert learner._optimal_sell_threshold == pytest.approx(0.39)
        assert learner.trainer.model_type == "lgbm"
        assert learner.trainer.model_params == {"num_leaves": 15}

    def test_apply_evolution_runtime_state_promotes_params_candidate_to_production_owner(self):
        from apps.trader.main import LiveTrader

        class DummyStrategy:
            def __init__(self, strategy_id, model, buy_threshold, sell_threshold):
                self.strategy_id = strategy_id
                self.model = model
                self.cfg = SimpleNamespace(
                    buy_threshold=buy_threshold,
                    sell_threshold=sell_threshold,
                )

            def set_thresholds(self, buy_threshold, sell_threshold):
                self.cfg.buy_threshold = buy_threshold
                self.cfg.sell_threshold = sell_threshold

        trader = _make_minimal_trader()
        strategy = DummyStrategy(
            "ml_BTC_USDT_1h",
            SimpleNamespace(model_type="rf"),
            0.61,
            0.39,
        )
        learner = SimpleNamespace(
            trainer=SimpleNamespace(model_type="rf", model_params={"n_estimators": 32}),
            _optimal_buy_threshold=0.61,
            _optimal_sell_threshold=0.39,
        )
        trader._strategies = [strategy]
        trader._continuous_learners = {"ml_BTC_USDT_1h": learner}
        trader._phase3_strategy_candidates = {"ml_BTC_USDT_1h": "cand-model-active"}
        trader._phase3_strategy_candidate_bindings = {
            "ml_BTC_USDT_1h": {
                "model": "cand-model-active",
                "params": "cand-params-active",
            }
        }
        trader._phase3_strategy_metric_bindings = {"ml_BTC_USDT_1h": "model"}
        trader._phase3_candidate_runtime_state = {
            "cand-model-active": {
                "strategy_id": "ml_BTC_USDT_1h",
                "runtime_kind": "ml_model",
                "binding_slot": "model",
                "model": strategy.model,
                "buy_threshold": 0.61,
                "sell_threshold": 0.39,
            },
            "cand-params-active": {
                "strategy_id": "ml_BTC_USDT_1h",
                "runtime_kind": "ml_params",
                "binding_slot": "params",
                "buy_threshold": 0.72,
                "sell_threshold": 0.28,
                "trainer_model_type": "lgbm",
                "trainer_model_params": {"num_leaves": 31},
            },
        }
        report = SimpleNamespace(
            decisions=[
                SimpleNamespace(candidate_id="cand-params-active", action="promote")
            ],
            active_snapshot=[
                SimpleNamespace(
                    candidate_id="cand-model-active",
                    metadata={
                        "strategy_id": "ml_BTC_USDT_1h",
                        "binding_slot": "model",
                    },
                ),
                SimpleNamespace(
                    candidate_id="cand-params-active",
                    metadata={
                        "strategy_id": "ml_BTC_USDT_1h",
                        "binding_slot": "params",
                    },
                ),
            ],
        )

        LiveTrader._apply_evolution_runtime_state(trader, report)

        assert trader._phase3_strategy_metric_bindings["ml_BTC_USDT_1h"] == "params"
        assert trader._phase3_strategy_candidates["ml_BTC_USDT_1h"] == "cand-params-active"
        assert strategy.cfg.buy_threshold == pytest.approx(0.72)
        assert strategy.cfg.sell_threshold == pytest.approx(0.28)
        assert learner._optimal_buy_threshold == pytest.approx(0.72)
        assert learner._optimal_sell_threshold == pytest.approx(0.28)
        assert learner.trainer.model_type == "lgbm"
        assert learner.trainer.model_params == {"num_leaves": 31}

    def test_apply_evolution_runtime_state_rolls_back_params_owner_to_model_snapshot(self):
        from apps.trader.main import LiveTrader

        class DummyStrategy:
            def __init__(self, strategy_id, model, buy_threshold, sell_threshold):
                self.strategy_id = strategy_id
                self.model = model
                self.cfg = SimpleNamespace(
                    buy_threshold=buy_threshold,
                    sell_threshold=sell_threshold,
                )

            def set_thresholds(self, buy_threshold, sell_threshold):
                self.cfg.buy_threshold = buy_threshold
                self.cfg.sell_threshold = sell_threshold

        trader = _make_minimal_trader()
        base_model = SimpleNamespace(model_type="rf")
        strategy = DummyStrategy(
            "ml_BTC_USDT_1h",
            base_model,
            0.72,
            0.28,
        )
        trader._strategies = [strategy]
        trader._phase3_strategy_candidates = {"ml_BTC_USDT_1h": "cand-params-active"}
        trader._phase3_strategy_candidate_bindings = {
            "ml_BTC_USDT_1h": {
                "model": "cand-model-active",
                "params": "cand-params-active",
            }
        }
        trader._phase3_strategy_metric_bindings = {"ml_BTC_USDT_1h": "params"}
        trader._phase3_candidate_runtime_state = {
            "cand-model-active": {
                "strategy_id": "ml_BTC_USDT_1h",
                "runtime_kind": "ml_model",
                "binding_slot": "model",
                "model": base_model,
                "buy_threshold": 0.61,
                "sell_threshold": 0.39,
            },
            "cand-params-active": {
                "strategy_id": "ml_BTC_USDT_1h",
                "runtime_kind": "ml_params",
                "binding_slot": "params",
                "buy_threshold": 0.72,
                "sell_threshold": 0.28,
                "trainer_model_type": "lgbm",
                "trainer_model_params": {"num_leaves": 31},
            },
        }
        report = SimpleNamespace(
            decisions=[
                SimpleNamespace(candidate_id="cand-params-active", action="rollback")
            ],
            active_snapshot=[
                SimpleNamespace(
                    candidate_id="cand-model-active",
                    metadata={
                        "strategy_id": "ml_BTC_USDT_1h",
                        "binding_slot": "model",
                    },
                )
            ],
        )

        LiveTrader._apply_evolution_runtime_state(trader, report)

        assert trader._phase3_strategy_metric_bindings["ml_BTC_USDT_1h"] == "model"
        assert trader._phase3_strategy_candidates["ml_BTC_USDT_1h"] == "cand-model-active"
        assert strategy.model is base_model
        assert strategy.cfg.buy_threshold == pytest.approx(0.61)
        assert strategy.cfg.sell_threshold == pytest.approx(0.39)

    def test_apply_evolution_runtime_state_keeps_model_owner_without_params_transition(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader._phase3_strategy_candidates = {"ml_BTC_USDT_1h": "cand-model-active"}
        trader._phase3_strategy_candidate_bindings = {
            "ml_BTC_USDT_1h": {
                "model": "cand-model-active",
                "params": "cand-params-baseline",
            }
        }
        trader._phase3_strategy_metric_bindings = {"ml_BTC_USDT_1h": "model"}
        report = SimpleNamespace(
            decisions=[],
            active_snapshot=[
                SimpleNamespace(
                    candidate_id="cand-model-active",
                    metadata={
                        "strategy_id": "ml_BTC_USDT_1h",
                        "binding_slot": "model",
                    },
                ),
                SimpleNamespace(
                    candidate_id="cand-params-baseline",
                    metadata={
                        "strategy_id": "ml_BTC_USDT_1h",
                        "binding_slot": "params",
                    },
                ),
            ],
        )

        LiveTrader._apply_evolution_runtime_state(trader, report)

        assert trader._phase3_strategy_metric_bindings["ml_BTC_USDT_1h"] == "model"
        assert trader._phase3_strategy_candidates["ml_BTC_USDT_1h"] == "cand-model-active"

    def test_apply_evolution_runtime_state_restores_active_rl_policy_candidate(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        base_policy = SimpleNamespace(version=lambda: "ppo_v1_base")
        challenger_policy = SimpleNamespace(version=lambda: "ppo_v1_new")
        trader._phase3_ppo = challenger_policy
        trader._phase3_rl_policy_mode = "paper"
        trader._phase3_strategy_candidates = {"phase3_rl_ppo_v1_base": "cand-new"}
        trader._phase3_candidate_runtime_state = {
            "cand-base": {
                "strategy_id": "phase3_rl_ppo_v1_base",
                "runtime_kind": "rl_policy",
                "policy": base_policy,
                "policy_mode": "active",
            },
            "cand-new": {
                "strategy_id": "phase3_rl_ppo_v1_base",
                "runtime_kind": "rl_policy",
                "policy": challenger_policy,
                "policy_mode": "paper",
            },
        }
        report = SimpleNamespace(
            active_snapshot=[
                SimpleNamespace(
                    candidate_id="cand-base",
                    metadata={"strategy_id": "phase3_rl_ppo_v1_base"},
                )
            ]
        )

        LiveTrader._apply_evolution_runtime_state(trader, report)

        assert trader._phase3_strategy_candidates["phase3_rl_ppo_v1_base"] == "cand-base"
        assert trader._phase3_ppo is base_policy
        assert trader._phase3_rl_policy_mode == "active"

    def test_apply_evolution_runtime_state_restores_active_market_making_candidate(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        base_mm = SimpleNamespace(config=SimpleNamespace(symbol="BTC/USDT"))
        challenger_mm = SimpleNamespace(config=SimpleNamespace(symbol="BTC/USDT"))
        trader._phase3_mm = challenger_mm
        trader._phase3_strategy_candidates = {"phase3_mm_btcusdt": "cand-new"}
        trader._phase3_candidate_runtime_state = {
            "cand-base": {
                "strategy_id": "phase3_mm_btcusdt",
                "runtime_kind": "market_making",
                "strategy": base_mm,
            },
            "cand-new": {
                "strategy_id": "phase3_mm_btcusdt",
                "runtime_kind": "market_making",
                "strategy": challenger_mm,
            },
        }
        report = SimpleNamespace(
            active_snapshot=[
                SimpleNamespace(
                    candidate_id="cand-base",
                    metadata={"strategy_id": "phase3_mm_btcusdt"},
                )
            ]
        )

        LiveTrader._apply_evolution_runtime_state(trader, report)

        assert trader._phase3_strategy_candidates["phase3_mm_btcusdt"] == "cand-base"
        assert trader._phase3_mm is base_mm

    def test_maybe_start_weekly_ml_params_optimization_uses_evolution_engine_scheduler(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        slot_time = datetime(2024, 1, 7, 3, 0, tzinfo=timezone.utc)
        slot_id = "2024-01-07T03:00:00+00:00"
        trader._phase3_evolution = MagicMock()
        trader._phase3_evolution.get_due_weekly_params_optimizer_slot.return_value = slot_id
        trader._phase3_evolution.weekly_params_optimizer_state.return_value = {}
        trader._phase3_evolution.record_weekly_params_optimizer_start.return_value = {
            "last_attempted_slot": slot_id,
            "status": "running",
        }
        trader._start_weekly_ml_params_optimization = MagicMock(return_value=True)

        LiveTrader._maybe_start_weekly_ml_params_optimization(trader, slot_time)

        trader._phase3_evolution.get_due_weekly_params_optimizer_slot.assert_called_once_with(
            slot_time
        )
        trader._phase3_evolution.record_weekly_params_optimizer_start.assert_called_once_with(
            slot_id,
            now=slot_time,
        )
        trader._start_weekly_ml_params_optimization.assert_called_once_with(slot_id)
        assert trader._phase3_params_optimizer_state["status"] == "running"

    def test_trigger_weekly_ml_params_optimization_starts_manual_slot(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader._phase3_evolution = MagicMock()
        trader._phase3_evolution.record_weekly_params_optimizer_start.return_value = {
            "status": "running",
        }
        trader._start_weekly_ml_params_optimization = MagicMock(return_value=True)

        result = LiveTrader.trigger_weekly_ml_params_optimization(trader)

        assert result["ok"] is True
        assert str(result["slot_id"]).startswith("manual:")
        trader._phase3_evolution.record_weekly_params_optimizer_start.assert_called_once()
        trader._start_weekly_ml_params_optimization.assert_called_once()

    def test_register_evolution_strategy_params_candidate_for_non_ml_strategy(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        strategy = SimpleNamespace(
            strategy_id="ma_cross_10_BTC_USDT",
            fast_window=10,
            slow_window=30,
            order_qty=0.25,
            use_ema=True,
            adx_filter=False,
            adx_entry_threshold=25.0,
            adx_close_threshold=18.0,
            volume_filter=True,
            vol_ma_window=20,
            vol_multiplier=1.5,
            timeframe="1h",
        )
        trader._strategies = [strategy]
        trader._phase3_evolution = MagicMock()
        trader._phase3_evolution.register_candidate.return_value = SimpleNamespace(
            candidate_id="cand-strategy-params"
        )
        trader._phase3_evolution.get_candidate.return_value = None
        trader._phase3_evolution.list_active.return_value = []

        candidate_id = LiveTrader._register_evolution_strategy_params_candidate(
            trader,
            strategy,
            source="initial_load",
        )

        assert candidate_id == "cand-strategy-params"
        metadata = trader._phase3_evolution.register_candidate.call_args.kwargs["metadata"]
        assert metadata["params_kind"] == "strategy_params"
        assert metadata["strategy_id"] == "ma_cross_10_BTC_USDT"
        assert (
            trader._phase3_candidate_runtime_state["cand-strategy-params"]["runtime_kind"]
            == "strategy_params"
        )
        assert trader._phase3_strategy_metric_bindings["ma_cross_10_BTC_USDT"] == "params"
        trader._phase3_evolution.force_promote.assert_called_once()

    def test_apply_evolution_runtime_state_restores_non_ml_strategy_params_candidate(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        strategy = SimpleNamespace(
            strategy_id="ma_cross_10_BTC_USDT",
            fast_window=7,
            slow_window=21,
            order_qty=0.1,
            use_ema=False,
            adx_filter=True,
            adx_entry_threshold=20.0,
            adx_close_threshold=15.0,
            volume_filter=False,
            vol_ma_window=10,
            vol_multiplier=1.1,
            timeframe="15m",
        )
        trader._strategies = [strategy]
        trader._phase3_strategy_candidates = {"ma_cross_10_BTC_USDT": "cand-current"}
        trader._phase3_strategy_candidate_bindings = {
            "ma_cross_10_BTC_USDT": {"params": "cand-current"}
        }
        trader._phase3_strategy_metric_bindings = {"ma_cross_10_BTC_USDT": "params"}
        trader._phase3_candidate_runtime_state = {
            "cand-baseline": {
                "strategy_id": "ma_cross_10_BTC_USDT",
                "runtime_kind": "strategy_params",
                "binding_slot": "params",
                "params_payload": {
                    "fast_window": 12,
                    "slow_window": 48,
                    "order_qty": 0.35,
                    "use_ema": True,
                    "adx_filter": False,
                    "adx_entry_threshold": 28.0,
                    "adx_close_threshold": 19.0,
                    "volume_filter": True,
                    "vol_ma_window": 30,
                    "vol_multiplier": 1.8,
                    "timeframe": "1h",
                },
            },
            "cand-current": {
                "strategy_id": "ma_cross_10_BTC_USDT",
                "runtime_kind": "strategy_params",
                "binding_slot": "params",
                "params_payload": {"fast_window": 7},
            },
        }
        report = SimpleNamespace(
            decisions=[],
            active_snapshot=[
                SimpleNamespace(
                    candidate_id="cand-baseline",
                    metadata={
                        "strategy_id": "ma_cross_10_BTC_USDT",
                        "binding_slot": "params",
                    },
                )
            ],
        )

        LiveTrader._apply_evolution_runtime_state(trader, report)

        assert trader._phase3_strategy_candidates["ma_cross_10_BTC_USDT"] == "cand-baseline"
        assert strategy.fast_window == 12
        assert strategy.slow_window == 48
        assert strategy.order_qty == pytest.approx(0.35)
        assert strategy.use_ema is True
        assert strategy.timeframe == "1h"

    def test_apply_evolution_runtime_state_restores_risk_params_candidate(self):
        from apps.trader.main import LiveTrader

        trader = _make_minimal_trader()
        trader._phase3_candidate_runtime_state = {
            "cand-risk-base": {
                "strategy_id": "phase2_risk_runtime",
                "runtime_kind": "risk_params",
                "binding_slot": "params",
                "params_payload": {
                    "risk_manager": {
                        "max_position_pct": 0.12,
                        "max_portfolio_drawdown": 0.18,
                        "max_daily_loss": 0.04,
                        "max_consecutive_losses": 5,
                        "circuit_breaker_cooldown_minutes": 45,
                    },
                    "adaptive_risk": {
                        "max_drawdown_for_entry": 0.1,
                        "max_daily_loss_for_entry": 0.03,
                        "drawdown_scalar_per_pct": 0.8,
                        "drawdown_scalar_floor": 0.4,
                        "low_confidence_threshold": 0.35,
                        "high_confidence_threshold": 0.7,
                        "low_confidence_scalar": 0.6,
                        "high_confidence_scalar": 1.3,
                        "high_vol_scalar": 0.75,
                        "unknown_regime_scalar": 0.65,
                        "max_position_scalar": 0.9,
                        "default_cooldown_minutes": 20,
                    },
                    "budget_checker": {
                        "max_budget_usage_pct": 0.55,
                        "max_single_order_budget_pct": 0.12,
                        "fee_reserve_pct": 0.005,
                        "slippage_reserve_pct": 0.01,
                        "dca_budget_cap_pct": 0.2,
                        "min_order_budget_pct": 0.02,
                        "intraday_budget_cap_pct": 0.35,
                    },
                    "kill_switch": {
                        "drawdown_trigger": 0.22,
                        "daily_loss_trigger": 0.05,
                        "max_consecutive_rejections": 4,
                        "max_consecutive_failures": 3,
                        "stale_data_timeout_sec": 45,
                        "stale_sources_trigger_count": 2,
                        "auto_recover_minutes": 12,
                    },
                    "position_sizer": {"max_position_pct": 0.14},
                },
            }
        }
        report = SimpleNamespace(
            decisions=[],
            active_snapshot=[
                SimpleNamespace(
                    candidate_id="cand-risk-base",
                    metadata={
                        "strategy_id": "phase2_risk_runtime",
                        "binding_slot": "params",
                    },
                )
            ],
        )

        LiveTrader._apply_evolution_runtime_state(trader, report)

        assert trader.risk_manager.config.max_position_pct == pytest.approx(0.12)
        assert trader.risk_manager.config.max_daily_loss == pytest.approx(0.04)
        assert trader._adaptive_risk.config.high_confidence_scalar == pytest.approx(1.3)
        assert trader._budget_checker.config.max_budget_usage_pct == pytest.approx(0.55)
        assert trader._kill_switch.config.drawdown_trigger == pytest.approx(0.22)
        assert trader.position_sizer._max_position_pct == Decimal("0.14")


# ─────────────────────────────────────────────────────────────
# 辅助工厂函数
# ─────────────────────────────────────────────────────────────

def _make_minimal_trader():
    """
    构造一个仅有 Phase 3 相关属性的极简 LiveTrader 实例（无真实 CCXT）。
    """
    from apps.trader.main import LiveTrader
    from modules.alpha.contracts import RegimeState
    from modules.data.fusion.alignment import AlignmentConfig, SourceAligner
    from modules.risk.adaptive_matrix import AdaptiveRiskMatrixConfig
    from modules.risk.budget_checker import BudgetConfig
    from modules.risk.kill_switch import KillSwitchConfig
    from modules.risk.manager import RiskConfig
    from modules.risk.position_sizer import PositionSizer

    obj = object.__new__(LiveTrader)

    # 最小必要属性
    from core.config import load_config
    obj.sys_config = load_config()
    obj.mode = "paper"
    obj._poll_interval_s = 60.0

    obj._phase3_enabled = False
    obj._phase3_mm = None
    obj._phase3_ppo = None
    obj._phase3_evolution = None
    obj._phase3_obs_builder = None
    obj._phase3_action_adapter = None
    obj._phase3_rl_policy_mode = "shadow"
    obj._phase3_realtime_enabled = False
    obj._phase3_ws_client = None
    obj._phase3_subscription_manager = None
    obj._phase3_depth_registry = None
    obj._phase3_trade_registry = None
    obj._phase3_micro_builder = None
    obj._phase3_strategy_candidates = {}
    obj._phase3_strategy_candidate_bindings = {}
    obj._phase3_strategy_metric_bindings = {}
    obj._phase3_params_artifact_signatures = {}
    obj._phase3_params_optimizer_state_store = None
    obj._phase3_params_optimizer_state = {}
    obj._phase3_params_optimizer_thread = None
    obj._phase3_params_optimizer_running = False
    obj._phase3_candidate_experiments = {}
    obj._phase3_candidate_runtime_state = {}
    obj._phase3_mm_realized_trade_records = {}
    obj._phase3_mm_last_realized_pnl = {}
    obj._phase3_mm_last_halt_reason = {}

    obj._phase1_feature_views = {}
    obj._phase1_data_kitchens = {}
    obj._phase1_regime_detectors = {}
    obj._phase1_orchestrator = MagicMock()
    obj._portfolio_enabled = False
    obj.allocator = None
    obj.rebalancer = None
    obj._phase2_source_aligner = SourceAligner(AlignmentConfig(max_fill_periods=72))
    obj._phase2_external_enabled = False
    obj._onchain_collector = None
    obj._onchain_feature_builder = None
    obj._sentiment_collector = None
    obj._sentiment_feature_builder = None
    obj._symbol_regimes = {}
    obj._last_trace_ids = {}
    obj._symbol_risk_plans = {}

    obj._positions = {}
    obj._entry_prices = {}
    obj._latest_prices = {}
    obj._kline_store = {}
    obj._current_equity = 0.0
    obj._stop_loss_pending = set()
    obj._current_regime = RegimeState(
        bull_prob=0.33,
        bear_prob=0.33,
        sideways_prob=0.34,
        high_vol_prob=0.0,
        confidence=0.5,
        dominant_regime="sideways",
    )

    obj.bus = MagicMock()
    obj.metrics = MagicMock()
    obj.position_sizer = PositionSizer(max_position_pct=0.2)
    obj.order_manager = MagicMock()
    obj.order_manager.get_open_orders.return_value = []
    obj._strategies = []
    obj._alpha_runtime = MagicMock()
    obj._strategy_registry = MagicMock()
    obj._continuous_learners = {}

    # risk_manager mock
    obj.risk_manager = MagicMock()
    obj.risk_manager.config = RiskConfig()
    obj.risk_manager.is_circuit_broken.return_value = False
    obj.risk_manager.get_state_summary.return_value = {
        "circuit_broken": False,
        "circuit_reason": "",
        "daily_pnl": 0.0,
        "consecutive_losses": 0,
        "peak_equity": 0.0,
        "daily_start_equity": 0.0,
    }
    obj.risk_manager.check.return_value = (True, "通过")

    obj._budget_checker = MagicMock()
    obj._budget_checker.config = BudgetConfig()
    obj._budget_checker.snapshot.return_value = {
        "remaining_budget_pct": 1.0,
        "deployed_pct": 0.0,
    }
    obj._budget_checker.check.return_value = (True, "通过", 0.0)

    obj._kill_switch = MagicMock()
    obj._kill_switch.config = KillSwitchConfig()
    obj._kill_switch.is_active = False
    obj._kill_switch.evaluate.return_value = False
    obj._kill_switch.health_snapshot.return_value = {"reason": ""}

    obj._adaptive_risk = MagicMock()
    obj._adaptive_risk.config = AdaptiveRiskMatrixConfig()
    obj._adaptive_risk.health_snapshot.return_value = {
        "cooldown": {"active_symbols": {}}
    }

    return obj


def _load_p3_cfg():
    from core.config import load_config
    p3_cfg = load_config().phase3
    rt_cfg = p3_cfg.realtime_feed.model_copy(update={"provider": "mock"})
    return p3_cfg.model_copy(update={"realtime_feed": rt_cfg})
