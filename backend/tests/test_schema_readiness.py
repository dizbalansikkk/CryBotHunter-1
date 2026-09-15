from __future__ import annotations

import pytest

from app.services import schema_readiness


class StubHeartbeat:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict]] = []

    async def set_status(self, status: str, detail: dict | None = None) -> None:
        self.updates.append((status, detail or {}))


class StubShutdown:
    def __init__(self, *, stop_on_wait: bool = False) -> None:
        self.requested = False
        self.stop_on_wait = stop_on_wait
        self.waits: list[float] = []

    async def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        if self.stop_on_wait:
            self.requested = True
            return True
        return False


@pytest.mark.asyncio
async def test_wait_for_required_tables_recovers_after_migration(monkeypatch):
    checks = iter([["shadow_trades"], []])

    async def fake_missing(_engine, _tables):
        return next(checks)

    monkeypatch.setattr(schema_readiness, "missing_required_tables", fake_missing)
    heartbeat = StubHeartbeat()
    shutdown = StubShutdown()

    ready = await schema_readiness.wait_for_required_tables(
        object(),
        ("trade_post_mortems", "shadow_trades"),
        heartbeat=heartbeat,
        shutdown=shutdown,
        retry_seconds=0,
    )

    assert ready is True
    assert shutdown.waits == [1.0]
    assert heartbeat.updates == [
        (
            "RUNNING",
            {
                "stage": "waiting_for_database_migration",
                "missing_tables": ["shadow_trades"],
                "retry_seconds": 1.0,
            },
        )
    ]


@pytest.mark.asyncio
async def test_wait_for_required_tables_stops_gracefully(monkeypatch):
    async def fake_missing(_engine, _tables):
        return ["trade_post_mortems", "shadow_trades"]

    monkeypatch.setattr(schema_readiness, "missing_required_tables", fake_missing)
    heartbeat = StubHeartbeat()
    shutdown = StubShutdown(stop_on_wait=True)

    ready = await schema_readiness.wait_for_required_tables(
        object(),
        ("trade_post_mortems", "shadow_trades"),
        heartbeat=heartbeat,
        shutdown=shutdown,
    )

    assert ready is False
    assert heartbeat.updates[0][0] == "RUNNING"
    assert heartbeat.updates[0][1]["stage"] == "waiting_for_database_migration"
