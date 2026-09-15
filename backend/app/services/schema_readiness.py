from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine


logger = logging.getLogger(__name__)


class HeartbeatLike(Protocol):
    async def set_status(self, status: str, detail: dict | None = None) -> None: ...


class ShutdownLike(Protocol):
    @property
    def requested(self) -> bool: ...

    async def wait(self, timeout: float) -> bool: ...


async def missing_required_tables(engine: AsyncEngine, table_names: Sequence[str]) -> list[str]:
    required = tuple(dict.fromkeys(str(name) for name in table_names if name))
    async with engine.connect() as connection:
        return await connection.run_sync(
            lambda sync_connection: [
                table_name
                for table_name in required
                if not inspect(sync_connection).has_table(table_name)
            ]
        )


async def wait_for_required_tables(
    engine: AsyncEngine,
    table_names: Sequence[str],
    *,
    heartbeat: HeartbeatLike,
    shutdown: ShutdownLike,
    retry_seconds: float = 5.0,
) -> bool:
    """Keep a worker healthy while Alembic finishes a parallel Railway deploy."""
    required = tuple(dict.fromkeys(str(name) for name in table_names if name))
    announced_wait = False
    while not shutdown.requested:
        try:
            missing = await missing_required_tables(engine, required)
            error_name: str | None = None
        except Exception as exc:
            missing = list(required)
            error_name = type(exc).__name__
            logger.warning("Database schema readiness check failed error=%s", error_name)

        if not missing:
            if announced_wait:
                logger.info("Database schema is ready required_tables=%s", list(required))
            return True

        detail: dict[str, object] = {
            "stage": "waiting_for_database_migration",
            "missing_tables": missing,
            "retry_seconds": max(float(retry_seconds), 1.0),
        }
        if error_name:
            detail["database_error"] = error_name
        await heartbeat.set_status("RUNNING", detail)
        if not announced_wait:
            logger.warning("Waiting for database migration missing_tables=%s", missing)
            announced_wait = True
        if await shutdown.wait(max(float(retry_seconds), 1.0)):
            return False
    return False
