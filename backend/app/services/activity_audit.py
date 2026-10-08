from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import EquitySnapshot, LogEntry, Order, Position, StrategyRelease, TradingPeriodSnapshot


class ActivityAuditService:
    """Calendar-day operational audit; missing historical equity stays missing."""

    timezone_name = "Europe/Simferopol"
    windows = ((0, 7, "00:00–07:00"), (7, 14, "07:00–14:00"), (14, 24, "14:00–24:00"))

    async def report(self, db: AsyncSession, day: date | None = None, *, now: datetime | None = None) -> dict:
        now = self.aware(now or datetime.now(timezone.utc))
        tz = ZoneInfo(self.timezone_name)
        day = day or (now.astimezone(tz).date() - timedelta(days=1))
        start = datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc)
        end = (datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)).astimezone(timezone.utc)
        positions = list((await db.execute(select(Position).where(
            Position.entered_at < end, or_(Position.closed_at.is_(None), Position.closed_at >= start),
        ))).scalars().all())
        orders = list((await db.execute(select(Order).where(Order.created_at >= start, Order.created_at < end))).scalars().all())
        # Only audit fields, never raw payloads that might contain credentials.
        logs = (await db.execute(select(
            LogEntry.created_at, LogEntry.level,
            LogEntry.context["event"].as_string().label("event"),
            LogEntry.context["gate"].as_string().label("gate"),
        ).where(LogEntry.created_at >= start, LogEntry.created_at < end))).all()
        settings = get_settings()
        mode = "PAPER" if settings.paper_trading or not settings.live_trading_enabled else "LIVE"
        snapshots = list((await db.execute(select(EquitySnapshot).where(
            EquitySnapshot.mode == mode, EquitySnapshot.captured_at >= start, EquitySnapshot.captured_at < end,
        ).order_by(EquitySnapshot.captured_at, EquitySnapshot.id))).scalars().all())
        first_snapshot = (await db.execute(select(func.min(EquitySnapshot.captured_at)).where(EquitySnapshot.mode == mode))).scalar_one()
        releases = list((await db.execute(select(StrategyRelease).where(
            StrategyRelease.deployed_at < end, StrategyRelease.deployed_at <= now,
        ).order_by(StrategyRelease.deployed_at.desc()).limit(10))).scalars().all())
        report = self.build(day, positions, orders, logs, snapshots, releases, first_snapshot, mode, now=now)
        saved = list((await db.execute(select(TradingPeriodSnapshot).where(
            TradingPeriodSnapshot.report_date == day, TradingPeriodSnapshot.mode == mode,
        ).order_by(TradingPeriodSnapshot.start_hour))).scalars().all())
        report["saved_periods"] = [{"start_hour": s.start_hour, "end_hour": s.end_hour,
            "captured_at": self.aware(s.captured_at).isoformat(), "source": s.source, **s.payload} for s in saved]
        return report

    def build(self, day, positions, orders, logs, snapshots, releases, first_snapshot, mode, *, now):
        tz = ZoneInfo(self.timezone_name)
        start = datetime.combine(day, time.min, tzinfo=tz)
        end = start + timedelta(days=1)
        snapshots = sorted((s for s in snapshots if self.aware(s.captured_at) <= now), key=lambda row: self.aware(row.captured_at))

        def inside(value, lo, hi):
            return value is not None and lo <= self.aware(value) < hi and self.aware(value) <= now

        def metrics(lo, hi):
            closed = [p for p in positions if p.status == "CLOSED" and inside(p.closed_at, lo, hi)]
            selected_orders = [o for o in orders if inside(o.created_at, lo, hi)]
            selected_logs = [row for row in logs if inside(row.created_at, lo, hi)]
            points = [s for s in snapshots if inside(s.captured_at, lo, hi)]
            events = Counter(row.event for row in selected_logs if row.event)
            gates = Counter(getattr(row, "gate", None) or "UNKNOWN" for row in selected_logs if row.event == "ENTRY_SKIPPED")
            failures = Counter((o.raw or {}).get("message", "Причина не записана") for o in selected_orders if o.status == "FAILED")
            return {
                "opened": sum(inside(p.entered_at, lo, hi) for p in positions),
                "closed": len(closed),
                "realized_pnl": round(sum(float(p.pnl or 0) for p in closed), 4),
                "orders": len(selected_orders),
                "failed_orders": sum(o.status == "FAILED" for o in selected_orders),
                "log_records": len(selected_logs),
                "errors": sum(row.level in {"ERROR", "CRITICAL"} for row in selected_logs),
                "skipped_entries": events.get("ENTRY_SKIPPED", 0),
                "failed_closes": events.get("POSITION_CLOSE_FAILED", 0),
                "snapshots": len(points),
                "equity_first": float(points[0].total_equity) if points else None,
                "equity_last": float(points[-1].total_equity) if points else None,
                "max_drawdown_percent": max(float(p.drawdown_percent) for p in points) if points else None,
                "events": [{"event": key, "count": value} for key, value in events.most_common(10)],
                "failure_reasons": [{"reason": key, "count": value} for key, value in failures.most_common(5)],
                "entry_blockers": [{"gate": key, "count": value} for key, value in gates.most_common(12)],
            }

        periods = []
        for lo, hi, label in self.windows:
            a, b = start + timedelta(hours=lo), start + timedelta(hours=hi)
            periods.append({"label": label, "start_hour": lo, "end_hour": hi, "status": "FUTURE" if now < a else "COMPLETE" if now >= b else "IN_PROGRESS", **metrics(a, b)})
        cutoff = min(end, now)
        open_at_cutoff = sum(
            self.aware(p.entered_at) < cutoff and (p.closed_at is None or self.aware(p.closed_at) >= cutoff)
            for p in positions
        )
        return {
            "date": day.isoformat(), "timezone": self.timezone_name, "mode": mode,
            "generated_at": now.isoformat(), "complete": now >= end,
            "summary": {**metrics(start, end), "open_at_end": open_at_cutoff},
            "periods": periods,
            "coverage": {
                "equity_collection_started_at": self.aware(first_snapshot).isoformat() if first_snapshot else None,
                "message": "Срезы капитала сохранены в течение выбранного дня; первая и последняя точки не обязательно совпадают с границами суток." if snapshots else "За выбранный день срезы капитала не записывались. PnL закрытий и активность рассчитаны по сохранённым сделкам, ордерам и журналу; исторический equity не восстановлен.",
                "position_mode_note": "Старые позиции и ордера не имеют отдельной метки Paper/Live; их дневная статистика общая. Срезы капитала отфильтрованы по указанному режиму.",
            },
            "equity_snapshots": [self.snapshot_row(s) for s in snapshots],
            "releases": [{"version": r.version, "deployed_at": self.aware(r.deployed_at).isoformat(), "market": (r.parameters or {}).get("exchange_default_type"), "config_hash": r.config_hash} for r in releases],
        }

    @staticmethod
    def snapshot_row(s):
        return {"captured_at": ActivityAuditService.aware(s.captured_at).isoformat(), "source": s.source,
                **{key: float(getattr(s, key)) for key in (
                    "total_equity", "free_equity", "reserved_equity", "realized_pnl", "unrealized_pnl",
                    "open_stop_risk", "gross_exposure", "net_exposure", "drawdown_percent",
                )}}

    @staticmethod
    def aware(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
