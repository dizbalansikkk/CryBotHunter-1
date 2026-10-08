from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Position, PositionStatus


@dataclass
class PnlSummary:
    pnl_day: float
    pnl_week: float
    total_pnl: float
    open_pnl: float
    win_rate: float
    trades_count: int


class PnlMetricsService:
    async def summary(self, db: AsyncSession, now: datetime | None = None) -> PnlSummary:
        positions = (await db.execute(select(Position))).scalars().all()
        return self.summarize_positions(list(positions), now=now)

    def summarize_positions(self, positions: list[Position], now: datetime | None = None) -> PnlSummary:
        current_time = self._aware(now or datetime.now(timezone.utc))
        day_start = current_time.astimezone(ZoneInfo("Europe/Simferopol")).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).astimezone(timezone.utc)
        week_start = current_time - timedelta(days=7)

        closed_positions = [position for position in positions if position.status == PositionStatus.CLOSED.value]
        open_positions = [position for position in positions if position.status == PositionStatus.OPEN.value]

        open_pnl = self._sum_pnl(open_positions)
        total_realized = self._sum_pnl(closed_positions)
        day_realized = self._sum_pnl(
            [position for position in closed_positions if self._on_or_after(position.closed_at, day_start)]
        )
        week_realized = self._sum_pnl(
            [position for position in closed_positions if self._on_or_after(position.closed_at, week_start)]
        )

        trades_count = len(closed_positions)
        wins = sum(1 for position in closed_positions if float(position.pnl or 0) > 0)
        win_rate = wins / trades_count * 100 if trades_count else 0.0

        return PnlSummary(
            pnl_day=round(day_realized + open_pnl, 4),
            pnl_week=round(week_realized + open_pnl, 4),
            total_pnl=round(total_realized + open_pnl, 4),
            open_pnl=round(open_pnl, 4),
            win_rate=round(win_rate, 2),
            trades_count=trades_count,
        )

    def _sum_pnl(self, positions: list[Position]) -> float:
        return sum(float(position.pnl or 0.0) for position in positions)

    def _on_or_after(self, value: datetime | None, start: datetime) -> bool:
        if value is None:
            return False
        return self._aware(value) >= start

    def _aware(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
