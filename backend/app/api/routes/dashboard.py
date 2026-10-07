import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.config import get_settings
from app.db.session import get_db
from app.models.entities import Position, User, UserSettings
from app.schemas.dto import DashboardOut
from app.services.exchange import ExchangeClient
from app.services.pnl import PnlMetricsService
from app.services.trade_analytics import TradeAnalyticsService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
logger = logging.getLogger(__name__)


def paper_equity(starting_balance: float, total_pnl: float) -> tuple[float, float, float]:
    """Return paper account equity and its movement from the configured base."""
    baseline = float(starting_balance)
    change = float(total_pnl)
    return (
        round(baseline + change, 4),
        round(change, 4),
        round(change / baseline * 100, 4),
    )


@router.get("", response_model=DashboardOut)
async def dashboard(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> DashboardOut:
    user_settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one()
    positions = (await db.execute(select(Position).where(Position.status == "OPEN").order_by(Position.entered_at.desc()))).scalars().all()
    pnl = await PnlMetricsService().summary(db)
    analytics = await TradeAnalyticsService().summary(db)
    settings = get_settings()
    paper_mode = settings.paper_trading or not settings.live_trading_enabled
    starting_balance: float | None = None
    balance_change: float | None = None
    balance_change_percent: float | None = None
    balance_source = "EXCHANGE"
    if paper_mode:
        starting_balance = float(settings.paper_starting_balance)
        balance, balance_change, balance_change_percent = paper_equity(starting_balance, pnl.total_pnl)
        balance_source = "PAPER"
    else:
        exchange = ExchangeClient.from_user_settings(user_settings)
        try:
            balance = (await exchange.get_balance()).get("USDT", 0)
        except Exception:
            logger.exception("Не удалось получить баланс биржи для панели управления")
            balance = 0
        finally:
            await exchange.close()
    return DashboardOut(
        balance=balance,
        starting_balance=starting_balance,
        balance_change=balance_change,
        balance_change_percent=balance_change_percent,
        balance_source=balance_source,
        pnl_day=pnl.pnl_day,
        pnl_week=pnl.pnl_week,
        win_rate=pnl.win_rate,
        trades_count=pnl.trades_count,
        active_positions=list(positions),
        analytics=analytics,
    )
