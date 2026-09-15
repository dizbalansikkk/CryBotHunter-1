from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import LearningRule, Position, RlModel, TelegramOutboxMessage, WorkerHeartbeat
from app.services.heartbeat import expected_worker_names, worker_is_healthy
from app.services.pnl import PnlMetricsService


@dataclass(frozen=True)
class DailyPosition:
    symbol: str
    side: str
    pnl: float
    current_price: float


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


class TelegramDailyReportService:
    def __init__(self, *, settings: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self.pnl = PnlMetricsService()

    async def snapshot(
        self,
        db: AsyncSession,
        *,
        now: datetime | None = None,
    ) -> DailyReportSnapshot:
        generated_at = _aware(now or datetime.now(timezone.utc))
        day_start = generated_at.replace(hour=0, minute=0, second=0, microsecond=0)
        all_positions = list((await db.execute(select(Position))).scalars().all())
        pnl = self.pnl.summarize_positions(all_positions, now=generated_at)
        open_positions = [item for item in all_positions if item.status == "OPEN"]
        closed_today = sum(
            1
            for item in all_positions
            if item.status == "CLOSED" and item.closed_at and _aware(item.closed_at) >= day_start
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
            pnl_day=float(pnl.pnl_day),
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
        )


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


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
