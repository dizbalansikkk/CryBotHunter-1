from datetime import datetime, timedelta, timezone
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db.session import get_db
from app.models.entities import Candle, LogEntry, Order, OrderStatus, Position, Trade, User, UserSettings
from app.schemas.dto import PositionOut, TradeChartCandleOut, TradeChartLevelOut, TradeChartMarkerOut, TradeChartOut
from app.services.context_manager import ContextManager
from app.services.exchange import ExchangeClient
from app.services.execution import ExecutionService
from app.services.learning import LearningService
from app.services.locks import RedisLockManager, TRADING_CYCLE_LOCK
from app.services.post_mortem import PostMortemService

router = APIRouter(prefix="/positions", tags=["positions"])

_CHART_TIMEFRAMES = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14_400,
    "12h": 43_200,
    # CCXT/Binance call the 24-hour timeframe "1d".  The user interface
    # deliberately presents it as "24 часа".
    "1d": 86_400,
}
_CHART_MAX_POINTS = 600


def _log_position_event(
    db: AsyncSession,
    level: str,
    event: str,
    message: str,
    **details: object,
) -> None:
    """Keep manual actions searchable in the same audit stream as worker actions."""
    context = {"event": event}
    context.update({key: value for key, value in details.items() if value is not None})
    db.add(LogEntry(level=level, message=message, context=context))


def _executed_volume(value: object, maximum: float) -> float:
    try:
        volume = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(min(max(volume, 0.0), max(float(maximum), 0.0)), 8)


@router.get("", response_model=list[PositionOut])
async def list_positions(
    limit: int = Query(default=80, ge=1, le=250),
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Position]:
    return list((await db.execute(select(Position).order_by(Position.entered_at.desc()).limit(limit))).scalars().all())


