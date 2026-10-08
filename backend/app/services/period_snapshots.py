from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.models.entities import TradingPeriodSnapshot
from app.services.activity_audit import ActivityAuditService


class PeriodSnapshotService:
    """Three durable cutoffs daily; retries/restarts cannot duplicate them."""

    @staticmethod
    def due_periods(now, since=None):
        tz = ZoneInfo(ActivityAuditService.timezone_name)
        today = now.astimezone(tz).date()
        # First launch covers yesterday. Later launches catch up from the last
        # recorded day, including gaps, bounded by journal retention (30 days).
        first = max(since or today - timedelta(days=1), today - timedelta(days=30))
        result = []
        day = first
        while day <= today:
            midnight = datetime.combine(day, time.min, tzinfo=tz)
            for start, end, _ in ActivityAuditService.windows:
                cutoff = midnight + timedelta(hours=end)
                if cutoff <= now:
                    result.append((day, start, end, cutoff))
            day += timedelta(days=1)
        return result

    async def capture_due(self, db, *, now=None):
        now = now or datetime.now(timezone.utc)
        settings = get_settings()
        mode = "PAPER" if settings.paper_trading or not settings.live_trading_enabled else "LIVE"
        latest = (await db.execute(select(TradingPeriodSnapshot.report_date).where(
            TradingPeriodSnapshot.mode == mode,
        ).order_by(TradingPeriodSnapshot.report_date.desc()).limit(1))).scalar_one_or_none()
        due = self.due_periods(now, latest)
        if not due:
            return 0
        existing = set((await db.execute(select(
            TradingPeriodSnapshot.report_date, TradingPeriodSnapshot.start_hour,
        ).where(TradingPeriodSnapshot.mode == mode, TradingPeriodSnapshot.report_date >= due[0][0]))).all())
        reports = {}
        written = 0
        for day, start, end, cutoff in due:
            if (day, start) in existing:
                continue
            if day not in reports:
                reports[day] = await ActivityAuditService().report(db, day, now=now)
            report = reports[day]
            period = next(p for p in report["periods"] if p["start_hour"] == start)
            result = await db.execute(insert(TradingPeriodSnapshot).values(
                report_date=day, start_hour=start, end_hour=end, mode=mode,
                source="SCHEDULED" if now - cutoff <= timedelta(minutes=5) else "JOURNAL_BACKFILL",
                payload={"timezone": report["timezone"], "period": period, "coverage": report["coverage"]},
                captured_at=now,
            ).on_conflict_do_nothing(constraint="uq_trading_period_mode"))
            written += max(result.rowcount or 0, 0)
        return written
