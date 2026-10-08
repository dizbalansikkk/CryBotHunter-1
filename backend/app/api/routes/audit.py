from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db.session import get_db
from app.models.entities import User, UserSettings
from app.schemas.dto import TradingAuditOut
from app.services.trading_audit import TradingAuditService
from app.services.activity_audit import ActivityAuditService


router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/activity")
async def activity_audit(
    day: date | None = None,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if day and day > datetime.now(ZoneInfo(ActivityAuditService.timezone_name)).date():
        raise HTTPException(status_code=422, detail="Дата отчёта не может быть в будущем")
    return await ActivityAuditService().report(db, day)


@router.get("/trading-30d", response_model=TradingAuditOut)
async def trading_audit_30d(
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> TradingAuditOut:
    user_settings = (
        await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    ).scalar_one_or_none()
    return await TradingAuditService().report(db, user_settings)
