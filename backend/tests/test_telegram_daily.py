from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.entities import Position
from app.services.telegram_daily import TelegramDailyReportService
from app.services.telegram_daily import daily_report_due, daily_report_due_local


def test_daily_report_becomes_due_at_configured_utc_time():
    assert not daily_report_due(
        now=datetime(2026, 7, 21, 17, 59, tzinfo=timezone.utc),
        last_report_date=None,
        hour_utc=18,
        minute_utc=0,
    )
    assert daily_report_due(
        now=datetime(2026, 7, 21, 18, 0, tzinfo=timezone.utc),
        last_report_date=None,
        hour_utc=18,
        minute_utc=0,
    )


def test_daily_report_is_not_due_twice_on_same_date():
    assert not daily_report_due(
        now=datetime(2026, 7, 21, 23, 59, tzinfo=timezone.utc),
        last_report_date=date(2026, 7, 21),
        hour_utc=18,
        minute_utc=0,
    )
    assert daily_report_due(
        now=datetime(2026, 7, 22, 18, 0, tzinfo=timezone.utc),
        last_report_date=date(2026, 7, 21),
        hour_utc=18,
        minute_utc=0,
    )


def test_local_daily_report_targets_completed_simferopol_day():
    due, report_date = daily_report_due_local(
        now=datetime(2026, 7, 21, 21, 5, tzinfo=timezone.utc),
        last_report_date=None,
        timezone_name="Europe/Simferopol",
        hour_local=0,
        minute_local=5,
    )

    assert due is True
    assert report_date == date(2026, 7, 21)


class Result:
    def __init__(self, *, scalar=None, rows=None, row=None):
        self.scalar = scalar
        self.rows = rows or []
        self.row = row

    def scalar_one(self):
        return self.scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def one(self):
        return self.row


@pytest.mark.asyncio
async def test_daily_snapshot_uses_real_portfolio_learning_queue_and_workers():
    now = datetime(2026, 7, 21, 18, tzinfo=timezone.utc)
    opened = Position(
        symbol="BTC/USDT",
        side="LONG",
        entry_price=100,
        current_price=102,
        volume=1,
        stop=98,
        take=106,
        pnl=2,
        status="OPEN",
        entered_at=now - timedelta(hours=2),
    )
    closed = Position(
        symbol="ETH/USDT",
        side="SHORT",
        entry_price=100,
        current_price=95,
        volume=1,
        stop=103,
        take=94,
        pnl=5,
        status="CLOSED",
        entered_at=now - timedelta(days=1),
        closed_at=now - timedelta(hours=3),
    )
    heartbeats = [
        SimpleNamespace(worker_name="trader", status="OK", last_seen_at=now - timedelta(seconds=10)),
        SimpleNamespace(worker_name="optimizer", status="ERROR", last_seen_at=now - timedelta(seconds=10)),
    ]
    orders = [
        SimpleNamespace(created_at=now - timedelta(hours=2), fee=0.15, slippage=0.05),
        SimpleNamespace(created_at=now - timedelta(hours=3), fee=0.10, slippage=0.02),
    ]

    class Db:
        def __init__(self):
            self.results = [
                Result(rows=[opened, closed]),
                Result(rows=orders),
                Result(row=(3, 9)),
                Result(scalar=1),
                Result(scalar=2),
                Result(scalar=0),
                Result(rows=heartbeats),
            ]

        async def execute(self, _query):
            return self.results.pop(0)

    settings = SimpleNamespace(paper_trading=True, worker_heartbeat_stale_seconds=180)

    snapshot = await TelegramDailyReportService(settings=settings).snapshot(Db(), now=now)

    assert snapshot.pnl_day == 7
    assert snapshot.closed_today == 1
    assert snapshot.positions[0].symbol == "BTC/USDT"
    assert snapshot.learning_rules == 3
    assert snapshot.learning_observations == 9
    assert snapshot.active_rl_models == 1
    assert snapshot.healthy_workers == 1
    assert snapshot.unhealthy_workers == ("optimizer",)
    assert snapshot.pending_notifications == 2
    assert [period.label for period in snapshot.periods] == ["00:00–07:00", "07:00–14:00", "14:00–24:00"]
    assert snapshot.periods[2].opened == 1
    assert snapshot.periods[2].closed == 1
    assert snapshot.periods[2].net_pnl == 5
    assert snapshot.periods[2].fees == 0.25
