from datetime import datetime, timedelta, timezone

from app.models.entities import Position, PositionStatus
from app.services.pnl import PnlMetricsService


def position(status: str, pnl: float, closed_at=None) -> Position:
    return Position(status=status, pnl=pnl, closed_at=closed_at)


def test_pnl_summary_uses_real_day_and_week_windows():
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    service = PnlMetricsService()
    positions = [
        position(PositionStatus.CLOSED.value, 10.0, closed_at=now - timedelta(hours=2)),
        position(PositionStatus.CLOSED.value, -8.0, closed_at=now - timedelta(days=2)),
        position(PositionStatus.CLOSED.value, 3.0, closed_at=now - timedelta(days=10)),
        position(PositionStatus.OPEN.value, -2.5),
    ]

    summary = service.summarize_positions(positions, now=now)

    assert summary.pnl_day == 7.5
    assert summary.pnl_week == -0.5
    assert summary.total_pnl == 2.5
    assert summary.open_pnl == -2.5
    assert summary.trades_count == 3
    assert summary.win_rate == 66.67


def test_pnl_summary_treats_naive_closed_at_as_utc():
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    service = PnlMetricsService()
    positions = [
        position(PositionStatus.CLOSED.value, -12.0, closed_at=datetime(2026, 7, 14, 1, 0)),
    ]

    summary = service.summarize_positions(positions, now=now)

    assert summary.pnl_day == -12.0
    assert summary.pnl_week == -12.0


def test_day_starts_at_simferopol_midnight():
    now = datetime(2026, 10, 7, 0, 30, tzinfo=timezone.utc)
    rows = [
        position("CLOSED", 2, datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc)),
        position("CLOSED", 100, datetime(2026, 10, 6, 20, 59, tzinfo=timezone.utc)),
    ]
    assert PnlMetricsService().summarize_positions(rows, now).pnl_day == 2
