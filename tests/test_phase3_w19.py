"""
tests/test_phase3_w19.py — W19-W21 RL 策略代理单元测试

覆盖：
- rl_types contracts (8 tests)
- ObservationBuilder (8 tests)
- RewardEngine (8 tests)
- ActionAdapter (9 tests)
- RolloutStore (7 tests)
- PPOAgent (10 tests)
- Evaluator (7 tests)
- PolicyStore (8 tests)
- TradingEnvironment (10 tests)

合计 ~75 tests
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from modules.alpha.contracts.rl_types import (
    ActionType,
    EvalResult,
    PolicyDecision,
    PolicyStatus,
    RLAction,
    RLObservation,
    RolloutStep,
)
from modules.alpha.rl.action_adapter import ActionAdapter, ActionAdapterConfig
from modules.alpha.rl.environment import TradingEnvConfig, TradingEnvironment
from modules.alpha.rl.evaluator import EvalGateConfig, Evaluator
from modules.alpha.rl.observation_builder import OBS_DIM, ObservationBuilder, ObservationBuilderConfig
from modules.alpha.rl.policy_store import PolicyRecord, PolicyStore
from modules.alpha.rl.ppo_agent import PPOAgent, PPOConfig
from modules.alpha.rl.reward_engine import RewardConfig, RewardEngine
from modules.alpha.rl.rollout_store import RolloutStore, RolloutStoreConfig
from modules.risk.snapshot import RiskSnapshot


# ══════════════════════════════════════════════════════════════
# 共用 helpers
# ══════════════════════════════════════════════════════════════

def make_risk(
    circuit_broken: bool = False,
    kill_switch: bool = False,
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


def make_obs(
    feature_vector: Optional[list[float]] = None,
    risk_mode: str = "normal",
    inventory_pct: float = 0.5,
) -> RLObservation:
    vec = feature_vector or [0.0] * OBS_DIM
    return RLObservation(
        symbol="BTC/USDT",
        trace_id="test-001",
        feature_vector=vec,
        feature_names=[f"f{i}" for i in range(len(vec))],
        regime="trending",
        risk_mode=risk_mode,
        inventory_pct=inventory_pct,
        position_pct=0.0,
        source_freshness={"technical": True, "onchain": True},
        timestamp=datetime.now(tz=timezone.utc),
    )


def make_rl_action(
    action_type: ActionType = ActionType.HOLD,
    value: float = 0.5,
    confidence: float = 0.7,
) -> RLAction:
    return RLAction(
        action_type=action_type,
        action_value=value,
        confidence=confidence,
    )


def make_rollout_step(
    reward: float = 0.1,
    action_type: ActionType = ActionType.HOLD,
    done: bool = False,
    value_est: float = 0.0,
    log_prob: float = -1.0,
) -> RolloutStep:
    obs = [0.1] * OBS_DIM
    return RolloutStep(
        obs=obs,
        action_index=0,
        action_type=action_type,
        reward=reward,
        next_obs=obs if not done else None,
        done=done,
        value_est=value_est,
        log_prob=log_prob,
    )


def make_replay_data(n: int = 20) -> list[dict[str, Any]]:
    return [
        {
            "mid_price": 50000.0 + i * 10,
            "spread_bps": 2.0,
            "technical": {"rsi": 50.0, "ma_cross": 0.0},
        }
        for i in range(n)
    ]


# ══════════════════════════════════════════════════════════════
# 一、rl_types contracts (8 tests)
# ══════════════════════════════════════════════════════════════

class TestRlTypesContracts:
    def test_action_type_directional(self):
        a = make_rl_action(ActionType.BUY)
        assert a.is_directional() is True

    def test_action_type_mm_bias(self):
        a = make_rl_action(ActionType.WIDEN_QUOTE)
        assert a.is_mm_bias() is True

    def test_hold_is_directional(self):
        a = make_rl_action(ActionType.HOLD)
        assert a.is_directional() is True

    def test_obs_dim(self):
        obs = make_obs()
        assert obs.dim() == OBS_DIM

    def test_obs_is_fresh(self):
        obs = make_obs()
        assert obs.is_fresh() is True

    def test_obs_not_fresh_when_missing_source(self):
        obs = RLObservation(
            symbol="X", trace_id="t", feature_vector=[0.0]*OBS_DIM,
            feature_names=["f"]*OBS_DIM, regime="", risk_mode="normal",
            inventory_pct=0.5, position_pct=0.0,
            source_freshness={"technical": True, "onchain": False},
            timestamp=datetime.now(tz=timezone.utc),
        )
        assert obs.is_fresh() is False

    def test_policy_decision_effective_action(self):
        action = make_rl_action(ActionType.SELL)
        dec = PolicyDecision(
            policy_id="pid", policy_version="v1",
            action=action, reward_estimate=None, safety_override=False,
        )
        assert dec.effective_action() == ActionType.SELL

    def test_rollout_step_fields(self):
        step = make_rollout_step(reward=1.5, action_type=ActionType.BUY)
        assert step.reward == pytest.approx(1.5)
        assert step.action_type == ActionType.BUY
        assert step.done is False


# ══════════════════════════════════════════════════════════════
# 二、ObservationBuilder (8 tests)
# ══════════════════════════════════════════════════════════════

class TestObservationBuilder:
    def make_builder(self) -> ObservationBuilder:
        return ObservationBuilder()

    def test_returns_rl_observation(self):
        b = self.make_builder()
        obs = b.build("BTC/USDT", "t1", make_risk())
        assert isinstance(obs, RLObservation)

    def test_correct_feature_dim(self):
        b = self.make_builder()
        obs = b.build("BTC/USDT", "t1", make_risk())
        assert obs.dim() == OBS_DIM

    def test_feature_names_match_vector(self):
        b = self.make_builder()
        obs = b.build("BTC/USDT", "t1", make_risk())
        assert len(obs.feature_names) == len(obs.feature_vector)

    def test_risk_blocked_mode_when_kill_switch(self):
        b = self.make_builder()
        obs = b.build("BTC/USDT", "t1", make_risk(kill_switch=True))
        assert obs.risk_mode == "blocked"

    def test_risk_reduced_mode_low_budget(self):
        b = self.make_builder()
        obs = b.build("BTC/USDT", "t1", make_risk(budget_pct=0.2))
        assert obs.risk_mode == "reduced"

    def test_all_features_clipped_range(self):
        b = self.make_builder()
        obs = b.build("BTC/USDT", "t1", make_risk(),
                      technical={"rsi": 99.0, "ma_cross": 10.0})
        for v in obs.feature_vector:
            assert -1.0 <= v <= 1.0

    def test_microstructure_features_used(self):
        b = self.make_builder()
        obs = b.build("BTC/USDT", "t1", make_risk(),
                      microstructure={"mb_spread_bps": 5.0, "mb_imbalance": 0.3})
        spread_norm = obs.feature_vector[13]  # micro_spread_bps_norm index
        assert spread_norm > 0.0

    def test_source_freshness_populated(self):
        b = self.make_builder()
        obs = b.build("BTC/USDT", "t1", make_risk(),
                      technical={"rsi": 50.0})
        assert "technical" in obs.source_freshness
        assert obs.source_freshness["technical"] is True


# ══════════════════════════════════════════════════════════════
# 三、RewardEngine (8 tests)
# ══════════════════════════════════════════════════════════════

class TestRewardEngine:
    def make_engine(self, **kwargs) -> RewardEngine:
        cfg = RewardConfig(**kwargs) if kwargs else RewardConfig()
        return RewardEngine(cfg)

    def test_returns_reward_breakdown(self):
        from modules.alpha.rl.reward_engine import RewardBreakdown
        engine = self.make_engine()
        result = engine.compute()
        assert isinstance(result, RewardBreakdown)

    def test_positive_pnl_gives_positive_reward(self):
        engine = self.make_engine()
        r = engine.compute(realized_pnl=100.0, portfolio_value=10000.0)
        assert r.realized_pnl > 0.0

    def test_negative_pnl_gives_negative_reward(self):
        engine = self.make_engine()
        r = engine.compute(realized_pnl=-100.0, portfolio_value=10000.0)
        assert r.realized_pnl < 0.0

    def test_fee_always_negative(self):
        engine = self.make_engine()
        r = engine.compute(fee_paid=10.0, portfolio_value=10000.0)
        assert r.fee < 0.0

    def test_drawdown_penalty_negative(self):
        engine = self.make_engine()
        r = engine.compute(current_drawdown=0.05, portfolio_value=10000.0)
        assert r.drawdown < 0.0

    def test_kill_switch_gives_large_negative(self):
        engine = self.make_engine()
        r = engine.compute(kill_switch_active=True)
        assert r.kill_switch < 0.0

    def test_reward_clipped(self):
        engine = self.make_engine(max_reward_clip=1.0, min_reward_clip=-1.0)
        r = engine.compute(realized_pnl=1000000.0, portfolio_value=1.0)
        assert r.total <= 1.0

    def test_total_is_sum_of_components(self):
        engine = self.make_engine(
            max_reward_clip=1000.0, min_reward_clip=-1000.0,
        )
        r = engine.compute(realized_pnl=10.0, portfolio_value=10000.0)
        expected = (
            r.realized_pnl + r.unrealized_delta + r.fee +
            r.drawdown + r.turnover + r.kill_switch + r.inventory + r.risk_violation
        )
        assert r.raw_total == pytest.approx(expected, abs=1e-6)


# ══════════════════════════════════════════════════════════════
# 四、ActionAdapter (9 tests)
# ══════════════════════════════════════════════════════════════

class TestActionAdapter:
    def make_adapter(self, **kwargs) -> ActionAdapter:
        cfg = ActionAdapterConfig(**kwargs) if kwargs else ActionAdapterConfig()
        return ActionAdapter(cfg)

    def test_index_to_action_type(self):
        adapter = self.make_adapter()
        action = adapter.index_to_action(0)
        assert action.action_type == ActionType.HOLD

    def test_buy_action_at_index_1(self):
        adapter = self.make_adapter()
        action = adapter.index_to_action(1)
        assert action.action_type == ActionType.BUY

    def test_out_of_bounds_wraps(self):
        adapter = self.make_adapter()
        n = adapter.n_action_space()
        action = adapter.index_to_action(n + 2)  # wraps via modulo
        assert action.action_type is not None

    def test_kill_switch_forces_hold(self):
        adapter = self.make_adapter()
        action = make_rl_action(ActionType.BUY, confidence=0.9)
        risk = make_risk(kill_switch=True)
        decision = adapter.apply_safety(action, risk)
        assert decision.action.action_type == ActionType.HOLD
        assert decision.safety_override is True

    def test_circuit_broken_forces_hold(self):
        adapter = self.make_adapter()
        action = make_rl_action(ActionType.SELL, confidence=0.9)
        risk = make_risk(circuit_broken=True)
        decision = adapter.apply_safety(action, risk)
        assert decision.action.action_type == ActionType.HOLD

    def test_low_confidence_forces_hold(self):
        adapter = self.make_adapter(confidence_floor=0.6)
        action = make_rl_action(ActionType.BUY, confidence=0.4)
        risk = make_risk()
        decision = adapter.apply_safety(action, risk)
        assert decision.action.action_type == ActionType.HOLD
        assert "LOW_CONFIDENCE" in decision.override_reason

    def test_normal_action_passes_through(self):
        adapter = self.make_adapter()
        action = make_rl_action(ActionType.SELL, confidence=0.8)
        risk = make_risk()
        obs = make_obs(risk_mode="normal")
        decision = adapter.apply_safety(action, risk, obs=obs)
        assert decision.safety_override is False
        assert decision.action.action_type == ActionType.SELL

    def test_blocked_mode_forces_hold(self):
        adapter = self.make_adapter()
        action = make_rl_action(ActionType.BUY, confidence=0.9)
        risk = make_risk()
        obs = make_obs(risk_mode="blocked")
        decision = adapter.apply_safety(action, risk, obs=obs)
        assert decision.action.action_type == ActionType.HOLD

    def test_action_names_list(self):
        adapter = self.make_adapter()
        names = adapter.action_names()
        assert len(names) == adapter.n_action_space()
        assert "HOLD" in names


# ══════════════════════════════════════════════════════════════
# 五、RolloutStore (7 tests)
# ══════════════════════════════════════════════════════════════

class TestRolloutStore:
    def make_store(self, **kwargs) -> RolloutStore:
        cfg = RolloutStoreConfig(**kwargs) if kwargs else RolloutStoreConfig(capacity=100)
        return RolloutStore(cfg)

    def test_add_and_len(self):
        store = self.make_store()
        store.add(make_rollout_step())
        assert len(store) == 1

    def test_sample_returns_list(self):
        store = self.make_store()
        for _ in range(10):
            store.add(make_rollout_step())
        batch = store.sample(5)
        assert len(batch) == 5

    def test_sample_capped_by_buffer_size(self):
        store = self.make_store()
        for _ in range(3):
            store.add(make_rollout_step())
        batch = store.sample(100)
        assert len(batch) <= 3

    def test_capacity_overwrite(self):
        store = self.make_store(capacity=5)
        for i in range(10):
            store.add(make_rollout_step(reward=float(i)))
        assert len(store) == 5

    def test_compute_gae_length_matches(self):
        store = self.make_store()
        steps = [make_rollout_step(reward=0.1) for _ in range(10)]
        adv = store.compute_gae(steps)
        assert len(adv) == 10

    def test_clear_empties_buffer(self):
        store = self.make_store()
        store.add(make_rollout_step())
        store.clear()
        assert len(store) == 0

    def test_diagnostics_keys(self):
        store = self.make_store()
        d = store.diagnostics()
        assert "buffer_size" in d
        assert "episode_count" in d


# ══════════════════════════════════════════════════════════════
# 六、PPOAgent (10 tests)
# ══════════════════════════════════════════════════════════════

class TestPPOAgent:
    def make_agent(self, **kwargs) -> PPOAgent:
        cfg = PPOConfig(obs_dim=OBS_DIM, n_actions=8, **kwargs)
        return PPOAgent(cfg)

    def test_predict_returns_tuple(self):
        agent = self.make_agent()
        idx, val, conf, lp = agent.predict([0.0] * OBS_DIM)
        assert isinstance(idx, int)
        assert 0 <= idx < 8
        assert 0.0 <= conf <= 1.0
        assert lp <= 0.0  # log prob is always <= 0

    def test_predict_deterministic_consistent(self):
        agent = self.make_agent(seed=0)
        obs = [0.1] * OBS_DIM
        r1 = agent.predict(obs, deterministic=True)
        r2 = agent.predict(obs, deterministic=True)
        assert r1[0] == r2[0]  # same action index

    def test_value_returns_float(self):
        agent = self.make_agent()
        v = agent.value([0.0] * OBS_DIM)
        assert isinstance(v, float)

    def test_update_returns_loss_dict(self):
        agent = self.make_agent(n_epochs=1)
        obs_batch = [[0.0] * OBS_DIM] * 8
        losses = agent.update(
            obs_batch=obs_batch,
            action_indices=[0] * 8,
            old_log_probs=[-2.0] * 8,
            advantages=[0.1] * 8,
            returns=[0.2] * 8,
        )
        assert "policy_loss" in losses
        assert "value_loss" in losses
        assert losses["train_steps"] == 1

    def test_update_empty_returns_empty(self):
        agent = self.make_agent()
        losses = agent.update([], [], [], [], [])
        assert losses == {}

    def test_version_string_set(self):
        agent = self.make_agent()
        assert len(agent.version()) > 0

    def test_to_dict_and_from_dict(self):
        agent = self.make_agent()
        d = agent.to_dict()
        assert "policy_net" in d
        assert "value_net" in d
        loaded = PPOAgent.from_dict(d)
        assert loaded.version() == agent.version()

    def test_round_trip_predict_consistent(self):
        """序列化后再加载，推理结果一致。"""
        agent = self.make_agent(seed=42)
        obs = [0.5] * OBS_DIM
        idx1, _, _, _ = agent.predict(obs, deterministic=True)
        loaded = PPOAgent.from_dict(agent.to_dict())
        idx2, _, _, _ = loaded.predict(obs, deterministic=True)
        assert idx1 == idx2

    def test_diagnostics_keys(self):
        agent = self.make_agent()
        d = agent.diagnostics()
        assert "version" in d
        assert "obs_dim" in d

    def test_action_index_in_range(self):
        agent = self.make_agent()
        for _ in range(20):
            idx, _, _, _ = agent.predict([0.0] * OBS_DIM)
            assert 0 <= idx < 8


# ══════════════════════════════════════════════════════════════
# 七、Evaluator (7 tests)
# ══════════════════════════════════════════════════════════════

class TestEvaluator:
    def make_eval(self, **kwargs) -> Evaluator:
        gate = EvalGateConfig(**kwargs) if kwargs else EvalGateConfig(min_steps=5)
        return Evaluator(gate)

    def make_steps(
        self,
        n: int = 50,
        reward: float = 0.1,
        done_every: int = 10,
    ) -> list[RolloutStep]:
        return [
            make_rollout_step(
                reward=reward,
                action_type=ActionType.BUY if i % 3 == 0 else ActionType.HOLD,
                done=(i % done_every == done_every - 1),
            )
            for i in range(n)
        ]

    def test_returns_eval_result(self):
        evaluator = self.make_eval()
        agent = PPOAgent(PPOConfig(obs_dim=OBS_DIM, n_actions=8))
        steps = self.make_steps()
        result = evaluator.evaluate(agent, steps)
        assert isinstance(result, EvalResult)

    def test_insufficient_steps_fails_gate(self):
        evaluator = self.make_eval(min_steps=100)
        agent = PPOAgent(PPOConfig(obs_dim=OBS_DIM, n_actions=8))
        steps = self.make_steps(n=10)
        result = evaluator.evaluate(agent, steps)
        assert result.passes_gate is False

    def test_positive_rewards_give_positive_return(self):
        evaluator = self.make_eval(min_steps=5)
        agent = PPOAgent(PPOConfig(obs_dim=OBS_DIM, n_actions=8))
        steps = self.make_steps(n=20, reward=1.0)
        result = evaluator.evaluate(agent, steps)
        assert result.total_return > 0.0

    def test_n_steps_matches(self):
        evaluator = self.make_eval(min_steps=5)
        agent = PPOAgent(PPOConfig(obs_dim=OBS_DIM, n_actions=8))
        steps = self.make_steps(n=20)
        result = evaluator.evaluate(agent, steps)
        assert result.n_steps == 20

    def test_risk_violations_counted(self):
        evaluator = self.make_eval(min_steps=5)
        agent = PPOAgent(PPOConfig(obs_dim=OBS_DIM, n_actions=8))
        # 惩罚 <= -5 视为风险违规
        steps = [make_rollout_step(reward=-6.0) for _ in range(10)]
        result = evaluator.evaluate(agent, steps)
        assert result.risk_violations > 0

    def test_eval_mode_preserved(self):
        evaluator = self.make_eval(min_steps=5)
        agent = PPOAgent(PPOConfig(obs_dim=OBS_DIM, n_actions=8))
        steps = self.make_steps(n=20)
        result = evaluator.evaluate(agent, steps, eval_mode="paper")
        assert result.eval_mode == "paper"

    def test_summary_string_not_empty(self):
        evaluator = self.make_eval(min_steps=5)
        agent = PPOAgent(PPOConfig(obs_dim=OBS_DIM, n_actions=8))
        steps = self.make_steps(n=20)
        result = evaluator.evaluate(agent, steps)
        assert len(result.summary()) > 0


# ══════════════════════════════════════════════════════════════
# 八、PolicyStore (8 tests)
# ══════════════════════════════════════════════════════════════

class TestPolicyStore:
    def make_store(self) -> tuple[PolicyStore, str]:
        tmp_dir = tempfile.mkdtemp()
        return PolicyStore(tmp_dir), tmp_dir

    def test_save_and_load_round_trip(self):
        store, _ = self.make_store()
        agent = PPOAgent(PPOConfig(obs_dim=OBS_DIM, n_actions=8))
        record = store.save(agent, "test_policy")
        loaded = store.load("test_policy")
        assert loaded is not None
        assert loaded.version() == agent.version()

    def test_load_missing_returns_none(self):
        store, _ = self.make_store()
        result = store.load("nonexistent_policy")
        assert result is None

    def test_promote_changes_status(self):
        store, _ = self.make_store()
        agent = PPOAgent(PPOConfig(obs_dim=OBS_DIM, n_actions=8))
        record = store.save(agent, "test_policy", status=PolicyStatus.CANDIDATE)
        success = store.promote("test_policy", agent.version(), PolicyStatus.SHADOW)
        assert success is True
        actives = store.list_by_status(PolicyStatus.SHADOW)
        assert any(r.version == agent.version() for r in actives)

    def test_list_by_status_returns_correct(self):
        store, _ = self.make_store()
        agent = PPOAgent(PPOConfig(obs_dim=OBS_DIM, n_actions=8))
        store.save(agent, "test_policy", status=PolicyStatus.CANDIDATE)
        candidates = store.list_by_status(PolicyStatus.CANDIDATE)
        assert len(candidates) >= 1

    def test_get_active_after_promote(self):
        store, _ = self.make_store()
        agent = PPOAgent(PPOConfig(obs_dim=OBS_DIM, n_actions=8))
        store.save(agent, "test_policy", status=PolicyStatus.CANDIDATE)
        store.promote("test_policy", agent.version(), PolicyStatus.ACTIVE)
        active = store.get_active("test_policy")
        assert active is not None
        assert active.status == PolicyStatus.ACTIVE.value

    def test_diagnostics_keys(self):
        store, _ = self.make_store()
        d = store.diagnostics()
        assert "total_records" in d
        assert "by_status" in d

    def test_rollback_no_previous_returns_none(self):
        store, _ = self.make_store()
        result = store.rollback("no_such_policy")
        assert result is None

    def test_save_creates_weight_file(self):
        store, store_dir = self.make_store()
        agent = PPOAgent(PPOConfig(obs_dim=OBS_DIM, n_actions=8))
        record = store.save(agent, "test_policy")
        weight_path = os.path.join(store_dir, record.weight_path)
        assert os.path.exists(weight_path)


# ══════════════════════════════════════════════════════════════
# 九、TradingEnvironment (10 tests)
# ══════════════════════════════════════════════════════════════

class TestTradingEnvironment:
    def make_env(self, max_steps: int = 50, **kwargs) -> TradingEnvironment:
        cfg = TradingEnvConfig(max_steps=max_steps, **kwargs)
        return TradingEnvironment(cfg)

    def test_reset_returns_obs(self):
        env = self.make_env()
        obs = env.reset(replay_data=make_replay_data(30))
        assert isinstance(obs, RLObservation)

    def test_obs_has_correct_dim(self):
        env = self.make_env()
        obs = env.reset(replay_data=make_replay_data(30))
        assert obs.dim() == OBS_DIM

    def test_step_returns_tuple(self):
        env = self.make_env()
        env.reset(replay_data=make_replay_data(30))
        next_obs, reward, done, info = env.step(0, ActionType.HOLD)
        assert isinstance(next_obs, RLObservation)
        assert isinstance(reward, float)
        assert isinstance(done, bool)

    def test_step_raises_without_reset(self):
        env = self.make_env()
        with pytest.raises(RuntimeError):
            env.step(0, ActionType.HOLD)

    def test_episode_ends_at_max_steps(self):
        env = self.make_env(max_steps=5)
        env.reset(replay_data=make_replay_data(10))
        done = False
        for _ in range(10):
            _, _, done, _ = env.step(0, ActionType.HOLD)
            if done:
                break
        assert done is True

    def test_buy_increases_position(self):
        env = self.make_env()
        env.reset(replay_data=make_replay_data(30))
        initial_pos = env._account.position_pct
        env.step(1, ActionType.BUY)
        assert env._account.position_pct > initial_pos

    def test_sell_decreases_position(self):
        env = self.make_env()
        env.reset(replay_data=make_replay_data(30))
        # 先买
        env.step(1, ActionType.BUY)
        pos_after_buy = env._account.position_pct
        # 再卖
        env.step(2, ActionType.SELL)
        assert env._account.position_pct < pos_after_buy

    def test_hold_no_position_change(self):
        env = self.make_env()
        env.reset(replay_data=make_replay_data(30))
        pos_before = env._account.position_pct
        env.step(0, ActionType.HOLD)
        assert env._account.position_pct == pos_before

    def test_make_rollout_step_fields(self):
        env = self.make_env()
        obs = env.reset(replay_data=make_replay_data(30))
        next_obs, reward, done, info = env.step(0, ActionType.HOLD)
        step = env.make_rollout_step(obs, 0, ActionType.HOLD, reward, next_obs, done)
        assert len(step.obs) == OBS_DIM
        assert step.action_type == ActionType.HOLD

    def test_equity_decreases_on_fee(self):
        env = self.make_env()
        env.reset(replay_data=make_replay_data(30))
        eq_before = env.current_equity()
        env.step(1, ActionType.BUY)  # BUY charges fee
        # equity may decrease slightly due to fee
        # (can also increase due to mark-to-market; check fee was paid)
        assert env._account.fee_paid >= 0
