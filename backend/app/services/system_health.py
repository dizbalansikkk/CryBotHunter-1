from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable

from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import TelegramOutboxMessage, WorkerHeartbeat
from app.services.exchange import ExchangeClient
from app.services.heartbeat import worker_is_healthy, worker_stale_seconds


@dataclass(frozen=True)
class ComponentHealth:
    name: str
    ok: bool
    latency_ms: int | None
    detail: str


@dataclass(frozen=True)
class WorkerHealth:
    name: str
    status: str
    age_seconds: int
    healthy: bool
    detail: dict = field(default_factory=dict)
    stale_after_seconds: int = 180


@dataclass(frozen=True)
class SystemHealthSnapshot:
    generated_at: datetime
    database: ComponentHealth
    redis: ComponentHealth
    exchange: ComponentHealth
    workers: tuple[WorkerHealth, ...]
    pending_notifications: int
    failed_notifications: int
    trading_paused: bool
    pause_reason: str | None
    paper_trading: bool

    @property
    def healthy(self) -> bool:
        dependencies_ok = self.database.ok and self.redis.ok and self.exchange.ok
        return dependencies_ok and bool(self.workers) and all(worker.healthy for worker in self.workers)


class SystemHealthService:
    def __init__(
        self,
        *,
        settings: Any | None = None,
        redis_factory: Callable[..., Any] | None = None,
        exchange_factory: Callable[[], ExchangeClient] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.redis_factory = redis_factory or Redis.from_url
        self.exchange_factory = exchange_factory or ExchangeClient

    async def snapshot(
        self,
        db: AsyncSession,
        *,
        now: datetime | None = None,
    ) -> SystemHealthSnapshot:
        generated_at = _aware(now or datetime.now(timezone.utc))
        database, pending, failed, workers = await self._database_health(db, generated_at)
        redis, paused, pause_reason = await self._redis_health()
        exchange = await self._exchange_health()
        return SystemHealthSnapshot(
            generated_at=generated_at,
            database=database,
            redis=redis,
            exchange=exchange,
            workers=workers,
            pending_notifications=pending,
            failed_notifications=failed,
            trading_paused=paused,
            pause_reason=pause_reason,
            paper_trading=bool(self.settings.paper_trading),
        )

    async def _database_health(
        self,
        db: AsyncSession,
        now: datetime,
    ) -> tuple[ComponentHealth, int, int, tuple[WorkerHealth, ...]]:
        started = perf_counter()
        try:
            await asyncio.wait_for(db.execute(text("select 1")), timeout=5)
            pending = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(TelegramOutboxMessage)
                        .where(TelegramOutboxMessage.status.in_(("PENDING", "RETRY", "SENDING")))
                    )
                ).scalar_one()
            )
            failed = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(TelegramOutboxMessage)
                        .where(TelegramOutboxMessage.status == "FAILED")
                    )
                ).scalar_one()
            )
            rows = (
                await db.execute(select(WorkerHeartbeat).order_by(WorkerHeartbeat.worker_name.asc()))
            ).scalars().all()
            stale_seconds = max(int(self.settings.worker_heartbeat_stale_seconds), 60)
            startup_grace_seconds = int(
                getattr(self.settings, "worker_heartbeat_startup_grace_seconds", 600)
            )
            long_task_grace_seconds = int(
                getattr(self.settings, "worker_heartbeat_long_task_grace_seconds", 900)
            )
            workers: tuple[WorkerHealth, ...] = tuple(
                self._worker_health(
                    item,
                    now=now,
                    stale_seconds=stale_seconds,
                    startup_grace_seconds=startup_grace_seconds,
                    long_task_grace_seconds=long_task_grace_seconds,
                )
                for item in rows
            )
            elapsed = _elapsed_ms(started)
            return ComponentHealth("PostgreSQL", True, elapsed, "запросы и мониторинг доступны"), pending, failed, workers
        except Exception as exc:
            return (
                ComponentHealth("PostgreSQL", False, _elapsed_ms(started), type(exc).__name__),
                0,
                0,
                (),
            )

    def _worker_health(
        self,
        item: WorkerHeartbeat,
        *,
        now: datetime,
        stale_seconds: int,
        startup_grace_seconds: int,
        long_task_grace_seconds: int,
    ) -> WorkerHealth:
        detail = getattr(item, "detail", None) or {}
        age_seconds = max(int((now - _aware(item.last_seen_at)).total_seconds()), 0)
        stale_after_seconds = worker_stale_seconds(
            base_seconds=stale_seconds,
            status=item.status,
            detail=detail,
            startup_grace_seconds=startup_grace_seconds,
            long_task_grace_seconds=long_task_grace_seconds,
        )
        return WorkerHealth(
            name=item.worker_name,
            status=item.status,
            age_seconds=age_seconds,
            healthy=worker_is_healthy(
                status=item.status,
                age_seconds=age_seconds,
                base_seconds=stale_seconds,
                detail=detail,
                startup_grace_seconds=startup_grace_seconds,
                long_task_grace_seconds=long_task_grace_seconds,
            ),
            detail=detail,
            stale_after_seconds=stale_after_seconds,
        )

    async def _redis_health(self) -> tuple[ComponentHealth, bool, str | None]:
        started = perf_counter()
        client = None
        try:
            client = self.redis_factory(self.settings.redis_url, decode_responses=True)
            await asyncio.wait_for(client.ping(), timeout=5)
            reason = await asyncio.wait_for(client.get(self.settings.trading_panic_key), timeout=5)
            return (
                ComponentHealth("Redis", True, _elapsed_ms(started), "координация доступна"),
                bool(reason),
                str(reason) if reason else None,
            )
        except Exception as exc:
            return ComponentHealth("Redis", False, _elapsed_ms(started), type(exc).__name__), True, "redis_unavailable"
        finally:
            if client is not None:
                close = getattr(client, "aclose", None)
                if callable(close):
                    try:
                        await close()
                    except Exception:
                        pass

    async def _exchange_health(self) -> ComponentHealth:
        started = perf_counter()
        exchange = self.exchange_factory()
        symbol = (self.settings.candle_ingest_symbols or ["BTC/USDT"])[0]
        try:
            candles = await asyncio.wait_for(exchange.fetch_ohlcv(symbol, "1m", 2), timeout=12)
            if not candles:
                raise RuntimeError("EmptyMarketData")
            return ComponentHealth(
                "Binance",
                True,
                _elapsed_ms(started),
                f"публичные свечи {symbol} доступны",
            )
        except Exception as exc:
            return ComponentHealth("Binance", False, _elapsed_ms(started), type(exc).__name__)
        finally:
            try:
                await exchange.close()
            except Exception:
                pass


def _elapsed_ms(started: float) -> int:
    return max(int((perf_counter() - started) * 1000), 0)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
