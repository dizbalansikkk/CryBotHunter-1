from types import SimpleNamespace

import pytest

from app.services.agent_competition import AgentCompetitionService


def performance(name: str, role: str, status: str, rating: float, observations: int):
    return SimpleNamespace(
        agent_name=name,
        role=role,
        status=status,
        rating=rating,
        observations=observations,
        trust_history=[{}] * observations,
        promoted_at=None,
    )


def test_reward_checks_prediction_against_real_trade_outcome():
    service = AgentCompetitionService()

    reward, success = service._reward(action="BUY", position_side="LONG", pnl=25, result_r=1.2)
    wrong_reward, wrong = service._reward(action="SELL", position_side="LONG", pnl=25, result_r=1.2)

    assert success is True
    assert reward > 0
    assert wrong is False
    assert wrong_reward == 0  # An unexecuted alternative has no realized PnL.


def test_reward_cannot_be_increased_by_self_reported_confidence():
    service = AgentCompetitionService()

    confident_win, _ = service._reward(
        action="BUY", position_side="LONG", pnl=25, result_r=1, confidence=0.95
    )
    uncertain_win, _ = service._reward(
        action="BUY", position_side="LONG", pnl=25, result_r=1, confidence=0.55
    )
    confident_loss, _ = service._reward(
        action="BUY", position_side="LONG", pnl=-25, result_r=-1, confidence=0.95
    )

    assert confident_win == uncertain_win == 1
    assert confident_loss == -1


@pytest.mark.asyncio
async def test_better_challenger_replaces_worse_champion_after_enough_evidence(monkeypatch):
    service = AgentCompetitionService()
    rows = {
        "TrendAgent": performance("TrendAgent", "TREND", "CHAMPION", 0.38, 20),
        "AdaptiveTrendAgent": performance("AdaptiveTrendAgent", "TREND", "CHALLENGER", 0.64, 20),
        "MomentumAgent": performance("MomentumAgent", "MOMENTUM", "CHAMPION", 0.60, 20),
        "BreakoutAgent": performance("BreakoutAgent", "MOMENTUM", "CHALLENGER", 0.58, 20),
    }

    async def get_or_create(_db, name, _role):
        return rows[name]

    monkeypatch.setattr(service, "_get_or_create", get_or_create)
    changes = await service._reconcile_roles(object())

    assert rows["AdaptiveTrendAgent"].status == "CHAMPION"
    assert rows["TrendAgent"].status == "POOR"
    assert changes == [{"role": "TREND", "previous": "TrendAgent", "champion": "AdaptiveTrendAgent"}]
