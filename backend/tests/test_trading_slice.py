from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as Row
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SqlSession
from app.models.entities import LogEntry, Position
from app.services.trading_slice import completed_slice, metrics, build_slice, format_slice


@pytest.mark.parametrize("hour,end_hour,day", [(21, 0, 20), (5, 8, 19), (13, 16, 19)])
def test_three_local_slots_and_five_minute_delay(hour, end_hour, day):
    now = datetime(2026, 10, 19, hour, 5, tzinfo=timezone.utc)
    start, end = completed_slice(now, "Europe/Simferopol")
    assert end.hour == end_hour and end.day == day
    assert end - start == timedelta(hours=8)
    _, previous = completed_slice(now - timedelta(seconds=1), "Europe/Simferopol")
    assert previous == start


def test_statistics_include_zero_start_drawdown_and_net_risk_breaches():
    now = datetime.now(timezone.utc)
    positions = [Row(id=i, closed_at=now + timedelta(minutes=i), pnl=pnl,
                     entry_context={"planned_risk": 1}) for i, pnl in enumerate([-2, 3, -4])]
    result = metrics(positions)
    assert result["net"] == -3
    assert result["average_win"] == 3
    assert result["average_loss"] == -3
    assert result["drawdown"] == 4
    assert result["risk_breaches"] == 2


@pytest.mark.asyncio
async def test_slice_sql_boundaries_lanes_and_reason_counts():
    engine = create_engine("sqlite:///:memory:")
    Position.__table__.create(engine)
    LogEntry.__table__.create(engine)
    class AsyncAdapter:
        def __init__(self, session):
            self.session = session
        async def execute(self, query):
            return self.session.execute(query)
    start = datetime(2026, 10, 19, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=8)
    with SqlSession(engine) as db:
        for at, pnl, context in ((start, 2, {"paper_exploration": False}),
                                  (start, -3, {"paper_exploration": True, "planned_risk": 1}),
                                  (end, 100, {"paper_exploration": False})):
            db.add(Position(symbol="ETH/USDT", side="LONG", entry_price=100, current_price=100,
                volume=1, stop=90, take=110, status="CLOSED", closed_at=at, pnl=pnl, entry_context=context))
        for lane in ("strategy", "paper_exploration", None):
            db.add(LogEntry(level="INFO", message="skip", created_at=start,
                context={"event": "ENTRY_SKIPPED", "lane": lane, "gate": "RISK_MANAGER"}))
        db.add(LogEntry(level="ERROR", message="exit", created_at=start,
            context={"event": "POSITION_CLOSE_FAILED", "lane": "paper_exploration", "error_type": "TimeoutError"}))
        db.commit()
        groups = await build_slice(AsyncAdapter(db), start, end)
        assert groups["strategy"]["net"] == 2
        assert groups["paper_exploration"]["net"] == -3
        assert groups["paper_exploration"]["risk_breaches"] == 1
        assert groups["unknown"]["skipped"]["RISK_MANAGER"] == 1
        assert groups["paper_exploration"]["failed"]["TimeoutError"] == 1
        text = format_slice(groups, start, end)
        assert "TimeoutError" in text and len(text) < 4096
    engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_emits_three_distinct_deduplicated_slices(monkeypatch):
    from app.services.telegram_bot import TelegramPollingBot
    from app.services import trading_slice
    groups = {name: {**metrics([]), "skipped": {}, "failed": {}} for name in ("strategy", "paper_exploration", "unknown")}
    monkeypatch.setattr(trading_slice, "build_slice", AsyncMock(return_value=groups))
    monkeypatch.setattr(trading_slice, "format_slice", lambda *args: "slice")
    class Session:
        async def __aenter__(self):
            return object()
        async def __aexit__(self, *args):
            pass
    bot = TelegramPollingBot()
    bot.settings = Row(telegram_daily_report_enabled=True, telegram_trading_slices_enabled=True,
                       telegram_daily_report_timezone="Europe/Simferopol")
    bot.notifier.broadcast = AsyncMock()
    for hour in (0, 8, 16):
        now = datetime(2026, 10, 19, hour, 5, tzinfo=timezone.utc)
        await bot._send_daily_report_if_due(Session, now=now)
        await bot._send_daily_report_if_due(Session, now=now)
    assert bot.notifier.broadcast.await_count == 3
    assert len({call.kwargs["dedupe_key"] for call in bot.notifier.broadcast.call_args_list}) == 3
