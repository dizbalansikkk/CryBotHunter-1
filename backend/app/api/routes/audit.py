import asyncio
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
from app.services.control_dashboard import ControlDashboardService


router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/control/requests")
async def control_requests(_: User = Depends(current_user)):
    from app.services.request_diagnostics import request_diagnostics
    return await asyncio.to_thread(request_diagnostics)


@router.get("/control/entries")
async def control_entries(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await ControlDashboardService().entries(db)


@router.get("/control/positions")
async def control_positions(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await ControlDashboardService().positions(db)


@router.get("/control/positions/{position_id}")
async def control_position(position_id: int, _: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    result = await ControlDashboardService().lifecycle(db, position_id)
    if result is None:
        raise HTTPException(404, "Позиция не найдена")
    return result


@router.get("/control/learning")
async def control_learning(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await ControlDashboardService().learning(db)


@router.get("/control/storage")
async def control_storage(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    return await ControlDashboardService().storage(db)


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
