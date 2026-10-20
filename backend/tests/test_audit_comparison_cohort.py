from datetime import datetime, timedelta, timezone
from scripts.compare_audit_exits import cohort


def test_entry_cohort_keeps_open_losing_positions_and_excludes_old_closures():
    end = datetime(2026, 10, 20, tzinfo=timezone.utc)
    rows = [
        {"id": 1, "entered_at": (end - timedelta(days=21)).isoformat(), "status": "CLOSED"},
        {"id": 2, "entered_at": (end - timedelta(days=1)).isoformat(), "status": "OPEN", "pnl": -5},
        {"id": 3, "entered_at": (end - timedelta(days=22)).isoformat(), "status": "CLOSED",
         "closed_at": end.isoformat()},
        {"id": 4, "entered_at": end.isoformat(), "status": "OPEN"},
    ]
    start, split, selected = cohort(rows, end)
    assert [r["id"] for r in selected] == [1, 2]
    assert split - start == timedelta(days=14)
    assert end - split == timedelta(days=7)
