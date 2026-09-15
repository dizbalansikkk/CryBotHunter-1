from datetime import datetime, timedelta, timezone

from app.models.entities import AgentDecision
from app.services.agent_activity import AgentActivityService


def decision(agent: str, action: str, confidence: float, created_at: datetime) -> AgentDecision:
    return AgentDecision(
        agent_name=agent,
        symbol="BTC/USDT",
        action=action,
        confidence=confidence,
        rationale="test",
        created_at=created_at,
    )


def test_agent_activity_makes_agent_work_visible():
    now = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)
    rows = [
        decision("TradeCommittee", "BUY", 0.8, now - timedelta(hours=1)),
        decision("TradeCommittee", "WAIT", 0.6, now - timedelta(days=2)),
        decision("RiskSupervisorAgent", "ALLOW", 0.9, now - timedelta(hours=2)),
    ]

    summary = AgentActivityService().summarize(rows, now=now)

    assert summary.total_decisions == 3
    assert summary.decisions_24h == 2
    assert summary.active_agents == 2
    assert summary.committee_approvals == 1
    committee = next(item for item in summary.agents if item.agent_name == "TradeCommittee")
    assert committee.decisions == 2
    assert committee.decisions_24h == 1
    assert committee.directional_votes == 1
    assert committee.waits == 1
    assert committee.average_confidence == 0.7
