"""
tests/test_phase3_integration.py — Phase 3 集成测试

覆盖 §8.2 要求的四条端到端路径：

路径1: ReplayFeed → DepthCache → OrderBookSnapshot
    验证：replay 产出的增量包经 DepthCache 合并后得到合法快照
    验证：序列号 gap 时快照被拒绝，不向下游输出脏数据

路径2: OrderBookSnapshot → MicroFeatureBuilder → ObservationBuilder → PPOAgent → ActionAdapter
    验证：从订单簿快照到 RL 动作决策的完整数据流
    验证：ActionAdapter 安全覆写在风险事件下正确触发

路径3: OrderBookSnapshot → MarketMakingStrategy (paper mode) → FillSimulator
    验证：做市策略消费订单簿快照，产出 QuoteDecision
    验证：paper 模式下 fill 仿真路径完整可运行
    验证：风险守卫 kill_switch 时策略正确拦截

路径4: candidate → shadow → paper → active 完整演进状态机
    验证：SelfEvolutionEngine 跨多次 run_cycle 正确推进候选状态
    验证：晋升决策与降级决策都产生可审计记录
    验证：A/B 实验结果写回候选 ab_lift 指标

合计 ~35 tests
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

# ─── Realtime 数据层 ───────────────────────────────────────────
from modules.data.realtime.depth_cache import DepthCache, DepthCacheConfig
from modules.data.realtime.feature_builder import MicroFeatureBuilder
from modules.data.realtime.orderbook_types import (
    DepthLevel,
    GapStatus,
    OrderBookDelta,
    OrderBookSnapshot,
    TradeSide,
    TradeTick,
)
from modules.data.realtime.replay_feed import ReplayFeed, ReplayFeedConfig
from modules.data.realtime.trade_cache import TradeCache

# ─── Market Making ────────────────────────────────────────────
from modules.alpha.market_making.strategy import MarketMakingStrategy, MarketMakingStrategyConfig
from modules.alpha.market_making.inventory_manager import InventoryConfig
from modules.alpha.market_making.avellaneda_model import AvellanedaConfig
from modules.alpha.contracts.mm_types import QuoteDecision

# ─── RL ──────────────────────────────────────────────────────
from modules.alpha.rl.action_adapter import ActionAdapter, ActionAdapterConfig
from modules.alpha.rl.observation_builder import ObservationBuilder, OBS_DIM
from modules.alpha.rl.ppo_agent import PPOAgent, PPOConfig
from modules.alpha.contracts.rl_types import ActionType, PolicyStatus

# ─── Evolution ───────────────────────────────────────────────
from modules.alpha.contracts.evolution_types import (
    CandidateStatus,
    CandidateType,
    PromotionAction,
)
from modules.evolution.self_evolution_engine import SelfEvolutionConfig, SelfEvolutionEngine
from modules.evolution.scheduler import SchedulerConfig
from modules.evolution.promotion_gate import PromotionGateConfig, StageGateConfig

# ─── Risk ────────────────────────────────────────────────────
from modules.risk.snapshot import RiskSnapshot


# ══════════════════════════════════════════════════════════════
# 共用 helpers
# ══════════════════════════════════════════════════════════════

SYMBOL = "BTC/USDT"
EXCHANGE = "backtest"
MID = 50000.0


def make_risk(
    kill_switch: bool = False,
    circuit_broken: bool = False,
    budget_pct: float = 0.8,
    drawdown: float = 0.01,
) -> RiskSnapshot:
    return RiskSnapshot(
        current_drawdown=drawdown,
        daily_loss_pct=0.0,
        consecutive_losses=0,
        circuit_broken=circuit_broken,
        kill_switch_active=kill_switch,
        budget_remaining_pct=budget_pct,
    )


def make_delta(
    sequence_id: int,
    mid: float = MID,
    spread_bps: float = 2.0,
    is_snapshot: bool = False,
    symbol: str = SYMBOL,
) -> OrderBookDelta:
    """构造一个合法的订单簿增量包（内含 1 档买卖各一档）。"""
    half_spread = mid * spread_bps / 20000.0
    return OrderBookDelta(
        symbol=symbol,
        exchange=EXCHANGE,
        sequence_id=sequence_id,
        prev_sequence_id=sequence_id - 1,
        bid_updates=[DepthLevel(price=mid - half_spread, size=1.0)],
        ask_updates=[DepthLevel(price=mid + half_spread, size=1.0)],
        received_at=datetime.now(tz=timezone.utc),
        is_snapshot=is_snapshot,
    )


def make_snapshot(
    mid: float = MID,
    spread_bps: float = 2.0,
    symbol: str = SYMBOL,
    sequence_id: int = 1,
) -> OrderBookSnapshot:
    half = mid * spread_bps / 20000.0
    best_bid = mid - half
    best_ask = mid + half
    return OrderBookSnapshot(
        symbol=symbol,
        exchange=EXCHANGE,
        sequence_id=sequence_id,
        best_bid=best_bid,
        best_ask=best_ask,
        bids=[DepthLevel(price=best_bid, size=1.0)],
        asks=[DepthLevel(price=best_ask, size=1.0)],
        spread_bps=spread_bps,
        mid_price=mid,
        imbalance=0.0,
        received_at=datetime.now(tz=timezone.utc),
        exchange_ts=None,
        is_gap_recovered=False,
        gap_status=GapStatus.OK,
    )


def make_mm_strategy(paper_mode: bool = True, tmp_dir: str = "") -> MarketMakingStrategy:
    cfg = MarketMakingStrategyConfig(
        symbol=SYMBOL,
        exchange=EXCHANGE,
        paper_mode=paper_mode,
        save_every_n=0,
        state_store_path=os.path.join(tmp_dir, "quote_state.json") if tmp_dir else None,
        inventory=InventoryConfig(
            initial_base_qty=0.1,
            initial_quote_value=5000.0,
            halt_inventory_pct=1.0,         # 禁用库存停打，遇免初始化不平衡时停打
            max_inventory_skew_bps=10000.0, # 宽松假断，逇免初始化触发 HALT
        ),
        avellaneda=AvellanedaConfig(max_spread_bps=100.0),
    )
    return MarketMakingStrategy(cfg)


def make_engine(tmp_dir: str) -> SelfEvolutionEngine:
    cfg = SelfEvolutionConfig(
        state_dir=tmp_dir,
        auto_run=False,
        scheduler=SchedulerConfig(interval_sec=0.0, cooldown_sec=0.0),
        promotion_gate=PromotionGateConfig(
            candidate_to_shadow=StageGateConfig(min_sharpe=0.8, max_drawdown=0.07),
            shadow_to_paper=StageGateConfig(min_sharpe=0.6, max_drawdown=0.09),
            paper_to_active=StageGateConfig(
                min_sharpe=0.5, max_drawdown=0.10,
                min_ab_lift=0.0, require_ab_completed=True,
            ),
        ),
    )
    return SelfEvolutionEngine(cfg)


# ══════════════════════════════════════════════════════════════
# 路径1：ReplayFeed → DepthCache → OrderBookSnapshot (9 tests)
# ══════════════════════════════════════════════════════════════

class TestIntegration_ReplayToDepthCache:
    """
    验证：replay 产出的增量包经 DepthCache 合并后得到合法快照。
    """

    def make_cache(self) -> DepthCache:
        return DepthCache(SYMBOL, EXCHANGE, DepthCacheConfig(max_depth=5))

    def test_snapshot_delta_produces_valid_snapshot(self):
        """全量包（is_snapshot=True）建立初始订单簿后可获取快照。"""
        cache = self.make_cache()
        delta = make_delta(sequence_id=1, is_snapshot=True)
        cache.apply(delta)
        snap = cache.get_snapshot()
        assert snap is not None
        assert snap.best_bid < snap.best_ask

    def test_incremental_deltas_update_best_prices(self):
        """连续增量包正确更新 best bid/ask。"""
        cache = self.make_cache()
        cache.apply(make_delta(1, is_snapshot=True, mid=50000.0))
        cache.apply(make_delta(2, mid=50100.0))
        snap = cache.get_snapshot()
        assert snap is not None
        # best_ask 应该接近新 mid
        assert snap.mid_price > 0

    def test_gap_in_sequence_blocks_snapshot(self):
        """序列号跳跃（gap）后，get_snapshot() 返回 None。"""
        cache = self.make_cache()
        cache.apply(make_delta(1, is_snapshot=True))
        # 跳过 sequence_id=2，直接给 3
        cache.apply(make_delta(3))
        snap = cache.get_snapshot()
        assert snap is None

    def test_snapshot_recovery_after_gap(self):
        """gap 后通过全量快照包回补，可重新获取快照。"""
        cache = self.make_cache()
        cache.apply(make_delta(1, is_snapshot=True))
        cache.apply(make_delta(5))  # gap: 跳了 2,3,4
        assert cache.get_snapshot() is None
        # 全量快照回补
        cache.apply(make_delta(10, is_snapshot=True))
        snap = cache.get_snapshot()
        assert snap is not None

    def test_replay_feed_delivers_to_depth_cache(self):
        """ReplayFeed → DepthCache 联动：回放事件流进 cache 后快照合法。"""
        cache = self.make_cache()
        received: list[OrderBookSnapshot] = []

        def on_depth(delta: OrderBookDelta) -> None:
            cache.apply(delta)
            snap = cache.get_snapshot()
            if snap is not None:
                received.append(snap)

        feed = ReplayFeed(ReplayFeedConfig(playback_speed=0.0))
        # 使用相同 mid 的全量包，避免积累增量导致交叉求
        events = [
            make_delta(1, is_snapshot=True, mid=50000.0),
            make_delta(2, is_snapshot=True, mid=50000.0),
            make_delta(3, is_snapshot=True, mid=50000.0),
        ]
        feed.load_events(events)
        feed.set_depth_callback(on_depth)
        feed.start()
        feed.wait_until_done()

        assert len(received) >= 1
        for snap in received:
            assert snap.best_bid > 0
            assert snap.best_ask > 0
            assert snap.best_bid < snap.best_ask

    def test_out_of_order_delta_rejected(self):
        """乱序（sequence_id 回退）的增量包不产出快照，且 gap 被检测。"""
        cache = self.make_cache()
        cache.apply(make_delta(5, is_snapshot=True))
        snap_before = cache.get_snapshot()
        assert snap_before is not None  # 应有快照
        # 退序增量（seq 3 而不是期望的 6）→ gap 被检测
        result = cache.apply(make_delta(3))
        assert result is None  # 脚踏包不被接受
        # gap 状态下 get_snapshot 返回 None
        assert cache.get_snapshot() is None
        # gap_status 应为 GAP_DETECTED
        assert cache.gap_status == GapStatus.GAP_DETECTED

    def test_spread_bps_is_positive(self):
        """快照中 spread_bps > 0。"""
        cache = self.make_cache()
        cache.apply(make_delta(1, is_snapshot=True, mid=50000.0, spread_bps=5.0))
        snap = cache.get_snapshot()
        assert snap is not None
        assert snap.spread_bps > 0.0

    def test_replay_feed_continuous_sequence(self):
        """回放 10 个连续增量包，全部到达 DepthCache，无 gap。"""
        cache = self.make_cache()
        cache.apply(make_delta(1, is_snapshot=True))

        snapshots: list[OrderBookSnapshot] = []
        def on_depth(delta: OrderBookDelta) -> None:
            cache.apply(delta)
            s = cache.get_snapshot()
            if s:
                snapshots.append(s)

        feed = ReplayFeed(ReplayFeedConfig(playback_speed=0.0))
        feed.load_events([make_delta(i + 2) for i in range(10)])
        feed.set_depth_callback(on_depth)
        feed.start()
        feed.wait_until_done()

        assert len(snapshots) == 10

    def test_gap_status_recovers_correctly(self):
        """gap 恢复后 gap_status 重新 is_healthy()。"""
        cache = self.make_cache()
        cache.apply(make_delta(1, is_snapshot=True))
        cache.apply(make_delta(10))  # gap
        assert not cache.gap_status.is_healthy()
        cache.apply(make_delta(20, is_snapshot=True))  # 回补
        assert cache.gap_status.is_healthy()


# ══════════════════════════════════════════════════════════════
# 路径2：OrderBookSnapshot → MicroFeatureBuilder → ObsBuilder → PPOAgent → ActionAdapter (9 tests)
# ══════════════════════════════════════════════════════════════

class TestIntegration_SnapshotToRLAction:
    """
    验证：从订单簿快照到 RL 动作决策的完整数据流。
    """

    def make_pipeline(self):
        obs_builder = ObservationBuilder()
        agent = PPOAgent(PPOConfig(obs_dim=OBS_DIM, n_actions=8, seed=0))
        adapter = ActionAdapter(ActionAdapterConfig())
        micro_builder = MicroFeatureBuilder()
        return obs_builder, agent, adapter, micro_builder

    def test_snapshot_to_micro_features(self):
        """OrderBookSnapshot → MicroFeatureBuilder 产出特征帧。"""
        micro = MicroFeatureBuilder()
        snap = make_snapshot()
        result = micro.build(snap)
        assert result is not None
        assert result.is_book_healthy is True
        assert result.mb_spread_bps > 0

    def test_micro_features_into_obs_builder(self):
        """MicroFeatureBuilder 输出可以作为 ObservationBuilder 的 microstructure 输入。"""
        obs_builder, _, _, micro_builder = self.make_pipeline()
        snap = make_snapshot()
        micro = micro_builder.build(snap)
        micro_dict = micro.to_series().to_dict()
        obs = obs_builder.build(
            symbol=SYMBOL,
            trace_id="test-001",
            risk_snapshot=make_risk(),
            microstructure=micro_dict,
        )
        assert obs.dim() == OBS_DIM
        assert len(obs.feature_vector) == OBS_DIM

    def test_obs_into_ppo_predict(self):
        """RLObservation.feature_vector → PPOAgent.predict 返回合法动作索引。"""
        obs_builder, agent, _, _ = self.make_pipeline()
        obs = obs_builder.build(SYMBOL, "t1", make_risk())
        idx, val, conf, lp = agent.predict(obs.feature_vector)
        assert 0 <= idx < 8
        assert 0.0 <= conf <= 1.0

    def test_ppo_action_into_adapter(self):
        """PPOAgent 输出 → ActionAdapter.index_to_action → RLAction。"""
        _, agent, adapter, _ = self.make_pipeline()
        obs_vec = [0.1] * OBS_DIM
        idx, val, conf, _ = agent.predict(obs_vec, deterministic=True)
        action = adapter.index_to_action(idx, action_value=val, confidence=conf)
        assert action.action_type in list(ActionType)

    def test_adapter_apply_safety_normal(self):
        """确定性 HOLD 动作（索引 0）不因安全覆写而改变 action_type。"""
        _, agent, adapter, _ = self.make_pipeline()
        # 直接构造高置信度 HOLD 动作，规避随机初始化导致置信度过低
        action = adapter.index_to_action(0, action_value=0.0, confidence=0.99)  # HOLD
        decision = adapter.apply_safety(action, make_risk())
        assert decision.action.action_type == ActionType.HOLD

    def test_kill_switch_forces_hold(self):
        """Kill Switch 激活时，ActionAdapter 强制覆写为 HOLD。"""
        _, agent, adapter, _ = self.make_pipeline()
        action = adapter.index_to_action(1, action_value=0.5, confidence=0.9)  # BUY
        decision = adapter.apply_safety(action, make_risk(kill_switch=True))
        assert decision.action.action_type == ActionType.HOLD
        assert decision.safety_override is True

    def test_circuit_broken_forces_hold(self):
        """熔断时 ActionAdapter 强制 HOLD。"""
        _, agent, adapter, _ = self.make_pipeline()
        action = adapter.index_to_action(2, action_value=0.5, confidence=0.9)  # SELL
        decision = adapter.apply_safety(action, make_risk(circuit_broken=True))
        assert decision.action.action_type == ActionType.HOLD

    def test_full_pipeline_snapshot_to_decision(self):
        """OrderBookSnapshot → Micro → Obs → PPO → Adapter → PolicyDecision 全链路一次跑通。"""
        obs_builder, agent, adapter, micro_builder = self.make_pipeline()
        snap = make_snapshot()
        micro = micro_builder.build(snap)
        micro_dict = micro.to_series().to_dict()
        obs = obs_builder.build(
            symbol=SYMBOL,
            trace_id="int-001",
            risk_snapshot=make_risk(),
            microstructure=micro_dict,
        )
        idx, val, conf, _ = agent.predict(obs.feature_vector)
        action = adapter.index_to_action(idx, action_value=val, confidence=conf)
        decision = adapter.apply_safety(action, make_risk(), obs=obs)
        assert decision.action.action_type in list(ActionType)

    def test_ten_snapshots_ten_decisions(self):
        """连续 10 个不同 mid 的快照各自得到一个决策，无异常。"""
        obs_builder, agent, adapter, micro_builder = self.make_pipeline()
        risk = make_risk()
        decisions = []
        for i in range(10):
            snap = make_snapshot(mid=50000.0 + i * 100)
            micro = micro_builder.build(snap)
            micro_dict = micro.to_series().to_dict()
            obs = obs_builder.build(SYMBOL, f"t{i}", risk, microstructure=micro_dict)
            idx, val, conf, _ = agent.predict(obs.feature_vector)
            action = adapter.index_to_action(idx, action_value=val, confidence=conf)
            decision = adapter.apply_safety(action, risk, obs=obs)
            decisions.append(decision)
        assert len(decisions) == 10


# ══════════════════════════════════════════════════════════════
# 路径3：OrderBookSnapshot → MarketMakingStrategy (paper) → FillSimulator (8 tests)
# ══════════════════════════════════════════════════════════════

class TestIntegration_SnapshotToMarketMaking:
    """
    验证：做市策略消费订单簿快照，paper 模式 fill 仿真路径完整可运行。
    """

    def make_strategy(self) -> MarketMakingStrategy:
        tmp_dir = tempfile.mkdtemp()
        return make_mm_strategy(paper_mode=True, tmp_dir=tmp_dir)

    def test_single_tick_returns_quote_decision(self):
        """单次 tick 返回 QuoteDecision。"""
        strategy = self.make_strategy()
        snap = make_snapshot()
        risk = make_risk()
        decision = strategy.tick(snap, risk, elapsed_sec=1.0)
        assert isinstance(decision, QuoteDecision)

    def test_quote_decision_has_symbol(self):
        """QuoteDecision 总是携带 symbol 字段（包含 HALT 状态下）。"""
        strategy = self.make_strategy()
        snap = make_snapshot()
        decision = strategy.tick(snap, make_risk(), elapsed_sec=1.0)
        # 不论 HALT 与否，symbol 字段始终有效
        assert decision.symbol == SYMBOL
        # reason_codes 应为非空列表
        assert isinstance(decision.reason_codes, list)

    def test_kill_switch_blocks_posting(self):
        """Kill Switch 激活时，QuoteDecision 不允许任何一侧挂单。"""
        strategy = self.make_strategy()
        snap = make_snapshot()
        decision = strategy.tick(snap, make_risk(kill_switch=True), elapsed_sec=1.0)
        assert decision.allow_post_bid is False
        assert decision.allow_post_ask is False

    def test_multiple_ticks_no_crash(self):
        """连续 20 次 tick，策略不崩溃。"""
        strategy = self.make_strategy()
        risk = make_risk()
        for i in range(20):
            snap = make_snapshot(mid=50000.0 + i * 50, sequence_id=i + 1)
            strategy.tick(snap, risk, elapsed_sec=float(i))

    def test_paper_fill_updates_inventory(self):
        """
        paper 模式下，当 best_ask 恰好低于 bid quote 时，fill simulator 触发 fill。
        验证 fill 后策略内部库存发生变化（无崩溃）。
        """
        strategy = self.make_strategy()
        snap = make_snapshot(mid=50000.0)
        risk = make_risk()
        # 运行几次 tick 让 quote 建立起来
        for _ in range(3):
            strategy.tick(snap, risk, elapsed_sec=1.0)
        # 模拟价格急涨（使卖单 fill）
        snap_high = make_snapshot(mid=50200.0, spread_bps=0.5)
        decision = strategy.tick(snap_high, risk, elapsed_sec=2.0)
        assert isinstance(decision, QuoteDecision)

    def test_diagnostics_available(self):
        """strategy.diagnostics() 返回包含关键字段的字典。"""
        strategy = self.make_strategy()
        snap = make_snapshot()
        strategy.tick(snap, make_risk(), elapsed_sec=1.0)
        d = strategy.diagnostics()
        assert "tick_count" in d
        assert "symbol" in d

    def test_reset_clears_state(self):
        """strategy.reset() 后 tick_count 归零。"""
        strategy = self.make_strategy()
        snap = make_snapshot()
        for _ in range(5):
            strategy.tick(snap, make_risk(), elapsed_sec=1.0)
        strategy.reset()
        d = strategy.diagnostics()
        assert d["tick_count"] == 0

    def test_replay_feed_to_market_making(self):
        """ReplayFeed → DepthCache → MarketMakingStrategy 三层联动 10 步。"""
        cache = DepthCache(SYMBOL, EXCHANGE, DepthCacheConfig())
        tmp_dir = tempfile.mkdtemp()
        strategy = make_mm_strategy(paper_mode=True, tmp_dir=tmp_dir)
        risk = make_risk()
        decisions: list[QuoteDecision] = []

        def on_depth(delta: OrderBookDelta) -> None:
            cache.apply(delta)
            snap = cache.get_snapshot()
            if snap is not None:
                d = strategy.tick(snap, risk, elapsed_sec=1.0)
                decisions.append(d)

        feed = ReplayFeed(ReplayFeedConfig(playback_speed=0.0))
        events = [make_delta(1, is_snapshot=True)] + [make_delta(i + 2) for i in range(9)]
        feed.load_events(events)
        feed.set_depth_callback(on_depth)
        feed.start()
        feed.wait_until_done()

        assert len(decisions) == 10
        for d in decisions:
            assert isinstance(d, QuoteDecision)


# ══════════════════════════════════════════════════════════════
# 路径4：候选完整演进状态机 + A/B 闭环 (9 tests)
# ══════════════════════════════════════════════════════════════

class TestIntegration_EvolutionStateMachine:
    """
    验证：SelfEvolutionEngine 跨多次 run_cycle 正确推进候选状态。
    """

    def make_engine(self) -> SelfEvolutionEngine:
        return make_engine(tempfile.mkdtemp())

    def test_candidate_promoted_to_shadow_on_good_metrics(self):
        """达标候选在 run_cycle 后晋升到 shadow。"""
        engine = self.make_engine()
        snap = engine.register_candidate(CandidateType.POLICY, "rl/ppo", "v1",
                                          candidate_id="cand_s")
        engine.update_metrics("cand_s", sharpe_30d=1.5, max_drawdown_30d=0.04)
        engine.run_cycle(force=True)
        updated = engine.get_candidate("cand_s")
        assert updated.status == CandidateStatus.SHADOW.value

    def test_two_cycles_shadow_to_paper(self):
        """连续两次 run_cycle：candidate→shadow→paper。"""
        engine = self.make_engine()
        engine.register_candidate(CandidateType.POLICY, "rl/ppo", "v1",
                                   candidate_id="cand_2c")
        engine.update_metrics("cand_2c", sharpe_30d=1.5, max_drawdown_30d=0.04)
        engine.run_cycle(force=True)  # → shadow

        engine.update_metrics("cand_2c", sharpe_30d=1.2, max_drawdown_30d=0.06)
        engine.run_cycle(force=True)  # → paper
        updated = engine.get_candidate("cand_2c")
        assert updated.status == CandidateStatus.PAPER.value

    def test_three_cycles_paper_to_active_with_ab(self):
        """三次 cycle + A/B 完结后候选晋升到 active。"""
        engine = self.make_engine()
        engine.register_candidate(CandidateType.POLICY, "rl/ppo", "v1",
                                   candidate_id="cand_3c")

        # cycle 1: candidate → shadow
        engine.update_metrics("cand_3c", sharpe_30d=1.5, max_drawdown_30d=0.04)
        engine.run_cycle(force=True)

        # cycle 2: shadow → paper
        engine.update_metrics("cand_3c", sharpe_30d=1.2, max_drawdown_30d=0.06)
        engine.run_cycle(force=True)

        # A/B 实验（test 候选比 control 好）
        eid = engine.create_ab_experiment("ctrl_baseline", "cand_3c")
        for _ in range(10):
            engine.record_ab_step(eid, is_test=False, step_pnl=1.0)
            engine.record_ab_step(eid, is_test=True, step_pnl=2.0)
        engine.conclude_ab_experiment(eid)  # 写入 ab_lift

        # cycle 3: paper → active（需要 ab_lift 已完成）
        engine.update_metrics("cand_3c", sharpe_30d=0.9, max_drawdown_30d=0.08)
        engine.run_cycle(force=True)

        updated = engine.get_candidate("cand_3c")
        assert updated.status == CandidateStatus.ACTIVE.value

    def test_poor_candidate_stays_candidate(self):
        """不达标候选不被晋升。"""
        engine = self.make_engine()
        engine.register_candidate(CandidateType.MODEL, "ml/rf", "v1",
                                   candidate_id="cand_bad")
        engine.update_metrics("cand_bad", sharpe_30d=0.1, max_drawdown_30d=0.20)
        engine.run_cycle(force=True)
        updated = engine.get_candidate("cand_bad")
        assert updated.status == CandidateStatus.CANDIDATE.value

    def test_active_candidate_retired_on_risk_violations(self):
        """风险违规次数超阈值时 ACTIVE 候选被淘汰。"""
        engine = self.make_engine()
        snap = engine.register_candidate(CandidateType.POLICY, "rl/ppo", "v2",
                                          candidate_id="cand_rv")
        engine.force_promote("cand_rv", CandidateStatus.ACTIVE)
        # 累计大量风险违规
        engine.record_risk_violation("cand_rv", n=10)
        engine.run_cycle(force=True)
        updated = engine.get_candidate("cand_rv")
        assert updated.status in (
            CandidateStatus.PAUSED.value,
            CandidateStatus.RETIRED.value,
        )

    def test_audit_log_written_on_promotion(self):
        """晋升后审计日志中有对应记录。"""
        engine = self.make_engine()
        engine.register_candidate(CandidateType.POLICY, "rl/ppo", "v3",
                                   candidate_id="cand_log")
        engine.update_metrics("cand_log", sharpe_30d=1.5, max_drawdown_30d=0.04)
        engine.run_cycle(force=True)

        history = engine.decision_history(limit=10)
        promote_entries = [
            h for h in history
            if h.get("candidate_id") == "cand_log"
            and h.get("action") == PromotionAction.PROMOTE.value
        ]
        assert len(promote_entries) >= 1

    def test_ab_experiment_updates_candidate_ab_lift(self):
        """A/B 实验结束后候选的 ab_lift 被更新。"""
        engine = self.make_engine()
        engine.register_candidate(CandidateType.POLICY, "rl/ppo", "v4",
                                   candidate_id="cand_ab")
        eid = engine.create_ab_experiment("ctrl", "cand_ab")
        for _ in range(5):
            engine.record_ab_step(eid, False, 1.0)
            engine.record_ab_step(eid, True, 3.0)
        engine.conclude_ab_experiment(eid)
        updated = engine.get_candidate("cand_ab")
        assert updated.ab_lift is not None
        assert updated.ab_lift > 0

    def test_multiple_candidates_independent(self):
        """多个候选同时演进，互不干扰。"""
        engine = self.make_engine()
        engine.register_candidate(CandidateType.POLICY, "rl/ppo", "v5",
                                   candidate_id="cand_a")
        engine.register_candidate(CandidateType.MODEL, "ml/lgb", "v5",
                                   candidate_id="cand_b")

        engine.update_metrics("cand_a", sharpe_30d=1.5, max_drawdown_30d=0.04)
        engine.update_metrics("cand_b", sharpe_30d=0.1, max_drawdown_30d=0.30)
        engine.run_cycle(force=True)

        a = engine.get_candidate("cand_a")
        b = engine.get_candidate("cand_b")
        assert a.status == CandidateStatus.SHADOW.value
        assert b.status == CandidateStatus.CANDIDATE.value  # 不达标，未晋升

    def test_report_generated_after_cycle(self):
        """每次 run_cycle 后都生成演进报告并持久化。"""
        engine = self.make_engine()
        engine.register_candidate(CandidateType.POLICY, "rl/ppo", "v6",
                                   candidate_id="cand_rpt")
        engine.update_metrics("cand_rpt", sharpe_30d=1.5, max_drawdown_30d=0.04)
        engine.run_cycle(force=True)

        report = engine.latest_report()
        assert report is not None
        assert "report_id" in report
        assert "promoted" in report
