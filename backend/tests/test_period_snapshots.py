from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.activity_audit import ActivityAuditService
from app.services.period_snapshots import PeriodSnapshotService


def test_three_daily_cutoffs_use_local_time_and_never_future_periods():
    service = PeriodSnapshotService()
    for hour, minute, expected in [(3, 59, 0), (4, 0, 1), (11, 0, 2), (20, 59, 2)]:
        now = datetime(2026, 10, 8, hour, minute, tzinfo=timezone.utc)
        due = service.due_periods(now, date(2026, 10, 8))
        assert len(due) == expected
    midnight = datetime(2026, 10, 8, 21, tzinfo=timezone.utc)
    due = service.due_periods(midnight, date(2026, 10, 8))
    assert [(a, b) for _, a, b, _ in due] == [(0, 7), (7, 14), (14, 24)]
    assert all(day == date(2026, 10, 8) for day, *_ in due)


@pytest.mark.asyncio
async def test_repeated_capture_does_not_duplicate_periods(monkeypatch):
    fake_report = AsyncMock(return_value={
        "timezone": "Europe/Simferopol", "coverage": {"equity_collection_started_at": None},
        "periods": [{"start_hour": start, "label": label, "equity_last": None}
                    for start, _, label in ActivityAuditService.windows],
    })
    monkeypatch.setattr(ActivityAuditService, "report", fake_report)

    class Db:
        def __init__(self):
            self.rows = {}

        async def execute(self, statement):
            if statement.is_select:
                if len(statement.selected_columns) == 1:
                    latest = max((day for day, _ in self.rows), default=None)
                    return SimpleNamespace(scalar_one_or_none=lambda: latest)
                return SimpleNamespace(all=lambda: list(self.rows))
            values = statement.compile().params
            key = (values["report_date"], values["start_hour"])
            self.rows[key] = values
            return SimpleNamespace(rowcount=1)

    db = Db()
    now = datetime(2026, 10, 8, 11, 1, tzinfo=timezone.utc)
    assert await PeriodSnapshotService().capture_due(db, now=now) == 5
    assert await PeriodSnapshotService().capture_due(db, now=now) == 0
    assert len(db.rows) == 5
    assert db.rows[(date(2026, 10, 7), 14)]["source"] == "JOURNAL_BACKFILL"
    assert db.rows[(date(2026, 10, 8), 7)]["source"] == "SCHEDULED"
    assert all(row["payload"]["period"]["equity_last"] is None for row in db.rows.values())
