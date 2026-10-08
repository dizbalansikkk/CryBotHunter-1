from datetime import date, datetime, timezone
from types import SimpleNamespace as Row

from app.services.activity_audit import ActivityAuditService


def test_yesterday_reports_failures_without_inventing_equity_or_closed_trades():
    service = ActivityAuditService()
    now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    records = [Row(entered_at=datetime(2026, 10, 3, tzinfo=timezone.utc), closed_at=None, status="OPEN", pnl=-5)]
    at = datetime(2026, 10, 6, 4, tzinfo=timezone.utc)  # 07:00 local, second slice
    orders = [Row(created_at=at, status="FAILED", raw={"message": "below minimum"})]
    logs = [Row(created_at=at, level="ERROR", event="POSITION_CLOSE_FAILED")]
    report = service.build(date(2026, 10, 6), records, orders, logs, [], [], now, "PAPER", now=now)
    assert report["summary"]["closed"] == 0
    assert report["summary"]["realized_pnl"] == 0
    assert report["summary"]["open_at_end"] == 1
    assert report["summary"]["failed_closes"] == 1
    assert report["summary"]["equity_last"] is None
    assert report["periods"][0]["failed_orders"] == 0
    assert report["periods"][1]["failed_orders"] == 1
    assert report["equity_snapshots"] == []


def test_calendar_day_excludes_next_midnight_and_current_open_pnl():
    service = ActivityAuditService()
    now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    day_start = datetime(2026, 10, 5, 21, tzinfo=timezone.utc)
    next_day = datetime(2026, 10, 6, 21, tzinfo=timezone.utc)
    positions = [
        Row(entered_at=day_start, closed_at=next_day, status="CLOSED", pnl=100),
        Row(entered_at=day_start, closed_at=datetime(2026, 10, 6, 20, tzinfo=timezone.utc), status="CLOSED", pnl=2),
    ]
    report = service.build(date(2026, 10, 6), positions, [], [], [], [], None, "PAPER", now=now)
    assert report["summary"]["closed"] == 1
    assert report["summary"]["realized_pnl"] == 2
    assert report["summary"]["open_at_end"] == 1
    assert sum(p["realized_pnl"] for p in report["periods"]) == 2


def test_today_snapshot_and_future_periods_are_explicit():
    service = ActivityAuditService()
    now = datetime(2026, 10, 7, 9, tzinfo=timezone.utc)
    snapshot = Row(captured_at=now, source="PAPER_LEDGER", total_equity=638, free_equity=630,
                   reserved_equity=8, realized_pnl=0, unrealized_pnl=0, open_stop_risk=1,
                   gross_exposure=16, net_exposure=-16, drawdown_percent=0)
    report = service.build(date(2026, 10, 7), [], [], [], [snapshot], [], now, "PAPER", now=now)
    assert not report["complete"]
    assert report["periods"][1]["status"] == "IN_PROGRESS"
    assert report["periods"][2]["status"] == "FUTURE"
    assert report["summary"]["snapshots"] == 1
    assert report["equity_snapshots"][0]["net_exposure"] == -16
