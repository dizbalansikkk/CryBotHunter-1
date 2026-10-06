from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import LearningRule, Order, Position, RlModel, TelegramOutboxMessage, WorkerHeartbeat
from app.services.heartbeat import expected_worker_names, worker_is_healthy
from app.services.pnl import PnlMetricsService


@dataclass(frozen=True)
class DailyPosition:
    symbol: str
    side: str
    pnl: float
    current_price: float


@dataclass(frozen=True)
class DailyPeriodReport:
    label: str
    start_at: datetime
    end_at: datetime
    opened: int
    closed: int
    wins: int
    losses: int
    win_rate: float
    gross_profit: float
    gross_loss: float
    net_pnl: float
    fees: float
    slippage: float
    long_entries: int
    short_entries: int
    symbols: tuple[str, ...]
    exit_reasons: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class DailyReportSnapshot:
    generated_at: datetime
    paper_trading: bool
    pnl_day: float
    pnl_week: float
    total_pnl: float
    open_pnl: float
    win_rate: float
    trades_count: int
    closed_today: int
    positions: tuple[DailyPosition, ...]
    learning_rules: int
    learning_observations: int
    active_rl_models: int
    healthy_workers: int
    total_workers: int
    unhealthy_workers: tuple[str, ...]
    pending_notifications: int
    failed_notifications: int
    report_date: date | None = None
    timezone_name: str = "Europe/Simferopol"
    periods: tuple[DailyPeriodReport, ...] = ()


