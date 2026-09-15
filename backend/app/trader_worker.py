import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

import ccxt
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import decrypt_secret
from app.db.session import AsyncSessionLocal, engine as database_engine
from app.models.entities import LogEntry, UserSettings
from app.safety_manager import SafetyCredentials, SafetyManager, ShutdownController, configure_stdout_logging
from app.services.control import TradingControlService
from app.services.exchange import ExchangeClient, exchange_error_message
from app.services.heartbeat import HeartbeatReporter
from app.services.locks import RedisLockManager, TRADING_CYCLE_LOCK
from app.services.reconciliation import OrderReconciliationService
from app.services.risk_manager import RiskSettings
from app.services.schema_readiness import wait_for_required_tables
from app.services.telegram_bot import TelegramNotifier
from app.services.telegram_cards import safe_render_cycle_card
from app.services.telegram_reports import format_cycle_report, format_worker_error, format_worker_started
from app.services.trading_engine import TradingEngine

logger = logging.getLogger(__name__)


async def main() -> None:
    configure_stdout_logging()
    shutdown = ShutdownController()
    shutdown.install()
    safety_credentials = await _load_safety_credentials()
    await SafetyManager().run_or_exit(safety_credentials)
    if shutdown.requested:
        return

    settings = get_settings()
    locks = RedisLockManager()
    control = TradingControlService()
    notifier = TelegramNotifier()
    heartbeat = HeartbeatReporter("trader-worker")
    last_cycle_report_at: datetime | None = None
    last_error_report_at: datetime | None = None
    logger.info("Trader worker started with loop=%ss", settings.trader_loop_seconds)
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
        await control.close()
        logger.info("Trader worker stopped while waiting for database migration")
        return
    if settings.telegram_trade_reports_enabled:
        await notifier.broadcast(
            format_worker_started(
                paper_trading=settings.paper_trading,
                loop_seconds=settings.trader_loop_seconds,
                exploration_enabled=settings.paper_exploration_enabled,
                exploration_max_positions=settings.paper_exploration_max_positions,
                exploration_risk_percent=settings.paper_exploration_risk_percent,
                report_interval_minutes=settings.telegram_cycle_report_interval_minutes,
            )
        )
    while not shutdown.requested:
        current_exchange = settings.default_exchange
        exchange: ExchangeClient | None = None
        delay = settings.trader_loop_seconds
        try:
            async with AsyncSessionLocal() as db:
                async with locks.lock(
                    TRADING_CYCLE_LOCK,
                    ttl_seconds=max(settings.trader_loop_seconds - 5, 10),
                ) as acquired:
                    if acquired:
                        user_settings = (await db.execute(select(UserSettings).order_by(UserSettings.id.asc()).limit(1))).scalar_one_or_none()
                        if not user_settings:
                            db.add(LogEntry(level="WARNING", message="Trader worker is waiting for the first user settings row"))
                            await db.commit()
                            await heartbeat.set_status("DEGRADED", {"reason": "waiting_for_settings"})
                        else:
                            current_exchange = user_settings.exchange
                            exchange = ExchangeClient.from_user_settings(user_settings)
                            trading_engine = TradingEngine(exchange, control=control)
                            reconciliation = OrderReconciliationService(exchange)
                            tick = await trading_engine.manage_open_positions(db)
                            await reconciliation.reconcile(db)
                            paused, reason = await control.is_paused()
                            if paused:
                                logger.warning("Trader worker entry scan paused: %s", reason)
                                await heartbeat.set_status("PAUSED", {"reason": reason or "unknown"})
                            elif not shutdown.requested:
                                risk_settings = RiskSettings(
                                    balance=1000,
                                    risk_percent=user_settings.risk_percent,
                                    daily_risk_percent=user_settings.daily_risk_percent,
                                    max_positions=user_settings.max_positions,
                                    min_rating=user_settings.min_rating,
                                    stop_loss_percent=user_settings.stop_loss_percent,
                                    take_profit_percent=user_settings.take_profit_percent,
                                    trailing_stop_percent=user_settings.trailing_stop_percent,
                                    atr_stop_multiplier=user_settings.atr_stop_multiplier,
                                    risk_reward_ratio=user_settings.risk_reward_ratio,
                                    breakeven_trigger_r=user_settings.breakeven_trigger_r,
                                    breakeven_offset_percent=user_settings.breakeven_offset_percent,
                                    partial_take_profit_r=user_settings.partial_take_profit_r,
                                    partial_close_percent=user_settings.partial_close_percent,
                                )
                                run = await trading_engine.run_once(
                                    db,
                                    risk_settings,
                                    timeframe=user_settings.scan_interval,
                                )
                                summary = _cycle_summary(run.scanned, run.opened, run.skipped, run.decisions, tick.closed)
                                cycle_metrics = _cycle_metrics(run.decisions)
                                logger.info(summary)
                                db.add(LogEntry(level="INFO", message=summary))
                                await db.commit()
                                await heartbeat.set_status(
                                    "OK",
                                    {
                                        "scanned": run.scanned,
                                        "opened": run.opened,
                                        "skipped": run.skipped,
                                        "closed": tick.closed,
                                        **cycle_metrics,
                                    },
                                )
                                report_due = _report_due(
                                    last_cycle_report_at,
                                    settings.telegram_cycle_report_interval_minutes,
                                )
                                if (
                                    settings.telegram_cycle_reports_enabled
                                    and (report_due or run.opened > 0 or tick.closed > 0)
                                ):
                                    await notifier.broadcast(
                                        format_cycle_report(
                                            run,
                                            tick,
                                            paper_trading=settings.paper_trading,
                                        ),
                                        photo=safe_render_cycle_card(
                                            run,
                                            tick,
                                            paper_trading=settings.paper_trading,
                                        ),
                                        photo_filename="trading-cycle.jpg",
                                        photo_caption="<b>Визуальная сводка торгового цикла</b>",
                                    )
                                    last_cycle_report_at = datetime.now(timezone.utc)
        except ccxt.BaseError as exc:
            message = exchange_error_message(
                exc,
                exchange=current_exchange,
                market_type=settings.exchange_default_type,
                sandbox=settings.exchange_sandbox_enabled,
            )
            logger.error("Trader worker exchange unavailable: %s", message)
            await _record_worker_log(message)
            await heartbeat.set_status("DEGRADED", {"error": type(exc).__name__})
            if _report_due(last_error_report_at, 15):
                await notifier.broadcast(format_worker_error("биржа недоступна", message))
                last_error_report_at = datetime.now(timezone.utc)
            delay = max(settings.trader_loop_seconds, 300)
        except Exception as exc:
            logger.exception("Trader worker loop failed")
            await heartbeat.set_status("ERROR", {"error": type(exc).__name__})
            if _report_due(last_error_report_at, 15):
                await notifier.broadcast(
                    format_worker_error("ошибка торгового цикла", f"{type(exc).__name__}: {str(exc)[:800]}")
                )
                last_error_report_at = datetime.now(timezone.utc)
        finally:
            if exchange is not None:
                await exchange.close()
        if await shutdown.wait(delay):
            break
    await heartbeat.stop()
    await locks.close()
    await control.close()
    logger.info("Trader worker shutdown complete")


