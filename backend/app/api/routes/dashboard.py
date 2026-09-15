import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db.session import get_db
from app.models.entities import Position, User, UserSettings
from app.schemas.dto import DashboardOut
from app.services.exchange import ExchangeClient
from app.services.pnl import PnlMetricsService
from app.services.trade_analytics import TradeAnalyticsService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
logger = logging.getLogger(__name__)


@router.get("", response_model=DashboardOut)
async def dashboard(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> DashboardOut:
    user_settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one()
    try:
        balance = (await ExchangeClient.from_user_settings(user_settings).get_balance()).get("USDT", 0)
    except Exception:
        logger.exception("Failed to fetch dashboard exchange balance")
        balance = 0
    positions = (await db.execute(select(Position).where(Position.status == "OPEN").order_by(Position.entered_at.desc()))).scalars().all()
    pnl = await PnlMetricsService().summary(db)
    analytics = await TradeAnalyticsService().summary(db)
    return DashboardOut(
        balance=balance,
        pnl_day=pnl.pnl_day,
        pnl_week=pnl.pnl_week,
        win_rate=pnl.win_rate,
        trades_count=pnl.trades_count,
        active_positions=list(positions),
        analytics=analytics,
    )
