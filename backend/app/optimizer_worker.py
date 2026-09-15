import asyncio
import logging

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.entities import LogEntry
from app.services.heartbeat import HeartbeatReporter
from app.services.locks import RedisLockManager
from app.services.optimizer import StrategyOptimizerService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()
    optimizer = StrategyOptimizerService()
    locks = RedisLockManager()
    heartbeat = HeartbeatReporter("optimizer-worker")
    logger.info(
        "Optimizer worker started symbols=%s timeframes=%s loop=%ss",
        settings.candle_ingest_symbols,
        settings.candle_ingest_timeframes,
        settings.strategy_optimizer_loop_seconds,
    )
    await heartbeat.start()
    while True:
        try:
            if not settings.strategy_optimizer_worker_enabled:
                logger.warning("Optimizer worker disabled by STRATEGY_OPTIMIZER_WORKER_ENABLED=false")
                await heartbeat.set_status("DISABLED", {"enabled": False})
                await asyncio.sleep(settings.strategy_optimizer_loop_seconds)
                continue
            total_pairs = len(settings.candle_ingest_symbols) * len(settings.candle_ingest_timeframes)
            processed = 0
            await heartbeat.set_status(
                "RUNNING",
                {
                    "stage": "optimizer_cycle",
                    "progress": f"0/{total_pairs}",
                    "long_running": True,
                },
            )
            async with AsyncSessionLocal() as db:
                async with locks.lock(
                    "optimizer-worker-loop",
                    ttl_seconds=max(settings.strategy_optimizer_loop_seconds - 5, 60),
                ) as acquired:
                    if acquired:
                        refreshed: dict[str, int] = {}
                        skipped: list[str] = []
                        for symbol in settings.candle_ingest_symbols:
                            for timeframe in settings.candle_ingest_timeframes:
                                key = f"{symbol}:{timeframe}"
                                processed += 1
                                await heartbeat.set_status(
                                    "RUNNING",
                                    {
                                        "stage": "optimizing_strategy",
                                        "pair": key,
                                        "progress": f"{processed}/{total_pairs}",
                                        "long_running": True,
                                    },
                                )
                                if not await optimizer.needs_refresh(db, symbol, timeframe):
                                    skipped.append(key)
                                    continue
                                try:
                                    results = await optimizer.optimize(
                                        db,
                                        symbol=symbol,
                                        timeframe=timeframe,
                                        limit=min(settings.strategy_optimizer_limit, 3000),
                                        top_n=settings.strategy_optimizer_top_n,
                                    )
                                    refreshed[key] = len(results)
                                except Exception as exc:
                                    logger.exception("Optimizer failed for %s", key)
                                    db.add(LogEntry(level="ERROR", message=f"Optimizer failed for {key}: {exc.__class__.__name__}"))
                                    await db.commit()
                        db.add(LogEntry(level="INFO", message=f"Optimizer worker refreshed={refreshed} skipped={skipped}"))
                        await db.commit()
                        await heartbeat.set_status(
                            "OK",
                            {"refreshed": len(refreshed), "skipped": len(skipped)},
                        )
                    else:
                        await heartbeat.set_status(
                            "IDLE",
                            {"stage": "replica_wait", "reason": "lock_owned_by_another_replica"},
                        )
        except Exception as exc:
            logger.exception("Optimizer worker loop failed")
            await heartbeat.set_status("ERROR", {"error": type(exc).__name__})
        await asyncio.sleep(settings.strategy_optimizer_loop_seconds)


if __name__ == "__main__":
    asyncio.run(main())