@router.get("/{position_id}/chart", response_model=TradeChartOut)
async def position_chart(
    position_id: int,
    timeframe: str = Query(default="1h", pattern="^(1m|5m|15m|1h|4h|12h|1d)$"),
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> TradeChartOut:
    """Return only persisted chart data and recorded trade levels.

    This is deliberately database-backed: a historical trade graph must not
    silently substitute today's public price path for the one available when a
    position was opened or closed.
    """
    position = (await db.execute(select(Position).where(Position.id == position_id))).scalar_one_or_none()
    if not position:
        raise HTTPException(status_code=404, detail="Position not found")

    interval_seconds = _CHART_TIMEFRAMES[timeframe]
    entered_at = _as_utc(position.entered_at)
    ended_at = _as_utc(position.closed_at) if position.closed_at else datetime.now(timezone.utc)
    # Preserve visible context before entry and after final exit.  The returned
    # sample is then bounded for browser performance without manufacturing OHLC.
    start_at = entered_at - timedelta(seconds=interval_seconds * 48)
    end_at = ended_at + timedelta(seconds=interval_seconds * 12)
    candles = list(
        (
            await db.execute(
                select(Candle)
                .where(
                    Candle.symbol == position.symbol,
                    Candle.timeframe == timeframe,
                    Candle.timestamp >= start_at,
                    Candle.timestamp <= end_at,
                )
                .order_by(Candle.timestamp.asc())
            )
        ).scalars().all()
    )
    aggregated_from_hourly = False
    if not candles and timeframe in {"4h", "12h", "1d"}:
        # Most installations retain the economical 1h history.  Aggregate
        # those persisted OHLCV facts when a larger display interval was not
        # ingested separately; never replace historical data with a fresh
        # public-market path just to fill the chart.
        hourly_candles = list(
            (
                await db.execute(
                    select(Candle)
                    .where(
                        Candle.symbol == position.symbol,
                        Candle.timeframe == "1h",
                        Candle.timestamp >= start_at,
                        Candle.timestamp <= end_at,
                    )
                    .order_by(Candle.timestamp.asc())
                )
            ).scalars().all()
        )
        candles = _aggregate_hourly_candles(hourly_candles, timeframe)
        aggregated_from_hourly = bool(candles)
    sampled, sample_note = _downsample_candles(candles, _CHART_MAX_POINTS)
    if not sampled:
        data_note = (
            f"Недостаточно данных для отображения свечного графика: в локальной истории "
            f"нет сохранённых свечей {position.symbol} на таймфрейме {timeframe} в период сделки."
        )
    elif aggregated_from_hourly:
        data_note = (
            f"Показаны {len(sampled)} свечей {timeframe}, агрегированных из сохранённых 1h-свечей; "
            "OHLCV рассчитан только из локальной истории, без подмены данных биржей."
        )
        if sample_note:
            data_note = f"{data_note} {sample_note}"
    elif sample_note:
        data_note = sample_note
    else:
        sources = ", ".join(sorted({str(candle.source) for candle in sampled if candle.source}))
        data_note = f"Показаны сохранённые свечи: {len(sampled)} шт.; источник: {sources or 'не указан'}."

    return TradeChartOut(
        position_id=position.id,
        symbol=position.symbol,
        side=position.side,
        status=position.status,
        timeframe=timeframe,
        candles=[
            TradeChartCandleOut(
                timestamp=candle.timestamp,
                open=float(candle.open),
                high=float(candle.high),
                low=float(candle.low),
                close=float(candle.close),
                volume=float(candle.volume),
                source=candle.source,
            )
            for candle in sampled
        ],
        levels=_chart_levels(position),
        markers=_chart_markers(position),
        data_note=data_note,
    )


def _chart_levels(position: Position) -> list[TradeChartLevelOut]:
    context = position.entry_context if isinstance(position.entry_context, dict) else {}
    scale_out = context.get("scale_out") if isinstance(context.get("scale_out"), dict) else {}
    breakeven = context.get("breakeven_protection") if isinstance(context.get("breakeven_protection"), dict) else {}
    stop_execution = context.get("stop_execution") if isinstance(context.get("stop_execution"), dict) else {}
    levels = [
        TradeChartLevelOut(key="entry", label="Вход", price=float(position.entry_price), kind="ENTRY"),
        TradeChartLevelOut(key="stop", label="SL (последний сохранённый)", price=float(position.stop), kind="STOP"),
        TradeChartLevelOut(key="take", label="TP3 / целевой", price=float(position.take), kind="TAKE"),
    ]
    for key, label, kind in (
        ("tp1_price", "TP1 / безубыток", "BREAKEVEN"),
        ("tp2_price", "TP2 (план)", "TAKE"),
    ):
        price = _positive_number(scale_out.get(key))
        if price is not None:
            levels.append(TradeChartLevelOut(key=key, label=label, price=price, kind=kind))
    price_bu = _positive_number(breakeven.get("price_bu"))
    if price_bu is not None and not any(abs(level.price - price_bu) < 1e-12 for level in levels):
        levels.append(TradeChartLevelOut(key="price_bu", label="Безубыток", price=price_bu, kind="BREAKEVEN"))
    for stage, label, kind in (
        ("tp1", "TP1 исполнен (факт)", "BREAKEVEN"),
        ("tp2", "TP2 исполнен (факт)", "TAKE"),
    ):
        fill_price = _positive_number(scale_out.get(f"{stage}_fill_price"))
        if fill_price is not None:
            levels.append(TradeChartLevelOut(key=f"{stage}_fill", label=label, price=fill_price, kind=kind))
    stop_fill_price = _positive_number(stop_execution.get("actual_price"))
    if stop_fill_price is not None:
        levels.append(TradeChartLevelOut(key="stop_fill", label="Стоп исполнен (факт)", price=stop_fill_price, kind="EXIT"))
    return levels


def _chart_markers(position: Position) -> list[TradeChartMarkerOut]:
    markers = [
        TradeChartMarkerOut(
            key="entry",
            label="Вход",
            timestamp=_as_utc(position.entered_at),
            price=float(position.entry_price),
            kind="ENTRY",
        )
    ]
    if position.closed_at is not None:
        markers.append(
            TradeChartMarkerOut(
                key="exit",
                label="Фактический выход",
                timestamp=_as_utc(position.closed_at),
                price=float(position.current_price),
                kind="EXIT",
            )
        )
    return markers


def _downsample_candles(candles: list[Candle], limit: int) -> tuple[list[Candle], str | None]:
    if len(candles) <= limit:
        return candles, None
    step = (len(candles) - 1) / max(limit - 1, 1)
    indices = sorted({round(index * step) for index in range(limit)})
    sampled = [candles[index] for index in indices]
    return sampled, f"Показано {len(sampled)} из {len(candles)} сохранённых свечей; ряд прорежен только для отображения."


def _aggregate_hourly_candles(candles: list[Candle], timeframe: str) -> list[Candle]:
    """Build larger visual candles strictly from persisted 1h OHLCV rows."""
    interval_seconds = _CHART_TIMEFRAMES.get(timeframe)
    if interval_seconds is None or interval_seconds <= _CHART_TIMEFRAMES["1h"]:
        return []
    buckets: dict[datetime, list[Candle]] = {}
    for candle in candles:
        timestamp = _as_utc(candle.timestamp)
        bucket_epoch = int(timestamp.timestamp() // interval_seconds) * interval_seconds
        bucket = datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)
        buckets.setdefault(bucket, []).append(candle)

    aggregated: list[Candle] = []
    for timestamp, rows in sorted(buckets.items()):
        ordered = sorted(rows, key=lambda item: _as_utc(item.timestamp))
        first, last = ordered[0], ordered[-1]
        aggregated.append(
            Candle(
                symbol=first.symbol,
                timeframe=timeframe,
                timestamp=timestamp,
                open=float(first.open),
                high=max(float(item.high) for item in ordered),
                low=min(float(item.low) for item in ordered),
                close=float(last.close),
                volume=sum(float(item.volume) for item in ordered),
                source="aggregated_1h",
            )
        )
    return aggregated


def _positive_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and number == number else None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@router.post("/{position_id}/close", response_model=PositionOut)
async def close_position(
    position_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Position:
    locks = RedisLockManager()
    user_settings = (
        await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    ).scalar_one()
    execution = ExecutionService(ExchangeClient.from_user_settings(user_settings))
    try:
        async with locks.lock(TRADING_CYCLE_LOCK, ttl_seconds=55) as acquired:
            if not acquired:
                raise HTTPException(
                    status_code=409,
                    detail="Another trading operation is running; retry the close shortly",
                )
            return await _close_position_locked(position_id, db, execution)
    finally:
        await execution.exchange.close()
        await locks.close()


async def _close_position_locked(
    position_id: int,
    db: AsyncSession,
    execution: ExecutionService,
) -> Position:
    position = (await db.execute(select(Position).where(Position.id == position_id))).scalar_one_or_none()
    if not position:
        raise HTTPException(status_code=404, detail="Position not found")
    if position.status != "OPEN":
        raise HTTPException(status_code=409, detail="Position is already closed")
    pending_order = (
        await db.execute(
            select(Order)
            .where(Order.symbol == position.symbol, Order.status == OrderStatus.NEW.value)
            .order_by(Order.created_at.desc())
        )
    ).scalars().first()
    if pending_order:
        _log_position_event(
            db,
            "WARNING",
            "POSITION_MANAGEMENT_PAUSED",
            f"Position management paused for {position.symbol} #{position.id}: pending exchange order",
            symbol=position.symbol,
            position_id=position.id,
            side=position.side,
            gate="EXECUTION",
            order_id=pending_order.id,
        )
        await db.commit()
        raise HTTPException(status_code=409, detail="A previous exchange order is still pending for this position")
    requested_volume = position.volume
    exit_order = await execution.execute_market(
        db,
        position.symbol,
        "sell" if position.side == "LONG" else "buy",
        requested_volume,
        position.current_price,
        "EXIT_MANUAL",
    )
    if (
        exit_order.status not in {OrderStatus.FILLED.value, OrderStatus.PARTIAL.value}
        or not exit_order.average_price
    ):
        _log_position_event(
            db,
            "ERROR",
            "POSITION_CLOSE_FAILED",
            f"Failed to close {position.symbol} #{position.id}: MANUAL",
            symbol=position.symbol,
            position_id=position.id,
            side=position.side,
            exit_reason="MANUAL",
            requested_volume=requested_volume,
            requested_price=position.current_price,
            order_status=exit_order.status,
            filled_volume=exit_order.filled_amount,
        )
        await db.commit()
        raise HTTPException(status_code=502, detail="Exchange did not fill the closing order")
    closed_volume = _executed_volume(exit_order.filled_amount, requested_volume)
    if closed_volume <= 0:
        _log_position_event(
            db,
            "ERROR",
            "POSITION_CLOSE_FAILED",
            f"Failed to close {position.symbol} #{position.id}: MANUAL, no executed volume",
            symbol=position.symbol,
            position_id=position.id,
            side=position.side,
            exit_reason="MANUAL",
            requested_volume=requested_volume,
            requested_price=position.current_price,
            order_status=exit_order.status,
            filled_volume=exit_order.filled_amount,
        )
        await db.commit()
        raise HTTPException(status_code=502, detail="Exchange did not report an executed closing volume")
    if closed_volume < requested_volume * 0.999999:
        position.current_price = exit_order.average_price
        partial_profit = (position.current_price - position.entry_price) * closed_volume
        if position.side == "SHORT":
            partial_profit *= -1
        partial_profit = round(partial_profit - exit_order.fee, 4)
        position.volume = round(max(requested_volume - closed_volume, 0.0), 8)
        db.add(
            Trade(
                position_id=position.id,
                symbol=position.symbol,
                side=position.side,
                entry_price=position.entry_price,
                exit_price=position.current_price,
                profit=partial_profit,
            )
        )
        position_trades = list(
            (await db.execute(select(Trade).where(Trade.position_id == position.id))).scalars().all()
        )
        existing_profit = sum(float(item.profit or 0) for item in position_trades)
        if not any(item.exit_price is None for item in position_trades):
            legacy_open_trade = (
                await db.execute(
                    select(Trade)
                    .where(Trade.symbol == position.symbol, Trade.exit_price.is_(None))
                    .order_by(Trade.created_at.desc())
                )
            ).scalars().first()
            if legacy_open_trade:
                existing_profit += float(legacy_open_trade.profit or 0)
        remaining_profit = (position.current_price - position.entry_price) * position.volume
        if position.side == "SHORT":
            remaining_profit *= -1
        position.pnl = round(existing_profit + remaining_profit, 4)
        _log_position_event(
            db,
            "WARNING",
            "POSITION_EXIT_PARTIALLY_FILLED",
            f"Partially closed {position.symbol} #{position.id}: MANUAL, remaining={position.volume:.6f}",
            symbol=position.symbol,
            position_id=position.id,
            side=position.side,
            exit_reason="MANUAL",
            requested_volume=requested_volume,
            filled_volume=closed_volume,
            remaining_volume=position.volume,
            exit_price=exit_order.average_price,
            exit_fee=exit_order.fee,
            partial_profit=partial_profit,
            pnl=position.pnl,
        )
        await db.commit()
        await db.refresh(position)
        return position
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
    try:
        post_mortem = await PostMortemService(execution.exchange).analyze_loss(db, position, exit_order, "MANUAL")
        if post_mortem:
            _log_position_event(
                db,
                "WARNING",
                "POST_MORTEM_CREATED",
                (
                    f"Post-mortem {position.symbol} #{position.id}: "
                    f"label={post_mortem.primary_label}, reward={post_mortem.shaped_reward:+.2f}, "
                    f"priority={post_mortem.priority:.2f}"
                ),
                symbol=position.symbol,
                position_id=position.id,
                exit_reason="MANUAL",
                primary_label=post_mortem.primary_label,
                shaped_reward=round(float(post_mortem.shaped_reward), 4),
                priority=round(float(post_mortem.priority), 4),
            )
    except Exception as exc:
        _log_position_event(
            db,
            "ERROR",
            "POST_MORTEM_FAILED",
            f"Post-mortem failed for {position.symbol} #{position.id}: {type(exc).__name__}",
            symbol=position.symbol,
            position_id=position.id,
            exit_reason="MANUAL",
            error_type=type(exc).__name__,
        )
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
        _log_position_event(
            db,
            "ERROR",
            "TRADE_MEMORY_FAILED",
            f"SQLite trade memory failed for {position.symbol} #{position.id}: {exc}",
            symbol=position.symbol,
            position_id=position.id,
            error_type=type(exc).__name__,
        )
    entry_context = position.entry_context if isinstance(position.entry_context, dict) else {}
    entry_execution = entry_context.get("entry_execution", {})
    _log_position_event(
        db,
        "INFO",
        "POSITION_CLOSED",
        f"Closed position {position.symbol} #{position.id}: MANUAL, pnl={position.pnl:.2f}",
        symbol=position.symbol,
        position_id=position.id,
        side=position.side,
        exit_reason="MANUAL",
        pnl=position.pnl,
        entry_price=position.entry_price,
        exit_price=exit_order.average_price,
        volume=requested_volume,
        entry_fee=entry_execution.get("fee") if isinstance(entry_execution, dict) else None,
        exit_fee=exit_order.fee,
        exit_slippage=exit_order.slippage,
    )
    _log_position_event(
        db,
        "INFO",
        "LEARNING_UPDATED",
        f"Learning updated from {position.symbol} #{position.id}: reason=MANUAL, pnl={position.pnl:.2f}",
        symbol=position.symbol,
        position_id=position.id,
        exit_reason="MANUAL",
        pnl=position.pnl,
    )
    await db.commit()
    await db.refresh(position)
    return position
