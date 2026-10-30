from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import AgentDecision


@dataclass(frozen=True)
class RlGateAssessment:
    allowed: bool
    risk_multiplier: float
    reason: str


class RlDecisionGate:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def assess(self, db: AsyncSession, symbol: str, signal: str) -> RlGateAssessment:
        if not self.settings.rl_gate_enabled:
            return RlGateAssessment(True, 1.0, "RL gate disabled")
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(self.settings.rl_gate_max_age_hours, 0.1))
        decision = (
            await db.execute(
                select(AgentDecision)
                .where(
                    AgentDecision.agent_name == "rl_policy",
                    AgentDecision.symbol == symbol,
                    AgentDecision.created_at >= cutoff,
                )
                .order_by(AgentDecision.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if not decision:
            return RlGateAssessment(True, 1.0, "no fresh promoted RL decision")
        if (getattr(decision, "context", None) or {}).get("feature_schema_version") != 3:
            return RlGateAssessment(True, 1.0, "legacy RL schema has no voting authority")
        if decision.confidence < self.settings.rl_gate_min_confidence:
            return RlGateAssessment(True, self.settings.rl_wait_risk_multiplier, "RL confidence is below promotion gate")
        if decision.action == signal:
            return RlGateAssessment(True, 1.0, f"RL agrees ({decision.action}, {decision.confidence:.2f})")
        if decision.action == "WAIT":
            return RlGateAssessment(True, self.settings.rl_wait_risk_multiplier, "RL is neutral; risk reduced")
        return RlGateAssessment(False, 0.0, f"RL disagrees: strategy={signal}, RL={decision.action}")
