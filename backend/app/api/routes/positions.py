from datetime import datetime, timezone
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db.session import get_db
from app.models.entities import LogEntry, OrderStatus, Position, Trade, User
from app.schemas.dto import PositionOut
from app.services.context_manager import ContextManager
from app.services.execution import ExecutionService
from app.services.learning import LearningService

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("", response_model=list[PositionOut])
async def list_positions(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[Position]:
    return list((await db.execute(select(Position).order_by(Position.entered_at.desc()))).scalars().all())


@router.post("/{position_id}/close", response_model=PositionOut)
async def close_position(position_id: int, _: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> Position:
    position = (await db.execute(select(Position).where(Position.id == position_id))).scalar_one_or_none()
    if not position:
        raise HTTPException(status_code=404, detail="Position not found")
    if position.status != "OPEN":
        raise HTTPException(status_code=409, detail="Position is already closed")
    exit_order = await ExecutionService().execute_market(
        db,
        position.symbol,
        "sell" if position.side == "LONG" else "buy",
        position.volume,
        position.current_price,
        "EXIT_MANUAL",
    )
    if exit_order.status != OrderStatus.FILLED.value or not exit_order.average_price:
        raise HTTPException(status_code=502, detail="Exchange did not fill the closing order")
    position.status = "CLOSED"
    position.exit_reason = "MANUAL"
    position.closed_at = datetime.now(timezone.utc)
    position.current_price = exit_order.average_price
    multiplier = 1 if position.side == "LONG" else -1
    remaining_profit = (position.current_price - position.entry_price) * position.volume * multiplier
    trade = (
        await db.execute(
            select(Trade)
            .where(Trade.position_id == position.id, Trade.exit_price.is_(None))
            .order_by(Trade.created_at.desc())
        )
    ).scalars().first()
    if not trade:
        trade = (
            await db.execute(
                select(Trade)
                .where(Trade.symbol == position.symbol, Trade.exit_price.is_(None))
                .order_by(Trade.created_at.desc())
            )
        ).scalars().first()
    realized_trades = list(
        (
            await db.execute(
                select(Trade).where(Trade.position_id == position.id, Trade.exit_price.is_not(None))
            )
        ).scalars().all()
    )
    previous_realized = sum(float(item.profit or 0) for item in realized_trades)
    if trade:
        trade.exit_price = position.current_price
        trade.profit = round(float(trade.profit or 0) + remaining_profit - exit_order.fee, 4)
        final_profit = trade.profit
    else:
        final_profit = round(remaining_profit - exit_order.fee, 4)
        db.add(
            Trade(
                position_id=position.id,
                symbol=position.symbol,
                side=position.side,
                entry_price=position.entry_price,
                exit_price=position.current_price,
                profit=final_profit,
            )
        )
    position.pnl = round(previous_realized + final_profit, 4)
    await LearningService().record_closed_position(db, position, position.pnl, "MANUAL")
    try:
        await ContextManager().remember_trade(
            symbol=position.symbol,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=position.current_price,
            pnl=position.pnl,
            exit_reason="MANUAL",
            timestamp=position.closed_at,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        db.add(LogEntry(level="ERROR", message=f"SQLite trade memory failed for {position.symbol} #{position.id}: {exc}"))
    db.add(LogEntry(level="INFO", message=f"Closed position {position.symbol} #{position.id}"))
    db.add(LogEntry(level="INFO", message=f"Learning updated from {position.symbol} #{position.id}: reason=MANUAL, pnl={position.pnl:.2f}"))
    await db.commit()
    await db.refresh(position)
    return position
