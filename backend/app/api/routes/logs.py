from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db.session import get_db
from app.models.entities import LogEntry, Order, Position, Trade, TradePostMortem, User
from app.schemas.dto import LogOut
from app.services.trade_export import TradeAuditExportService

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("/trading-audit")
async def trading_audit(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> Response:
    positions = list((await db.execute(select(Position).order_by(Position.entered_at.asc()))).scalars().all())
    trades = list((await db.execute(select(Trade).order_by(Trade.created_at.asc()))).scalars().all())
    orders = list((await db.execute(select(Order).order_by(Order.created_at.asc()))).scalars().all())
    post_mortems = list(
        (await db.execute(select(TradePostMortem).order_by(TradePostMortem.closed_at.asc()))).scalars().all()
    )
    event_filter = or_(
        LogEntry.message.ilike("Opened % position for %"),
        LogEntry.message.ilike("Closed %"),
        LogEntry.message.ilike("Partially closed %"),
        LogEntry.message.ilike("Moved % stop to breakeven%"),
        LogEntry.message.ilike("Failed to close %"),
        LogEntry.message.ilike("Failed partial take profit %"),
        LogEntry.message.ilike("Post-mortem %"),
        LogEntry.message.ilike("Learning updated from %"),
    )
    events = list(
        (await db.execute(select(LogEntry).where(event_filter).order_by(LogEntry.created_at.asc()))).scalars().all()
    )
    now = datetime.now(timezone.utc)
    payload = TradeAuditExportService().build_archive(
        positions=positions,
        trades=trades,
        orders=orders,
        post_mortems=post_mortems,
        events=events,
        generated_at=now,
    )
    filename = f"crybothunter-trading-audit-{now:%Y%m%d-%H%M%S}.zip"
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Export-Positions": str(len(positions)),
            "Cache-Control": "no-store",
        },
    )


@router.get("", response_model=list[LogOut])
async def logs(_: User = Depends(current_user), db: AsyncSession = Depends(get_db), limit: int = 100) -> list[LogEntry]:
    query = select(LogEntry).order_by(LogEntry.created_at.desc()).limit(min(limit, 500))
    return list((await db.execute(query)).scalars().all())
