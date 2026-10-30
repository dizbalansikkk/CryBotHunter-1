from types import SimpleNamespace as Row
from unittest.mock import Mock
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.db.base import Base
from app.models.entities import Position, AgentPerformance
from app.services.agent_competition import AgentCompetitionService
from app.services.trust_score import rolling_trust
from app.services.market_features import join_market_features, MICRO_FEATURES
from app.services.rl_environment import CryptoTradingEnv, FEATURE_NAMES
from app.services.walk_forward import walk_forward_windows
from app.services.rl_training import RlTrainingService
from app.services.agents import AgentOrchestrator
from app.schemas.dto import AgentDecisionOut
from test_paper_exit_lifecycle import Ledger


def frame(n=120):
    data = pd.DataFrame({name: np.zeros(n) for name in FEATURE_NAMES})
    data["timestamp"] = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    data["close"] = 100.
    return data


def test_trust_uses_last_50_and_can_reach_zero():
    assert rolling_trust([])["trust"] == .25
    assert rolling_trust([1] * 50 + [-1] * 50)["trust"] == 0
    assert rolling_trust([-1] * 50 + [.5, 1] * 25)["trust"] > 0
    assert rolling_trust([-1] * 3)["trust"] == 0


def vote(name, action, weight):
    return AgentDecisionOut(agent_name=name, symbol="ETH/USDT", action=action,
        confidence=.8, rationale="test", context={"vote_weight": weight, "vote_family": name})


def test_zero_weight_agent_does_not_count_as_second_family_or_change_direction():
    engine = AgentOrchestrator()
    result, _ = engine._committee_consensus(vote("market", "BUY", 1), None, [vote("bad", "SELL", 0), vote("other", "BUY", 1)])
    assert result.action == "BUY"
    result, _ = engine._committee_consensus(vote("market", "BUY", 1), None, [vote("bad", "BUY", 0)])
    assert result.action == "WAIT"
    result, _ = engine._committee_consensus(vote("market", "BUY", 1), None, [vote("safety", "BLOCK", 0)])
    assert result.action == "WAIT"


@pytest.mark.asyncio
async def test_realized_agent_history_persisted_once_and_unexecuted_vote_unscored():
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    with Session(database, expire_on_commit=False) as session:
        db = Ledger(session)
        p = Position(id=1, symbol="ETH/USDT", side="LONG", pnl=-1, entry_context={"planned_risk": 1,
            "agent_votes": [{"agent": "Analyst", "action": "BUY"}, {"agent": "Other", "action": "SELL"}]})
        service = AgentCompetitionService()
        await service.record_closed_position(db, p)
        await db.commit()
        row = session.scalars(select(AgentPerformance).where(AgentPerformance.agent_name == "Analyst")).one()
        assert row.trust_history[0]["result_r"] == -1
        assert await service.record_closed_position(db, p) == []
        assert len(row.trust_history) == 1
        assert session.scalars(select(AgentPerformance).where(AgentPerformance.agent_name == "Other")).first() is None
    database.dispose()


def test_asof_never_uses_future_snapshot_and_marks_missing_zones():
    f = frame(4)
    snapshot = Row(available_at=f.timestamp.iloc[0] + pd.Timedelta(minutes=61), values={"order_book_imbalance": .75})
    joined = join_market_features(f.drop(columns=MICRO_FEATURES + [n + "_available" for n in MICRO_FEATURES]), [snapshot])
    assert joined.order_book_imbalance_available.tolist() == [0, 1, 0, 0]
    assert not joined.liquidation_long_distance_available.any()
    assert joined.order_book_imbalance.iloc[1] == .75


def test_reward_has_no_direction_bonus_and_churn_pays_both_legs():
    f = frame()
    f["ema_gap"] = 1
    env = CryptoTradingEnv(f, latency_ms=0, market_impact_bps=0)
    env.reset()
    _, reward, _, _, _ = env.step(1)
    assert reward == 0
    _, reward, _, _, info = env.step(0)
    assert reward < 0
    assert info["strategy_bonus"] == 0
    assert info["execution_cost_percent"] == pytest.approx(.12)


def test_terminal_liquidation_and_losing_holding_penalty():
    env = CryptoTradingEnv(frame(), latency_ms=0, market_impact_bps=0)
    env.reset()
    rewards = []
    for _ in range(119):
        _, reward, done, _, info = env.step(1)
        rewards.append(reward)
    assert done and env.position == 0
    assert info["execution_cost_percent"] == pytest.approx(.12)
    assert info["behavior_penalty"] < 0
    assert sum(rewards) < 0


def test_walk_forward_purges_boundary_and_never_overlaps_outer_tests():
    folds = walk_forward_windows(frame(24 * 150))
    assert len(folds) == 2
    for train, test in folds:
        assert train.timestamp.max() + pd.Timedelta(hours=1) < test.timestamp.min()
    assert folds[0][1].timestamp.max() < folds[1][1].timestamp.min()
    with pytest.raises(ValueError):
        walk_forward_windows(frame(1000))
    with pytest.raises(ValueError, match="contiguous"):
        walk_forward_windows(frame(24 * 150).drop(index=55))


def test_outer_test_does_not_select_candidate_or_seed(monkeypatch):
    service = RlTrainingService()
    f = frame(24 * 150)
    for name in MICRO_FEATURES:
        f[name + "_available"] = 1
    boundaries = []
    def select_candidate(train, validation):
        assert train.timestamp.max() < validation.timestamp.min()
        boundaries.append(validation.timestamp.max())
        return object(), {"profitable_seed_ratio": 1, "seed": 1}
    def evaluate(model, test):
        assert boundaries[-1] < test.timestamp.min()
        return {"return_percent": 1, "max_drawdown_percent": 1, "profit_factor": 2, "trades": 30}
    monkeypatch.setattr(service, "_train_candidates", select_candidate)
    monkeypatch.setattr(service, "_evaluate", evaluate)
    _, metrics = service._train_walk_forward(f)
    assert len(metrics["walk_forward_folds"]) == 2
    assert len(boundaries) == 2


def test_missing_liquidation_data_prevents_training(monkeypatch):
    service = RlTrainingService()
    fit = Mock(side_effect=AssertionError("Must not train on missing features"))
    monkeypatch.setattr(service, "_train_candidates", fit)
    with pytest.raises(ValueError, match="coverage"):
        service._train_walk_forward(frame(24 * 150))
    fit.assert_not_called()


def test_new_reward_and_features_can_train_ppo_without_nan():
    from stable_baselines3 import PPO
    f = frame()
    f["close"] = np.linspace(100, 105, len(f))
    env = CryptoTradingEnv(f)
    model = PPO("MlpPolicy", env, n_steps=32, batch_size=32, n_epochs=1,
        policy_kwargs={"net_arch": [8]}, seed=7, device="cpu", verbose=0)
    model.learn(total_timesteps=64)
    observation, _ = env.reset()
    action, _ = model.predict(observation, deterministic=True)
    assert int(action) in {0, 1, 2}
    assert np.isfinite(observation).all()
