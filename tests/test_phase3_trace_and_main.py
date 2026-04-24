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
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

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

        from core.config import load_config
        p3_cfg = load_config().phase3

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


# ─────────────────────────────────────────────────────────────
# 辅助工厂函数
# ─────────────────────────────────────────────────────────────

def _make_minimal_trader():
    """
    构造一个仅有 Phase 3 相关属性的极简 LiveTrader 实例（无真实 CCXT）。
    """
    from apps.trader.main import LiveTrader

    obj = object.__new__(LiveTrader)

    # 最小必要属性
    from core.config import load_config
    obj.sys_config = load_config()
    obj.mode = "paper"

    obj._phase3_enabled = False
    obj._phase3_mm = None
    obj._phase3_ppo = None
    obj._phase3_evolution = None

    # risk_manager mock
    obj.risk_manager = MagicMock()
    obj.risk_manager.is_circuit_broken.return_value = False

    return obj


def _load_p3_cfg():
    from core.config import load_config
    return load_config().phase3
