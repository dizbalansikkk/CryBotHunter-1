import asyncio
from datetime import datetime, timezone
import logging
from time import perf_counter

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal, engine as database_engine
from app.models.entities import LogEntry
from app.safety_manager import SafetyManager, ShutdownController, configure_stdout_logging
from app.services.heartbeat import HeartbeatReporter
from app.services.locks import RedisLockManager
from app.services.schema_readiness import wait_for_required_tables

logger = logging.getLogger(__name__)


async def main() -> None:
    configure_stdout_logging()
    shutdown = ShutdownController()
    shutdown.install()
    await SafetyManager().run_or_exit()
    if shutdown.requested:
        return

    # Keep PyTorch and Stable Baselines3 out of memory until pre-flight passes.
    try:
        from app.services.rl_training import RlTrainingInterrupted, RlTrainingService
    except ModuleNotFoundError as exc:
        if exc.name == "stable_baselines3":
            logger.critical(
                "RL-worker не запущен: в образе нет stable-baselines3. "
                "Проверьте, что Railway собрал текущую ветку main и завершил установку зависимостей."
            )
        raise

    settings = get_settings()
    trainer = RlTrainingService(stop_requested=lambda: shutdown.requested)
    locks = RedisLockManager()
    heartbeat = HeartbeatReporter("rl-worker")
    logger.info(
        "RL-worker запущен: пары=%s таймфреймы=%s цикл=%s сек. максимум обучений за цикл=%s",
        settings.rl_symbols,
        settings.candle_ingest_timeframes,
        settings.rl_prediction_loop_seconds,
        settings.rl_training_max_per_cycle,
    )
    await heartbeat.start()
    schema_ready = await wait_for_required_tables(
        database_engine,
        ("trade_post_mortems", "shadow_trades"),
        heartbeat=heartbeat,
        shutdown=shutdown,
    )
    if not schema_ready:
        await heartbeat.stop()
        await locks.close()
        logger.info("RL-worker остановлен во время ожидания миграции базы данных")
        return
    async with AsyncSessionLocal() as db:
        retired = await trainer.retire_excluded_symbols(db, settings.trading_excluded_symbols)
        if retired["models_retired"] or retired["shadow_trades_closed"]:
            db.add(
                LogEntry(
                    level="INFO",
                    message=(
                        f"Исключённые RL-пары очищены: моделей выведено={retired['models_retired']}; "
                        f"теневых сделок закрыто={retired['shadow_trades_closed']}; "
                        f"пары={settings.trading_excluded_symbols}"
                    ),
                )
            )
            logger.info("Очищено RL-состояние исключённых пар: %s", retired)
        await db.commit()
    while not shutdown.requested:
        cycle_started = perf_counter()
        processed = 0
        training_attempts = 0
        trained = 0
        promoted = 0
        rejected = 0
        shadowed = 0
        decisions = 0
        shadow_decisions = 0
        training_deferred = 0
        waiting = 0
        errors = 0
        total_pairs = len(settings.rl_symbols) * len(settings.candle_ingest_timeframes)
        training_budget = max(int(settings.rl_training_max_per_cycle), 0)
        try:
            if not settings.rl_trainer_enabled:
                logger.warning("RL-worker отключён: RL_TRAINER_ENABLED=false")
                await heartbeat.set_status("DISABLED", {"enabled": False})
            else:
                await heartbeat.set_status(
                    "RUNNING",
                    {
                        "stage": "cycle_start",
                        "progress": f"0/{total_pairs}",
                        "training_budget": training_budget,
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
                            for symbol in settings.rl_symbols:
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
                                        promotion_state = await trainer.evaluate_shadow_promotion(db, symbol, timeframe)
                                        if promotion_state == "PROMOTED":
                                            promoted += 1
                                            logger.info("RL-модель прошла виртуальную проверку и активирована: %s", key)
                                            db.add(LogEntry(level="INFO", message=f"RL-модель активирована после forward-теста: {key}"))
                                            await db.commit()
                                        elif promotion_state == "REJECTED":
                                            rejected += 1
                                            logger.info("RL-модель не прошла виртуальную проверку: %s", key)
                                            db.add(LogEntry(level="WARNING", message=f"RL-модель не прошла forward-тест: {key}"))
                                            await db.commit()
                                        needs_training = await trainer.needs_refresh(db, symbol, timeframe)
                                        if needs_training and training_attempts < training_budget:
                                            training_attempts += 1
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
                                                decisions += 1
                                            else:
                                                shadowed += 1
                                                shadow_decisions += 1
                                                waiting += 1
                                            logger.info("RL-модель обучена: %s статус=%s метрики=%s", key, model.status, model.metrics)
                                            db.add(LogEntry(level="INFO", message=f"RL-модель обучена: {key}; статус={model.status}"))
                                            await db.commit()
                                        else:
                                            if needs_training:
                                                training_deferred += 1
                                            await heartbeat.set_status(
                                                "RUNNING",
                                                {
                                                    "stage": "training_deferred" if needs_training else "publishing_decision",
                                                    "pair": key,
                                                    "progress": f"{processed}/{total_pairs}",
                                                    "training_budget": training_budget,
                                                    "training_deferred": training_deferred,
                                                },
                                            )
                                            decision, shadow_decision = await trainer.publish_decisions(db, symbol, timeframe)
                                            if decision:
                                                decisions += 1
                                                logger.info("RL-решение: %s действие=%s уверенность=%.2f", key, decision.action, decision.confidence)
                                            if shadow_decision:
                                                shadow_decisions += 1
                                                logger.info(
                                                    "Теневое RL-решение: %s действие=%s уверенность=%.2f права на торговлю=нет",
                                                    key,
                                                    shadow_decision.action,
                                                    shadow_decision.confidence,
                                                )
                                            if not decision:
                                                waiting += 1
                                    except RlTrainingInterrupted:
                                        logger.info("Обучение RL остановлено корректным завершением процесса")
                                        await db.rollback()
                                        break
                                    except Exception as exc:
                                        errors += 1
                                        logger.exception("Ошибка RL-worker для %s", key)
                                        await db.rollback()
                                        db.add(LogEntry(level="ERROR", message=f"Ошибка RL-worker для {key}: {exc.__class__.__name__}"))
                                        await db.commit()
                        else:
                            waiting = total_pairs
                            logger.info("Цикл RL пропущен: блокировка уже принадлежит другому экземпляру worker")
                duration_seconds = round(perf_counter() - cycle_started, 2)
                summary = {
                    "stage": "cycle_complete",
                    "processed": processed,
                    "total_pairs": total_pairs,
                    "trained": trained,
                    "training_attempts": training_attempts,
                    "promoted": promoted,
                    "rejected": rejected,
                    "shadowed": shadowed,
                    "decisions": decisions,
                    "shadow_decisions": shadow_decisions,
                    "training_deferred": training_deferred,
                    "training_budget": training_budget,
                    "waiting": waiting,
                    "errors": errors,
                    "duration_seconds": duration_seconds,
                    "next_cycle_seconds": max(settings.rl_prediction_loop_seconds, 60),
                }
                await heartbeat.set_status("DEGRADED" if errors else "IDLE", summary)
                logger.info(
                    "Цикл RL завершён: обработано=%s/%s обучено=%s активировано=%s в-тени=%s отклонено=%s решений=%s теневых_решений=%s отложено=%s ожидает=%s ошибок=%s длительность=%.2f сек.",
                    processed,
                    total_pairs,
                    trained,
                    promoted,
                    shadowed,
                    rejected,
                    decisions,
                    shadow_decisions,
                    training_deferred,
                    waiting,
                    errors,
                    duration_seconds,
                )
        except Exception as exc:
            logger.exception("Критическая ошибка цикла RL-worker")
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
    await locks.close()
    logger.info("RL-воркер корректно завершил работу")


if __name__ == "__main__":
    asyncio.run(main())
