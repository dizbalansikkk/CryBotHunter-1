from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Response, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db.session import get_db
from app.models.entities import LogEntry, Order, Position, Trade, TradePostMortem, User
from app.schemas.dto import LogOut
from app.services.trade_export import TradeAuditExportService

router = APIRouter(prefix="/logs", tags=["logs"])


EXIT_AUDIT_EVENTS = (
    "POSITION_PARTIALLY_CLOSED", "SECOND_TAKE_PROFIT_FILLED",
    "DYNAMIC_TAKE_PROFIT_EXTENDED", "POSITION_EXIT_PARTIALLY_FILLED", "POSITION_CLOSED",
    "BREAKEVEN_APPLIED", "PROTECTIVE_STOP_CONFIRMED", "PROTECTIVE_STOP_UNCONFIRMED",
    "BREAKEVEN_STOP_CONFIRMATION_PENDING", "PARTIAL_TAKE_PROFIT_FAILED",
    "SECOND_TAKE_PROFIT_FAILED", "DYNAMIC_TAKE_PROFIT_FAILED", "POSITION_CLOSE_FAILED",
    "SCALE_OUT_CANCELLED_MIN_NOTIONAL",
)


@router.get("/trading-audit")
async def trading_audit(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> Response:
    positions = list((await db.execute(select(Position).order_by(Position.entered_at.asc()))).scalars().all())
    trades = list((await db.execute(select(Trade).order_by(Trade.created_at.asc()))).scalars().all())
    orders = list((await db.execute(select(Order).order_by(Order.created_at.asc()))).scalars().all())
    post_mortems = list(
        (await db.execute(select(TradePostMortem).order_by(TradePostMortem.closed_at.asc()))).scalars().all()
    )
    event_filter = or_(
        LogEntry.context["event"].as_string().in_(EXIT_AUDIT_EVENTS),
        LogEntry.message.ilike("Opened % position for %"),
        LogEntry.message.ilike("Closed %"),
        LogEntry.message.ilike("Partially closed %"),
        LogEntry.message.ilike("Dynamic take profit extended%"),
        LogEntry.message.ilike("Dynamic take profit failed%"),
        LogEntry.message.ilike("Moved % stop to breakeven%"),
        LogEntry.message.ilike("Failed to close %"),
        LogEntry.message.ilike("Failed partial take profit %"),
        LogEntry.message.ilike("Post-mortem %"),
        LogEntry.message.ilike("Learning updated from %"),
        LogEntry.message.ilike("Skipped %"),
        LogEntry.message.ilike("Performance guard blocked entries%"),
        LogEntry.message.ilike("AI committee %"),
        LogEntry.message.ilike("Position % cannot be managed: price unavailable"),
        LogEntry.message.ilike("Position ticker snapshot failed%"),
        LogEntry.message.ilike("Position market snapshot failed%"),
        LogEntry.message.ilike("Position management paused%"),
        LogEntry.message.ilike("Order % partially filled during reconciliation%"),
        LogEntry.message.ilike("Order % marked failed during reconciliation"),
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
async def logs(
    _: User = Depends(current_user), db: AsyncSession = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
    category: Literal["all", "exits"] = "all",
    position_id: int | None = Query(default=None, ge=1),
    before_id: int | None = Query(default=None, ge=1),
) -> list[LogEntry]:
    query = select(LogEntry)
    if category == "exits":
        query = query.where(LogEntry.context["event"].as_string().in_(EXIT_AUDIT_EVENTS))
    if position_id is not None:
        query = query.where(LogEntry.context["position_id"].as_string() == str(position_id))
    if before_id is not None:
        query = query.where(LogEntry.id < before_id)
    query = query.order_by(LogEntry.id.desc()).limit(limit)
    return list((await db.execute(query)).scalars().all())
