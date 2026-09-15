from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db.session import get_db
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models.entities import LearningRule, Position, RlModel, ShadowTrade, TradePostMortem, User
from app.schemas.dto import (
    LearningInsightsOut,
    LearningProgressOut,
    LearningRuleOut,
    LearningSummaryOut,
    RlModelOut,
    ShadowTradeOut,
    StrategyOptimizationOut,
    TradePostMortemOut,
)
from app.services.learning import LearningService
from app.services.learning_progress import LearningProgressService
from app.services.optimizer import StrategyOptimizerService

router = APIRouter(prefix="/strategy-lab", tags=["strategy-lab"])


@router.post("/optimize", response_model=list[StrategyOptimizationOut])
async def optimize(
    symbol: str = "ETH/USDT",
    timeframe: str = "1h",
    limit: int = 500,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[StrategyOptimizationOut]:
    bounded_limit = max(220, min(limit, 1000))
    return await StrategyOptimizerService().optimize(db, symbol=symbol, timeframe=timeframe, limit=bounded_limit)


@router.get("/results", response_model=list[StrategyOptimizationOut])
async def results(_: User = Depends(current_user), db: AsyncSession = Depends(get_db), limit: int = 20) -> list[StrategyOptimizationOut]:
    bounded_limit = max(1, min(limit, 100))
    return await StrategyOptimizerService().recent(db, limit=bounded_limit)


@router.get("/rl-models", response_model=list[RlModelOut])
async def rl_models(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 20,
) -> list[RlModelOut]:
    bounded_limit = max(1, min(limit, 100))
    statement = select(RlModel)
    excluded = get_settings().trading_excluded_symbols
    if excluded:
        statement = statement.where(RlModel.symbol.not_in(excluded))
    result = await db.execute(statement.order_by(RlModel.created_at.desc()).limit(bounded_limit))
    return list(result.scalars().all())


@router.get("/shadow-trades", response_model=list[ShadowTradeOut])
async def shadow_trades(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 30,
) -> list[ShadowTradeOut]:
    bounded_limit = max(1, min(limit, 100))
    statement = select(ShadowTrade)
    excluded = get_settings().trading_excluded_symbols
    if excluded:
        statement = statement.where(ShadowTrade.symbol.not_in(excluded))
    result = await db.execute(statement.order_by(ShadowTrade.entered_at.desc()).limit(bounded_limit))
    return list(result.scalars().all())


@router.get("/post-mortems", response_model=list[TradePostMortemOut])
async def post_mortems(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 30,
) -> list[TradePostMortemOut]:
    bounded_limit = max(1, min(limit, 100))
    result = await db.execute(
        select(TradePostMortem)
        .order_by(TradePostMortem.closed_at.desc(), TradePostMortem.priority.desc())
        .limit(bounded_limit)
    )
    return list(result.scalars().all())


@router.get("/learning-rules", response_model=list[LearningRuleOut])
async def learning_rules(_: User = Depends(current_user), db: AsyncSession = Depends(get_db), limit: int = 50) -> list[LearningRuleOut]:
    bounded_limit = max(1, min(limit, 200))
    result = await db.execute(
        select(LearningRule)
        .order_by(LearningRule.penalty.desc(), LearningRule.updated_at.desc())
        .limit(bounded_limit)
    )
    service = LearningService()
    return [
        LearningRuleOut(
            **rule.__dict__,
            confidence=round(service.rule_confidence(rule.observations, rule.updated_at), 2),
            risk_level=service.risk_level_for_rule(rule),
        )
        for rule in result.scalars().all()
    ]


@router.get("/learning-summary", response_model=LearningSummaryOut)
async def learning_summary(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> LearningSummaryOut:
    result = await db.execute(select(LearningRule))
    service = LearningService()
    rules = list(result.scalars().all())
    levels = [service.risk_level_for_rule(rule) for rule in rules]
    return LearningSummaryOut(
        total_rules=len(rules),
        watch_rules=sum(1 for level in levels if level == "WATCH"),
        warn_rules=sum(1 for level in levels if level == "WARN"),
        block_rules=sum(1 for level in levels if level == "BLOCK"),
        total_observations=sum(rule.observations for rule in rules),
        total_losses=sum(rule.losses for rule in rules),
        total_wins=sum(rule.wins for rule in rules),
    )


@router.get("/learning-insights", response_model=LearningInsightsOut)
async def learning_insights(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 12,
) -> LearningInsightsOut:
    rules = list((await db.execute(select(LearningRule))).scalars().all())
    learned_from_trades = int(
        (
            await db.execute(
                select(func.count()).select_from(Position).where(Position.status == "CLOSED")
            )
        ).scalar_one()
    )
    return LearningService().build_insights(rules, learned_from_trades, limit=max(1, min(limit, 50)))


@router.get("/learning-progress", response_model=LearningProgressOut)
async def learning_progress(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> LearningProgressOut:
    return await LearningProgressService().build(db)
