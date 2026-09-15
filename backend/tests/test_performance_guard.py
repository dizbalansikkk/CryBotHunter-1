from datetime import datetime, timedelta, timezone

import pytest

from app.services.performance_guard import PerformanceGuardService


def test_loss_streak_count_stops_at_first_win():
    assert PerformanceGuardService().loss_streak([-1, -2, 3, -4]) == 2
    assert PerformanceGuardService().loss_streak([5, -1, -2]) == 0


class Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class Db:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement):
        return Result(self.rows)


@pytest.mark.asyncio
async def test_guard_blocks_during_recovery_cooldown(monkeypatch):
    service = PerformanceGuardService()
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    settings = service_settings(monkeypatch)
    settings.guard_recovery_cooldown_hours = 6
    rows = [(-1.0, now - timedelta(hours=1)) for _ in range(5)]

    report = await service.evaluate(Db(rows), now=now)

    assert report.allowed is False
    assert report.recovery_mode is False
    assert report.retry_at == now + timedelta(hours=5)
    assert "loss streak limit reached" in report.reason


@pytest.mark.asyncio
async def test_guard_allows_one_reduced_risk_probe_after_cooldown(monkeypatch):
    service = PerformanceGuardService()
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    settings = service_settings(monkeypatch)
    settings.guard_recovery_cooldown_hours = 6
    settings.guard_recovery_risk_multiplier = 0.25
    rows = [(-1.0, now - timedelta(hours=7)) for _ in range(5)]

    report = await service.evaluate(Db(rows), now=now)

    assert report.allowed is True
    assert report.recovery_mode is True
    assert report.risk_multiplier == 0.25
    assert report.retry_at is None
    assert "recovery probe" in report.reason


@pytest.mark.asyncio
async def test_new_loss_restarts_guard_recovery_cooldown(monkeypatch):
    service = PerformanceGuardService()
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    service_settings(monkeypatch).guard_recovery_cooldown_hours = 6
    rows = [
        (-2.0, now - timedelta(minutes=10)),
        (-1.0, now - timedelta(hours=8)),
        (-1.0, now - timedelta(hours=9)),
        (-1.0, now - timedelta(hours=10)),
        (-1.0, now - timedelta(hours=11)),
    ]

    report = await service.evaluate(Db(rows), now=now)

    assert report.allowed is False
    assert report.retry_at == now + timedelta(hours=5, minutes=50)


@pytest.mark.asyncio
async def test_guard_ignores_paper_learning_outcomes(monkeypatch):
    service = PerformanceGuardService()
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    settings = service_settings(monkeypatch)
    settings.guard_min_trades = 2
    rows = [
        (-10.0, now - timedelta(hours=1), {"paper_exploration": True}),
        (2.0, now - timedelta(hours=2), {"paper_exploration": False}),
        (1.0, now - timedelta(hours=3), {}),
    ]

    report = await service.evaluate(Db(rows), now=now)

    assert report.allowed is True
    assert report.trades_checked == 2
    assert report.win_rate == 100
    assert report.total_profit == 3


def service_settings(monkeypatch):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "guard_min_trades", 5)
    monkeypatch.setattr(settings, "guard_max_loss_streak", 3)
    monkeypatch.setattr(settings, "guard_min_win_rate", 35.0)
    monkeypatch.setattr(settings, "guard_min_total_profit", -50.0)
    monkeypatch.setattr(settings, "guard_recovery_enabled", True)
    monkeypatch.setattr(settings, "guard_recovery_cooldown_hours", 6.0)
    monkeypatch.setattr(settings, "guard_recovery_risk_multiplier", 0.25)
    return settings