async def _load_safety_credentials() -> SafetyCredentials | None:
    async with AsyncSessionLocal() as db:
        user_settings = (
            await db.execute(select(UserSettings).order_by(UserSettings.id.asc()).limit(1))
        ).scalar_one_or_none()
    if user_settings is None:
        return None
    return SafetyCredentials(
        exchange=user_settings.exchange,
        api_key=decrypt_secret(user_settings.api_key_encrypted),
        api_secret=decrypt_secret(user_settings.secret_key_encrypted),
        passphrase=decrypt_secret(user_settings.passphrase_encrypted),
    )


def _cycle_summary(scanned: int, opened: int, skipped: int, decisions: list, closed: int) -> str:
    metrics = _cycle_metrics(decisions)
    ranked = sorted(
        decisions,
        key=lambda decision: (decision.action == "OPENED", decision.signal in {"BUY", "SELL"}, decision.score),
        reverse=True,
    )
    samples = "; ".join(
        f"{decision.symbol}={decision.signal}/{decision.action}({decision.score}): {decision.reason}"
        for decision in ranked[:5]
    )
    message = (
        f"Auto-trade cycle scanned={scanned} opened={opened} skipped={skipped} "
        f"closed={closed} learning_updates={closed} directional={metrics['directional_candidates']} "
        f"strong_waits={metrics['strong_wait_candidates']} top_blocker={metrics['top_blocker']}"
    )
    return f"{message}; top_opportunities: {samples}"[:1000] if samples else message


def _cycle_metrics(decisions: list) -> dict[str, int | str]:
    directional = sum(decision.signal in {"BUY", "SELL"} for decision in decisions)
    min_score = int(get_settings().paper_exploration_min_score)
    strong_waits = sum(decision.signal == "WAIT" and decision.score >= min_score for decision in decisions)
    blockers = Counter(
        _cycle_blocker(decision.reason)
        for decision in decisions
        if decision.action == "SKIPPED"
    )
    top_blocker = blockers.most_common(1)[0][0] if blockers else "NONE"
    return {
        "directional_candidates": directional,
        "strong_wait_candidates": strong_waits,
        "top_blocker": top_blocker,
    }


def _cycle_blocker(reason: str) -> str:
    normalized = str(reason or "").lower()
    markers = (
        ("position already open", "POSITION_ALREADY_OPEN"),
        ("recovery position limit", "RECOVERY_POSITION_LIMIT"),
        ("maximum open positions", "MAX_POSITIONS"),
        ("per-cycle entry limit", "PAPER_LANE_CYCLE_LIMIT"),
        ("paper exploration position limit", "PAPER_LANE_POSITION_LIMIT"),
        ("strategy wait", "STRATEGY_WAIT"),
        ("performance guard", "PERFORMANCE_GUARD"),
        ("signal score below", "LOW_SCORE"),
        ("pre-trade quality", "PRETRADE_QUALITY"),
        ("market quality", "MARKET_QUALITY"),
        ("micro gate", "MICROSTRUCTURE"),
        ("committee rejected", "COMMITTEE"),
        ("rl disagrees", "RL_DISAGREEMENT"),
        ("learning", "LEARNING_MEMORY"),
        ("cooldown", "COOLDOWN"),
        ("same-side", "DIRECTIONAL_EXPOSURE"),
        ("exposure", "EXPOSURE"),
    )
    return next((code for marker, code in markers if marker in normalized), "OTHER")


def _report_due(last_sent_at: datetime | None, interval_minutes: int) -> bool:
    if last_sent_at is None:
        return True
    if last_sent_at.tzinfo is None:
        last_sent_at = last_sent_at.replace(tzinfo=timezone.utc)
    interval = timedelta(minutes=max(int(interval_minutes), 1))
    return datetime.now(timezone.utc) - last_sent_at >= interval


async def _record_worker_log(message: str) -> None:
    try:
        async with AsyncSessionLocal() as db:
            db.add(LogEntry(level="ERROR", message=f"Trader worker exchange unavailable: {message[:900]}"))
            await db.commit()
    except Exception:
        logger.exception("Failed to record trader worker exchange error")


if __name__ == "__main__":
    asyncio.run(main())