class TelegramDailyReportService:
    def __init__(self, *, settings: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self.pnl = PnlMetricsService()

    async def snapshot(
        self,
        db: AsyncSession,
        *,
        now: datetime | None = None,
        report_date: date | None = None,
    ) -> DailyReportSnapshot:
        generated_at = _aware(now or datetime.now(timezone.utc))
        timezone_name = str(getattr(self.settings, "telegram_daily_report_timezone", "Europe/Simferopol"))
        report_timezone = _timezone(timezone_name)
        local_now = generated_at.astimezone(report_timezone)
        selected_date = report_date or local_now.date()
        day_start_local = datetime.combine(selected_date, time.min, tzinfo=report_timezone)
        day_end_local = day_start_local + timedelta(days=1)
        day_start = day_start_local.astimezone(timezone.utc)
        day_end = day_end_local.astimezone(timezone.utc)
        all_positions = list((await db.execute(select(Position))).scalars().all())
        all_orders = list(
            (
                await db.execute(
                    select(Order).where(Order.created_at >= day_start, Order.created_at < day_end)
                )
            ).scalars().all()
        )
        pnl = self.pnl.summarize_positions(all_positions, now=generated_at)
        open_positions = [item for item in all_positions if item.status == "OPEN"]
        day_closed = [
            item
            for item in all_positions
            if item.status == "CLOSED"
            and item.closed_at
            and day_start <= _aware(item.closed_at) < day_end
        ]
        closed_today = len(day_closed)
        realized_day = sum(float(item.pnl or 0.0) for item in day_closed)
        periods = self._periods(
            all_positions,
            all_orders,
            day_start_local=day_start_local,
            report_timezone=report_timezone,
        )

        learning_row = (
            await db.execute(
                select(
                    func.count(LearningRule.id),
                    func.coalesce(func.sum(LearningRule.observations), 0),
                )
            )
        ).one()
        active_models_statement = (
            select(func.count()).select_from(RlModel).where(RlModel.is_active.is_(True))
        )
        rl_symbols = getattr(self.settings, "rl_symbols", None)
        rl_timeframes = getattr(self.settings, "candle_ingest_timeframes", None)
        if rl_symbols:
            active_models_statement = active_models_statement.where(RlModel.symbol.in_(rl_symbols))
        if rl_timeframes:
            active_models_statement = active_models_statement.where(
                RlModel.timeframe.in_(rl_timeframes)
            )
        active_models = int(
            (await db.execute(active_models_statement)).scalar_one()
        )
        pending = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(TelegramOutboxMessage)
                    .where(TelegramOutboxMessage.status.in_(("PENDING", "RETRY", "SENDING")))
                )
            ).scalar_one()
        )
        failed = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(TelegramOutboxMessage)
                    .where(TelegramOutboxMessage.status == "FAILED")
                )
            ).scalar_one()
        )
        expected_workers = expected_worker_names(self.settings)
        heartbeat_statement = select(WorkerHeartbeat)
        if expected_workers:
            heartbeat_statement = heartbeat_statement.where(
                WorkerHeartbeat.worker_name.in_(expected_workers)
            )
        heartbeat_rows = (
            await db.execute(heartbeat_statement.order_by(WorkerHeartbeat.worker_name.asc()))
        ).scalars().all()
        heartbeats_by_name = {item.worker_name: item for item in heartbeat_rows}
        worker_names = expected_workers or tuple(sorted(heartbeats_by_name))
        stale_seconds = max(int(self.settings.worker_heartbeat_stale_seconds), 60)
        startup_grace_seconds = int(
            getattr(self.settings, "worker_heartbeat_startup_grace_seconds", 600)
        )
        long_task_grace_seconds = int(
            getattr(self.settings, "worker_heartbeat_long_task_grace_seconds", 900)
        )
        unhealthy_workers = tuple(
            worker_name
            for worker_name in worker_names
            if (
                worker_name not in heartbeats_by_name
                or not worker_is_healthy(
                    status=heartbeats_by_name[worker_name].status,
                    age_seconds=max(
                        int(
                            (
                                generated_at
                                - _aware(heartbeats_by_name[worker_name].last_seen_at)
                            ).total_seconds()
                        ),
                        0,
                    ),
                    base_seconds=stale_seconds,
                    detail=getattr(heartbeats_by_name[worker_name], "detail", None) or {},
                    startup_grace_seconds=startup_grace_seconds,
                    long_task_grace_seconds=long_task_grace_seconds,
                )
            )
        )

        return DailyReportSnapshot(
            generated_at=generated_at,
            paper_trading=bool(self.settings.paper_trading),
            pnl_day=round(realized_day + float(pnl.open_pnl), 4),
            pnl_week=float(pnl.pnl_week),
            total_pnl=float(pnl.total_pnl),
            open_pnl=float(pnl.open_pnl),
            win_rate=float(pnl.win_rate),
            trades_count=int(pnl.trades_count),
            closed_today=closed_today,
            positions=tuple(
                DailyPosition(
                    symbol=item.symbol,
                    side=item.side,
                    pnl=float(item.pnl or 0.0),
                    current_price=float(item.current_price),
                )
                for item in sorted(open_positions, key=lambda position: position.entered_at, reverse=True)
            ),
            learning_rules=int(learning_row[0]),
            learning_observations=int(learning_row[1]),
            active_rl_models=active_models,
            healthy_workers=len(worker_names) - len(unhealthy_workers),
            total_workers=len(worker_names),
            unhealthy_workers=unhealthy_workers,
            pending_notifications=pending,
            failed_notifications=failed,
            report_date=selected_date,
            timezone_name=timezone_name,
            periods=periods,
        )

    def _periods(
        self,
        positions: list[Position],
        orders: list[Order],
        *,
        day_start_local: datetime,
        report_timezone: ZoneInfo,
    ) -> tuple[DailyPeriodReport, ...]:
        windows = ((0, 7, "00:00–07:00"), (7, 14, "07:00–14:00"), (14, 24, "14:00–24:00"))
        result: list[DailyPeriodReport] = []
        for start_hour, end_hour, label in windows:
            start_local = day_start_local + timedelta(hours=start_hour)
            end_local = day_start_local + timedelta(hours=end_hour)
            start_utc = start_local.astimezone(timezone.utc)
            end_utc = end_local.astimezone(timezone.utc)
            opened = [item for item in positions if self._inside(item.entered_at, start_utc, end_utc)]
            closed = [
                item
                for item in positions
                if item.status == "CLOSED" and self._inside(item.closed_at, start_utc, end_utc)
            ]
            period_orders = [item for item in orders if self._inside(item.created_at, start_utc, end_utc)]
            wins = sum(float(item.pnl or 0.0) > 0 for item in closed)
            losses = sum(float(item.pnl or 0.0) < 0 for item in closed)
            gross_profit = sum(max(float(item.pnl or 0.0), 0.0) for item in closed)
            gross_loss = sum(abs(min(float(item.pnl or 0.0), 0.0)) for item in closed)
            reasons = Counter(str(item.exit_reason or "UNKNOWN") for item in closed)
            symbols = tuple(sorted({item.symbol for item in [*opened, *closed]}))
            result.append(
                DailyPeriodReport(
                    label=label,
                    start_at=start_local.astimezone(report_timezone),
                    end_at=end_local.astimezone(report_timezone),
                    opened=len(opened),
                    closed=len(closed),
                    wins=wins,
                    losses=losses,
                    win_rate=round(wins / len(closed) * 100, 2) if closed else 0.0,
                    gross_profit=round(gross_profit, 4),
                    gross_loss=round(gross_loss, 4),
                    net_pnl=round(sum(float(item.pnl or 0.0) for item in closed), 4),
                    fees=round(sum(max(float(item.fee or 0.0), 0.0) for item in period_orders), 8),
                    slippage=round(sum(max(float(item.slippage or 0.0), 0.0) for item in period_orders), 8),
                    long_entries=sum(str(item.side).upper() == "LONG" for item in opened),
                    short_entries=sum(str(item.side).upper() == "SHORT" for item in opened),
                    symbols=symbols,
                    exit_reasons=tuple(sorted(reasons.items())),
                )
            )
        return tuple(result)

    def _inside(self, value: datetime | None, start: datetime, end: datetime) -> bool:
        return value is not None and start <= _aware(value) < end


def daily_report_due(
    *,
    now: datetime,
    last_report_date: date | None,
    hour_utc: int,
    minute_utc: int,
) -> bool:
    current = _aware(now)
    hour = min(max(int(hour_utc), 0), 23)
    minute = min(max(int(minute_utc), 0), 59)
    scheduled = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return last_report_date != current.date() and current >= scheduled


def daily_report_due_local(
    *,
    now: datetime,
    last_report_date: date | None,
    timezone_name: str,
    hour_local: int = 0,
    minute_local: int = 5,
) -> tuple[bool, date]:
    local_now = _aware(now).astimezone(_timezone(timezone_name))
    report_date = local_now.date() - timedelta(days=1)
    scheduled = local_now.replace(
        hour=min(max(int(hour_local), 0), 23),
        minute=min(max(int(minute_local), 0), 59),
        second=0,
        microsecond=0,
    )
    return last_report_date != report_date and local_now >= scheduled, report_date


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")
