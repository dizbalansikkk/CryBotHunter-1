import asyncio
import logging

import ccxt

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.entities import LogEntry
from app.services.history import HistoricalDataService
from app.services.heartbeat import HeartbeatReporter
from app.services.locks import RedisLockManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    history = HistoricalDataService()
    locks = RedisLockManager()
    heartbeat = HeartbeatReporter("candle-worker")
    delay = settings.candle_ingest_loop_seconds
    logger.info(
        "Candle worker started symbols=%s timeframes=%s loop=%ss",
        settings.candle_ingest_symbols,
        settings.candle_ingest_timeframes,
        settings.candle_ingest_loop_seconds,
    )
    await heartbeat.start()
    try:
        while True:
            try:
                async with AsyncSessionLocal() as db:
                    await heartbeat.set_status(
                        "RUNNING",
                        {
                            "stage": "candle_ingestion",
                            "pairs": len(settings.candle_ingest_symbols)
                            * len(settings.candle_ingest_timeframes),
                            "long_running": True,
                        },
                    )
                    async with locks.lock("candle-worker-loop", ttl_seconds=max(settings.candle_ingest_loop_seconds - 5, 30)) as acquired:
                        if acquired:
                            inserted = await history.ingest_many(
                                db,
                                symbols=settings.candle_ingest_symbols,
                                timeframes=settings.candle_ingest_timeframes,
                                limit=min(settings.candle_ingest_limit, 1000),
                            )
                            total = sum(inserted.values())
                            db.add(LogEntry(level="INFO", message=f"Candle worker inserted {total} candle(s): {inserted}"))
                            await db.commit()
                            await heartbeat.set_status("OK", {"inserted": total})
                        else:
                            await heartbeat.set_status(
                                "IDLE",
                                {"stage": "replica_wait", "reason": "lock_owned_by_another_replica"},
                            )
                delay = settings.candle_ingest_loop_seconds
            except (ccxt.RateLimitExceeded, ccxt.DDoSProtection):
                delay = _next_rate_limit_delay(delay, settings.candle_ingest_loop_seconds)
                logger.warning("Binance rate limit reached; candle ingestion will retry in %ss", delay)
                await heartbeat.set_status("DEGRADED", {"reason": "rate_limit", "retry_seconds": delay})
            except Exception as exc:
                delay = settings.candle_ingest_loop_seconds
                logger.exception("Candle worker loop failed")
                await heartbeat.set_status("ERROR", {"error": type(exc).__name__})
            await asyncio.sleep(delay)
    finally:
        await heartbeat.stop()
        await locks.close()
        await history.exchange.close()


def _next_rate_limit_delay(current_delay: int, base_delay: int) -> int:
    base = max(int(base_delay), 60)
    return min(max(int(current_delay), base) * 2, 1800)


if __name__ == "__main__":
    asyncio.run(main())
