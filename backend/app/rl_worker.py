import asyncio
from datetime import datetime, timezone
import logging
from time import perf_counter

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.entities import LogEntry
from app.safety_manager import SafetyManager, ShutdownController, configure_stdout_logging
from app.services.heartbeat import HeartbeatReporter
from app.services.locks import RedisLockManager

logger = logging.getLogger(__name__)


async def main() -> None:
    configure_stdout_logging()
    shutdown = ShutdownController()
    shutdown.install()
    await SafetyManager().run_or_exit()
    if shutdown.requested:
        return

    # Keep PyTorch and Stable Baselines3 out of memory until pre-flight passes.
    from app.services.rl_training import RlTrainingInterrupted, RlTrainingService

    settings = get_settings()
    trainer = RlTrainingService(stop_requested=lambda: shutdown.requested)
    locks = RedisLockManager()
    heartbeat = HeartbeatReporter("rl-worker")
    logger.info(
        "RL worker started symbols=%s timeframes=%s loop=%ss",
        settings.candle_ingest_symbols,
        settings.candle_ingest_timeframes,
        settings.rl_prediction_loop_seconds,
    )
    await heartbeat.start()
    while not shutdown.requested:
        cycle_started = perf_counter()
        processed = 0
        trained = 0
        promoted = 0
        rejected = 0
        decisions = 0
        waiting = 0
        errors = 0
        total_pairs = len(settings.candle_ingest_symbols) * len(settings.candle_ingest_timeframes)
        try:
            if not settings.rl_trainer_enabled:
                logger.warning("RL worker disabled by RL_TRAINER_ENABLED=false")
                await heartbeat.set_status("DISABLED", {"enabled": False})
            else:
                await heartbeat.set_status(
                    "RUNNING",
                    {
                        "stage": "cycle_start",
                        "progress": f"0/{total_pairs}",
                        "cycle_started_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                async with AsyncSessionLocal() as db:
                    lock_ttl = max(
                        settings.rl_prediction_loop_seconds * 4,
                        settings.worker_heartbeat_long_task_grace_seconds,
                        600,
                    )
                    async with locks.lock("rl-worker-loop", ttl_seconds=lock_ttl) as acquired:
                        if acquired:
                            for symbol in settings.candle_ingest_symbols:
                                if shutdown.requested:
                                    break
                                for timeframe in settings.candle_ingest_timeframes:
                                    if shutdown.requested:
                                        break
                                    key = f"{symbol}:{timeframe}"
                                    processed += 1
                                    try:
                                        await heartbeat.set_status(
                                            "RUNNING",
                                            {
                                                "stage": "checking_model",
                                                "pair": key,
                                                "progress": f"{processed}/{total_pairs}",
                                            },
                                        )
                                        if await trainer.needs_refresh(db, symbol, timeframe):
                                            await heartbeat.set_status(
                                                "TRAINING",
                                                {
                                                    "stage": "ppo_training",
                                                    "pair": key,
                                                    "progress": f"{processed}/{total_pairs}",
                                                    "timesteps_per_seed": settings.rl_training_timesteps,
                                                    "seeds": len(settings.rl_training_seeds),
                                                    "long_running": True,
                                                    "stale_after_seconds": settings.worker_heartbeat_long_task_grace_seconds,
                                                },
                                            )
                                            model = await trainer.train_symbol(db, symbol, timeframe)
                                            trained += 1
                                            if model.is_active:
                                                promoted += 1
                                            else:
                                                rejected += 1
                                            logger.info("RL trained %s status=%s metrics=%s", key, model.status, model.metrics)
                                            db.add(LogEntry(level="INFO", message=f"RL trained {key}: status={model.status}"))
                                            await db.commit()
                                        else:
                                            await heartbeat.set_status(
                                                "RUNNING",
                                                {
                                                    "stage": "publishing_decision",
                                                    "pair": key,
                                                    "progress": f"{processed}/{total_pairs}",
                                                },
                                            )
                                            decision = await trainer.publish_active_decision(db, symbol, timeframe)
                                            if decision:
                                                decisions += 1
                                                logger.info("RL decision %s action=%s confidence=%.2f", key, decision.action, decision.confidence)
                                            else:
                                                waiting += 1
                                    except RlTrainingInterrupted:
                                        logger.info("RL training stopped by graceful shutdown")
                                        await db.rollback()
                                        break
                                    except Exception as exc:
                                        errors += 1
                                        logger.exception("RL worker failed for %s", key)
                                        await db.rollback()
                                        db.add(LogEntry(level="ERROR", message=f"RL worker failed for {key}: {exc.__class__.__name__}"))
                                        await db.commit()
                        else:
                            waiting = total_pairs
                            logger.info("RL cycle skipped because another replica owns the loop lock")
                duration_seconds = round(perf_counter() - cycle_started, 2)
                summary = {
                    "stage": "cycle_complete",
                    "processed": processed,
                    "total_pairs": total_pairs,
                    "trained": trained,
                    "promoted": promoted,
                    "rejected": rejected,
                    "decisions": decisions,
                    "waiting": waiting,
                    "errors": errors,
                    "duration_seconds": duration_seconds,
                    "next_cycle_seconds": max(settings.rl_prediction_loop_seconds, 60),
                }
                await heartbeat.set_status("DEGRADED" if errors else "IDLE", summary)
                logger.info(
                    "RL cycle completed processed=%s/%s trained=%s promoted=%s rejected=%s decisions=%s waiting=%s errors=%s duration=%.2fs",
                    processed,
                    total_pairs,
                    trained,
                    promoted,
                    rejected,
                    decisions,
                    waiting,
                    errors,
                    duration_seconds,
                )
        except Exception as exc:
            logger.exception("RL worker loop failed")
            await heartbeat.set_status(
                "ERROR",
                {
                    "stage": "cycle_failed",
                    "error": type(exc).__name__,
                    "processed": processed,
                    "total_pairs": total_pairs,
                },
            )
        finally:
            await trainer.close()
        if await shutdown.wait(max(settings.rl_prediction_loop_seconds, 60)):
            break
    await heartbeat.stop()
    logger.info("RL worker shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
