from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.system_health import SystemHealthService


class Result:
    def __init__(self, *, scalar=None, rows=None):
        self.scalar = scalar
        self.rows = rows or []

    def scalar_one(self):
        return self.scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows


@pytest.mark.asyncio
async def test_system_health_checks_all_dependencies_and_worker_freshness():
    now = datetime(2026, 7, 21, 18, tzinfo=timezone.utc)
    worker = SimpleNamespace(
        worker_name="trader",
        status="OK",
        last_seen_at=now - timedelta(seconds=10),
    )

    class Db:
        def __init__(self):
            self.results = [Result(), Result(scalar=2), Result(scalar=1), Result(rows=[worker])]

        async def execute(self, _query):
            return self.results.pop(0)

    class RedisClient:
        closed = False

        async def ping(self):
            return True

        async def get(self, _key):
            return None

        async def aclose(self):
            self.closed = True

    redis_client = RedisClient()

    class Exchange:
        closed = False

        async def fetch_ohlcv(self, symbol, timeframe, limit):
            assert (symbol, timeframe, limit) == ("BTC/USDT", "1m", 2)
            return [[1, 1, 1, 1, 1, 1]]

        async def close(self):
            self.closed = True

    exchange = Exchange()
    settings = SimpleNamespace(
        redis_url="redis://unused",
        trading_panic_key="trading:panic",
        candle_ingest_symbols=["BTC/USDT"],
        worker_heartbeat_stale_seconds=180,
        paper_trading=True,
    )
    service = SystemHealthService(
        settings=settings,
        redis_factory=lambda *_args, **_kwargs: redis_client,
        exchange_factory=lambda: exchange,
    )

    snapshot = await service.snapshot(Db(), now=now)

    assert snapshot.healthy
    assert snapshot.pending_notifications == 2
    assert snapshot.failed_notifications == 1
    assert snapshot.workers[0].healthy
    assert not snapshot.trading_paused
    assert redis_client.closed
    assert exchange.closed
