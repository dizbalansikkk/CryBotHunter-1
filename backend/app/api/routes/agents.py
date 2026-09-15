from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db.session import get_db
from app.models.entities import AgentDecision, User
from app.schemas.dto import AgentActivityOut, AgentAnalysisOut, AgentDecisionOut
from app.services.agent_activity import AgentActivityService
from app.services.agents import AgentOrchestrator

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("/analyze", response_model=AgentAnalysisOut)
async def analyze_symbol(
    symbol: str = "ETH/USDT",
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentAnalysisOut:
    return await AgentOrchestrator().analyze(db, symbol)


@router.get("/decisions", response_model=list[AgentDecisionOut])
async def recent_decisions(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 20,
) -> list[AgentDecisionOut]:
    rows = (
        await db.execute(select(AgentDecision).order_by(AgentDecision.created_at.desc()).limit(min(limit, 100)))
    ).scalars().all()
    return [
        AgentDecisionOut(
            agent_name=row.agent_name,
            symbol=row.symbol,
            action=row.action,
            confidence=row.confidence,
            rationale=row.rationale,
            context=row.context or {},
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/activity", response_model=AgentActivityOut)
async def agent_activity(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 1000,
) -> AgentActivityOut:
    bounded_limit = max(50, min(limit, 5000))
    rows = list(
        (
            await db.execute(
                select(AgentDecision).order_by(AgentDecision.created_at.desc()).limit(bounded_limit)
            )
        ).scalars().all()
    )
    return AgentActivityService().summarize(rows)
