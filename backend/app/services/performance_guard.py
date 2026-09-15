from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import Position


@dataclass
class PerformanceGuardReport:
    allowed: bool
    reason: str
    trades_checked: int
    win_rate: float
    loss_streak: int
    total_profit: float
    recovery_mode: bool = False
    risk_multiplier: float = 1.0
    retry_at: datetime | None = None


class PerformanceGuardService:
    async def evaluate(
        self,
        db: AsyncSession,
        limit: int = 20,
        *,
        now: datetime | None = None,
    ) -> PerformanceGuardReport:
        settings = get_settings()
        fetch_limit = max(limit * 10, limit)
        raw_rows = (
            await db.execute(
                select(Position.pnl, Position.closed_at, Position.entry_context)
                .where(Position.status == "CLOSED")
                .order_by(Position.closed_at.desc(), Position.id.desc())
                .limit(fetch_limit)
            )
        ).all()
        rows = [
            (row[0], row[1])
            for row in raw_rows
            if not self._is_paper_exploration(row[2] if len(row) > 2 else None)
        ][:limit]
        if len(rows) < settings.guard_min_trades:
            return PerformanceGuardReport(True, "not enough closed trades for guard", len(rows), 0, 0, 0)

        profits = [float(profit) for profit, _closed_at in rows]
        wins = [profit for profit in profits if profit > 0]
        win_rate = len(wins) / len(profits) * 100
        total_profit = sum(profits)
        loss_streak = self.loss_streak(profits)

        block_reason: str | None = None
        if loss_streak >= settings.guard_max_loss_streak:
            block_reason = "loss streak limit reached"
        elif win_rate < settings.guard_min_win_rate:
            block_reason = "win rate below guard threshold"
        elif total_profit < settings.guard_min_total_profit:
            block_reason = "recent total profit below guard threshold"

        if block_reason is None:
            return PerformanceGuardReport(
                True,
                "performance guard passed",
                len(rows),
                round(win_rate, 2),
                loss_streak,
                round(total_profit, 2),
            )

        return self._recovery_decision(
            reason=block_reason,
            trades_checked=len(rows),
            win_rate=win_rate,
            loss_streak=loss_streak,
            total_profit=total_profit,
            last_closed_at=rows[0][1],
            now=now,
        )

    def _recovery_decision(
        self,
        *,
        reason: str,
        trades_checked: int,
        win_rate: float,
        loss_streak: int,
        total_profit: float,
        last_closed_at: datetime,
        now: datetime | None = None,
    ) -> PerformanceGuardReport:
        settings = get_settings()
        current_time = self._aware(now or datetime.now(timezone.utc))
        last_closed = self._aware(last_closed_at)
        cooldown = timedelta(hours=max(float(settings.guard_recovery_cooldown_hours), 0.0))
        retry_at = last_closed + cooldown
        common = {
            "trades_checked": trades_checked,
            "win_rate": round(win_rate, 2),
            "loss_streak": loss_streak,
            "total_profit": round(total_profit, 2),
        }

        if not settings.guard_recovery_enabled:
            return PerformanceGuardReport(False, reason, **common)
        if current_time < retry_at:
            return PerformanceGuardReport(
                False,
                f"{reason}; recovery cooldown until {retry_at.isoformat()}",
                retry_at=retry_at,
                **common,
            )

        risk_multiplier = min(max(float(settings.guard_recovery_risk_multiplier), 0.01), 0.5)
        return PerformanceGuardReport(
            True,
            f"recovery probe after {reason}; risk reduced to {risk_multiplier:.2f}x",
            recovery_mode=True,
            risk_multiplier=risk_multiplier,
            **common,
        )

    def loss_streak(self, profits: list[float]) -> int:
        streak = 0
        for profit in profits:
            if profit < 0:
                streak += 1
            else:
                break
        return streak

    def _aware(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _is_paper_exploration(self, entry_context: dict | None) -> bool:
        return bool(isinstance(entry_context, dict) and entry_context.get("paper_exploration"))
