import asyncio
from datetime import date, datetime, timedelta, timezone
from html import escape
import logging

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import (
    LearningRule,
    LogEntry,
    Position,
    RlModel,
    TelegramOutboxMessage,
    WorkerHeartbeat,
)
from app.services.control import TradingControlService
from app.services.exchange import ExchangeClient
from app.services.heartbeat import (
    HeartbeatReporter,
    WorkerHeartbeatService,
    expected_worker_names,
    worker_is_healthy,
)
from app.services.performance_guard import PerformanceGuardService
from app.services.pnl import PnlMetricsService
from app.services.reconciliation import OrderReconciliationService
from app.services.system_health import SystemHealthService
from app.services.telegram_cards import safe_render_daily_report_card
from app.services.telegram_daily import TelegramDailyReportService, daily_report_due
from app.services.telegram_outbox import TelegramOutboxService
from app.services.telegram_reports import (
    format_daily_report,
    format_position_details,
    format_system_health,
    format_trade_closed,
    format_worker_heartbeat_event,
    human_reason,
    split_telegram_message,
    worker_detail_summary,
    worker_status_label,
)

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.base_url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}"
        self.outbox = TelegramOutboxService()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.telegram_bot_token)

    async def send_message(self, chat_id: int, text: str, *, parse_mode: str | None = None) -> bool:
        if not self.enabled:
            return False
        chunks = split_telegram_message(text)
        if not chunks:
            return False
        results = [await self._send_message_chunk(chat_id, chunk, parse_mode=parse_mode) for chunk in chunks]
        return all(results)

    async def _send_message_chunk(self, chat_id: int, text: str, *, parse_mode: str | None = None) -> bool:
        try:
            payload: dict[str, str | int] = {"chat_id": chat_id, "text": text}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(f"{self.base_url}/sendMessage", json=payload)
                response.raise_for_status()
                return True
        except httpx.HTTPError as exc:
            logger.warning("Telegram sendMessage failed for chat_id=%s error=%s", chat_id, type(exc).__name__)
            return False

    async def send_photo(
        self,
        chat_id: int,
        photo: bytes,
        *,
        filename: str = "trade-card.jpg",
        caption: str | None = None,
    ) -> bool:
        if not self.enabled or not photo:
            return False
        try:
            data: dict[str, str] = {"chat_id": str(chat_id)}
            if caption:
                data.update({"caption": caption[:1024], "parse_mode": "HTML"})
            files = {"photo": (filename, photo, "image/jpeg")}
            async with httpx.AsyncClient(timeout=25) as client:
                response = await client.post(f"{self.base_url}/sendPhoto", data=data, files=files)
                response.raise_for_status()
                return True
        except httpx.HTTPError as exc:
            logger.warning("Telegram sendPhoto failed for chat_id=%s error=%s", chat_id, type(exc).__name__)
            return False

    async def broadcast(
        self,
        text: str,
        *,
        photo: bytes | None = None,
        photo_filename: str = "trade-card.jpg",
        photo_caption: str | None = None,
        parse_mode: str | None = "HTML",
        dedupe_key: str | None = None,
    ) -> int:
        if not self.enabled:
            return 0
        if getattr(self.settings, "telegram_outbox_enabled", True):
            try:
                message_ids = await self.outbox.enqueue_many(
                    self.settings.telegram_allowed_chat_ids,
                    text=text,
                    parse_mode=parse_mode,
                    photo=photo,
                    photo_filename=photo_filename,
                    photo_caption=photo_caption,
                    dedupe_key=dedupe_key,
                )
                if not message_ids:
                    return 0
                return await self.flush_outbox(message_ids=message_ids)
            except Exception as exc:
                logger.warning("Telegram outbox unavailable; using direct delivery error=%s", type(exc).__name__)
        return await self._broadcast_direct(
            text,
            photo=photo,
            photo_filename=photo_filename,
            photo_caption=photo_caption,
            parse_mode=parse_mode,
        )

    async def flush_outbox(self, *, message_ids: list[int] | None = None) -> int:
        if not self.enabled or not getattr(self.settings, "telegram_outbox_enabled", True):
            return 0
        delivered = 0
        messages = await self.outbox.claim(message_ids=message_ids)
        for item in messages:
            text_delivered = bool(item.text_sent)
            if not text_delivered:
                text_delivered = await self.send_message(item.chat_id, item.text, parse_mode=item.parse_mode)
                if text_delivered:
                    await self.outbox.mark_progress(item.id, text_sent=True)

            photo_delivered = bool(item.photo_sent) or not item.photo
            if item.photo and not photo_delivered:
                photo_delivered = await self.send_photo(
                    item.chat_id,
                    item.photo,
                    filename=item.photo_filename or "trade-card.jpg",
                    caption=item.photo_caption,
                )
                if photo_delivered:
                    await self.outbox.mark_progress(item.id, photo_sent=True)
            if text_delivered and photo_delivered:
                await self.outbox.mark_delivered(item.id)
                delivered += 1
            else:
                await self.outbox.mark_failed(item.id, "TelegramDeliveryError")
        return delivered

    async def _broadcast_direct(
        self,
        text: str,
        *,
        photo: bytes | None,
        photo_filename: str,
        photo_caption: str | None,
        parse_mode: str | None,
    ) -> int:
        delivered = 0
        for chat_id in self.settings.telegram_allowed_chat_ids:
            text_delivered = await self.send_message(chat_id, text, parse_mode=parse_mode)
            photo_delivered = True
            if photo:
                photo_delivered = await self.send_photo(
                    chat_id,
                    photo,
                    filename=photo_filename,
                    caption=photo_caption,
                )
            delivered += int(text_delivered and photo_delivered)
        return delivered


class TelegramCommandService:
    async def handle(self, command: str, db: AsyncSession) -> str:
        command = command.strip()
        if not command:
            return "Отправь команду /help, чтобы увидеть возможности бота."
        command = command.split()[0].lower()
        if command in {"/start", "/help"}:
            return (
                "CryBotHunter работает.\n\n"
                "Команды:\n"
                "/status — режим и состояние системы\n"
                "/health — PostgreSQL, Redis, Binance, очередь и воркеры\n"
                "/report — полный текущий отчёт\n"
                "/positions — открытые позиции\n"
                "/trades — последние закрытые сделки\n"
                "/why — последние решения и причины пропуска\n"
                "/learning — состояние обучения\n"
                "/balance — виртуальный/реальный баланс\n"
                "/stats — торговая статистика\n"
                "/guard — защитные ограничения\n"
                "/panic — остановить новые входы\n"
                "/resume — разрешить новые входы\n"
                "/reconcile — сверить локальные ордера\n"
                "/chatid — показать ID этого чата"
            )
        if command == "/stop":
            return "Остановить сервис можно в Railway. Для временной остановки новых входов используй /panic."
        if command == "/panic":
            control = TradingControlService()
            try:
                applied = await control.panic("telegram")
            finally:
                await control.close()
            if not applied:
                return "Redis unavailable. Panic state could not be persisted; trading remains fail-closed."
            return "PANIC enabled. New entries are paused. Open positions will still be managed."
        if command == "/resume":
            control = TradingControlService()
            try:
                applied = await control.resume()
            finally:
                await control.close()
            if not applied:
                return "Redis unavailable. Trading remains fail-closed."
            return "Trading resumed. New entries are allowed again."
        if command == "/status":
            settings = get_settings()
            control = TradingControlService()
            try:
                paused, reason = await control.is_paused()
            finally:
                await control.close()
            open_positions = (
                await db.execute(select(func.count()).select_from(Position).where(Position.status == "OPEN"))
            ).scalar_one()
            pending_notifications = (
                await db.execute(
                    select(func.count())
                    .select_from(TelegramOutboxMessage)
                    .where(TelegramOutboxMessage.status.in_(("PENDING", "RETRY", "SENDING")))
                )
            ).scalar_one()
            expected_workers = expected_worker_names(settings)
            heartbeat_statement = select(WorkerHeartbeat)
            if expected_workers:
                heartbeat_statement = heartbeat_statement.where(
                    WorkerHeartbeat.worker_name.in_(expected_workers)
                )
            heartbeats = (
                await db.execute(heartbeat_statement.order_by(WorkerHeartbeat.worker_name.asc()))
            ).scalars().all()
            mode = "PAPER — виртуальные деньги" if settings.paper_trading else "LIVE — реальные ордера"
            report = (
                "<b>🟢 CRYBOTHUNTER · ONLINE</b>\n"
                f"<code>{mode}</code>\n\n"
                "<b>Торговля</b>\n"
                f"├ Новые входы: <code>{'пауза' if paused else 'разрешены'}</code>\n"
                f"├ Причина паузы: {escape(str(reason or 'нет'), quote=False)}\n"
                f"├ Открытых позиций: <code>{int(open_positions)}</code>\n"
                f"├ Тестовые входы: <code>{'включены' if settings.paper_trading and settings.paper_exploration_enabled else 'выключены'}</code>\n"
                f"├ Лимит позиций: <code>{settings.paper_exploration_max_positions}</code>\n"
                f"├ Риск на позицию: <code>до {settings.paper_exploration_risk_percent:.3f}%</code>\n"
                f"├ Полная сводка: <code>{settings.telegram_cycle_report_interval_minutes} мин.</code>\n"
                f"└ Telegram outbox: <code>{int(pending_notifications)} в очереди</code>"
            )
            missing_workers = [
                worker_name
                for worker_name in expected_workers
                if worker_name not in {item.worker_name for item in heartbeats}
            ]
            if heartbeats or missing_workers:
                heartbeat_now = datetime.now(timezone.utc)
                stale_seconds = max(int(settings.worker_heartbeat_stale_seconds), 60)
                heartbeat_lines = [
                    _heartbeat_status_line(
                        item,
                        settings=settings,
                        now=heartbeat_now,
                        stale_seconds=stale_seconds,
                    )
                    for item in heartbeats
                ]
                heartbeat_lines.extend(
                    f"🔴 {escape(worker_name)} · <code>{escape(worker_status_label('MISSING'))}</code>\n"
                    "   первый heartbeat ещё не получен"
                    for worker_name in missing_workers
                )
                report += "\n\n<b>Worker heartbeat</b>\n" + "\n".join(heartbeat_lines)
            return report
        if command == "/health":
            snapshot = await SystemHealthService().snapshot(db)
            return format_system_health(snapshot)
        if command == "/balance":
            exchange = ExchangeClient()
            try:
                balance = await exchange.get_balance()
                return "Баланс:\n" + "\n".join([f"{asset}: {amount:.2f}" for asset, amount in balance.items()])
            finally:
                await exchange.close()
        if command == "/stats":
            pnl = await PnlMetricsService().summary(db)
            return (
                f"Статистика:\n"
                f"Закрытых сделок: {pnl.trades_count}\n"
                f"PnL за день: {pnl.pnl_day:+.2f} USDT\n"
                f"PnL за неделю: {pnl.pnl_week:+.2f} USDT\n"
                f"Общий PnL: {pnl.total_pnl:+.2f} USDT\n"
                f"Открытый PnL: {pnl.open_pnl:+.2f} USDT\n"
                f"Доля прибыльных: {pnl.win_rate:.2f}%"
            )
        if command == "/guard":
            report = await PerformanceGuardService().evaluate(db)
            recovery = (
                f"\nРежим восстановления: да, риск {report.risk_multiplier:.2f}x"
                if report.recovery_mode
                else ""
            )
            retry = (
                f"\nСледующая проба: {report.retry_at:%d.%m.%Y %H:%M} UTC"
                if report.retry_at is not None
                else ""
            )
            return (
                f"Защитный фильтр:\n"
                f"Новые входы разрешены: {'да' if report.allowed else 'нет'}\n"
                f"Причина: {human_reason(report.reason)}\n"
                f"Проверено сделок: {report.trades_checked}\n"
                f"Доля прибыльных: {report.win_rate:.2f}%\n"
                f"Серия убытков: {report.loss_streak}\n"
                f"Суммарный результат: {report.total_profit:+.2f} USDT"
                f"{recovery}{retry}"
            )
        if command == "/positions":
            positions = (
                await db.execute(select(Position).where(Position.status == "OPEN").order_by(Position.entered_at.desc()))
            ).scalars().all()
            if not positions:
                return "Открытых позиций сейчас нет."
            return "<b>📌 ОТКРЫТЫЕ ПОЗИЦИИ</b>\n\n" + "\n\n────────────\n\n".join(
                format_position_details(item) for item in positions
            )
        if command == "/report":
            pnl = await PnlMetricsService().summary(db)
            positions = (
                await db.execute(select(Position).where(Position.status == "OPEN").order_by(Position.entered_at.desc()))
            ).scalars().all()
            lines = [
                "<b>📊 ПОЛНЫЙ ТОРГОВЫЙ ОТЧЁТ</b>",
                "",
                "<b>Портфель</b>",
                f"├ Открытых позиций: <code>{len(positions)}</code>",
                f"├ Открытый PnL: <b>{pnl.open_pnl:+.2f} USDT</b>",
                f"├ PnL за день: <code>{pnl.pnl_day:+.2f} USDT</code>",
                f"├ Общий PnL: <code>{pnl.total_pnl:+.2f} USDT</code>",
                f"├ Закрытых сделок: <code>{pnl.trades_count}</code>",
                f"└ Доля прибыльных: <code>{pnl.win_rate:.2f}%</code>",
            ]
            if positions:
                lines.extend(["", "<b>Позиции — подробно</b>", ""])
                lines.append("\n\n────────────\n\n".join(format_position_details(item) for item in positions))
            return "\n".join(lines)
        if command == "/trades":
            positions = (
                await db.execute(
                    select(Position)
                    .where(Position.status == "CLOSED")
                    .order_by(Position.closed_at.desc())
                    .limit(5)
                )
            ).scalars().all()
            if not positions:
                return "Закрытых сделок пока нет."
            return "<b>🧾 ПОСЛЕДНИЕ ЗАКРЫТЫЕ СДЕЛКИ</b>\n\n" + "\n\n────────────\n\n".join(
                format_trade_closed(
                    item,
                    exit_price=item.current_price,
                    reason=item.exit_reason or "MANUAL",
                )
                for item in positions
            )
        if command == "/why":
            entries = (
                await db.execute(
                    select(LogEntry)
                    .where(LogEntry.message.like("Auto-trade cycle%"))
                    .order_by(LogEntry.created_at.desc())
                    .limit(3)
                )
            ).scalars().all()
            if not entries:
                return "Отчётов торгового цикла пока нет."
            return "Последние решения:\n" + "\n\n".join(human_reason(item.message) for item in entries)
        if command == "/learning":
            rules = int((await db.execute(select(func.count()).select_from(LearningRule))).scalar_one())
            observations = int(
                (await db.execute(select(func.coalesce(func.sum(LearningRule.observations), 0)))).scalar_one()
            )
            models = (
                await db.execute(select(RlModel).order_by(RlModel.created_at.desc()).limit(5))
            ).scalars().all()
            lines = [
                "🧠 СОСТОЯНИЕ ОБУЧЕНИЯ",
                f"Правил из закрытых сделок: {rules}",
                f"Наблюдений по сделкам: {observations}",
                "Последние RL-модели:",
            ]
            lines.extend(
                f"• {item.symbol} {item.timeframe}: {item.status}, "
                f"доходность {float((item.metrics or {}).get('return_percent', 0)):+.2f}%, "
                f"причина {(item.metrics or {}).get('promotion_reason', 'нет')}"
                for item in models
            )
            if not models:
                lines.append("• моделей пока нет")
            return "\n".join(lines)
        if command == "/reconcile":
            result = await OrderReconciliationService().reconcile(db)
            return (
                f"Сверка ордеров:\n"
                f"Проверено: {result['checked']}\n"
                f"Обновлено: {result['updated']}\n"
                f"Ошибок: {result['failed']}"
            )
        return "Неизвестная команда. Используй /help."


class TelegramPollingBot:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.notifier = TelegramNotifier()
        self.commands = TelegramCommandService()
        self.heartbeat = HeartbeatReporter("telegram")
        self.heartbeat_service = WorkerHeartbeatService()
        self.daily_reports = TelegramDailyReportService()
        self.offset = 0
        self.last_outbox_cleanup_at: datetime | None = None
        self.last_daily_report_date: date | None = None
        self.last_daily_report_attempt_at: datetime | None = None

    async def run(self, session_factory) -> None:
        if not self.notifier.enabled:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        logger.info("Telegram polling bot started")
        await self.heartbeat.start()
        try:
            while True:
                try:
                    await self._maintenance(session_factory)
                    updates = await self._get_updates()
                    for update in updates:
                        self.offset = max(self.offset, update["update_id"] + 1)
                        await self._handle_update(update, session_factory)
                    await self.heartbeat.set_status("OK", {"offset": self.offset})
                except httpx.HTTPStatusError as exc:
                    status_code = exc.response.status_code
                    if status_code == 409:
                        logger.warning("Telegram polling conflict: another poller is active; retrying")
                    else:
                        logger.error("Telegram polling HTTP error status=%s", status_code)
                    await self.heartbeat.set_status("DEGRADED", {"http_status": status_code})
                    await asyncio.sleep(5)
                except httpx.HTTPError as exc:
                    logger.warning("Telegram polling transport error type=%s", type(exc).__name__)
                    await self.heartbeat.set_status("DEGRADED", {"error": type(exc).__name__})
                    await asyncio.sleep(5)
                except Exception as exc:
                    logger.exception("Telegram polling error")
                    await self.heartbeat.set_status("ERROR", {"error": type(exc).__name__})
                    await asyncio.sleep(5)
        finally:
            await self.heartbeat.stop()

    async def _maintenance(self, session_factory) -> None:
        try:
            await self.notifier.flush_outbox()
            now = datetime.now(timezone.utc)
            if self.last_outbox_cleanup_at is None or now - self.last_outbox_cleanup_at >= timedelta(hours=6):
                await self.notifier.outbox.cleanup()
                self.last_outbox_cleanup_at = now
        except Exception as exc:
            logger.warning("Telegram outbox maintenance failed error=%s", type(exc).__name__)
        try:
            events = await self.heartbeat_service.watchdog_events()
        except Exception as exc:
            logger.warning("Worker heartbeat watchdog failed error=%s", type(exc).__name__)
            events = []
        for event in events:
            await self.notifier.broadcast(
                format_worker_heartbeat_event(
                    kind=event.kind,
                    worker_name=event.worker_name,
                    status=event.status,
                    age_seconds=event.age_seconds,
                    detail=event.detail,
                    stale_after_seconds=event.stale_after_seconds,
                ),
                dedupe_key=(
                    f"heartbeat:{event.kind}:{event.worker_name}:"
                    f"{event.last_seen_at.isoformat()}"
                ),
            )
        await self._send_daily_report_if_due(session_factory)

    async def _send_daily_report_if_due(
        self,
        session_factory,
        *,
        now: datetime | None = None,
    ) -> None:
        if not self.settings.telegram_daily_report_enabled or session_factory is None:
            return
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
        if not daily_report_due(
            now=current,
            last_report_date=self.last_daily_report_date,
            hour_utc=self.settings.telegram_daily_report_hour_utc,
            minute_utc=self.settings.telegram_daily_report_minute_utc,
        ):
            return
        if (
            self.last_daily_report_attempt_at is not None
            and current - self.last_daily_report_attempt_at < timedelta(minutes=15)
        ):
            return
        self.last_daily_report_attempt_at = current
        try:
            async with session_factory() as db:
                snapshot = await self.daily_reports.snapshot(db, now=current)
            report = format_daily_report(snapshot)
            await self.notifier.broadcast(
                report,
                photo=safe_render_daily_report_card(snapshot),
                photo_filename=f"daily-report-{current:%Y-%m-%d}.jpg",
                photo_caption=(
                    f"<b>📊 CRYBOTHUNTER · ИТОГИ {current:%d.%m.%Y}</b>\n"
                    f"PnL за день: <b>{snapshot.pnl_day:+.2f} USDT</b>"
                ),
                dedupe_key=f"daily-report:{current:%Y-%m-%d}",
            )
            self.last_daily_report_date = current.date()
            logger.info("Telegram daily report queued report_date=%s", current.date())
        except Exception as exc:
            logger.warning("Telegram daily report failed error=%s", type(exc).__name__)

    async def _get_updates(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=35) as client:
            response = await client.get(
                f"{self.notifier.base_url}/getUpdates",
                params={"timeout": 30, "offset": self.offset, "allowed_updates": '["message"]'},
            )
            response.raise_for_status()
            return response.json().get("result", [])

    async def _handle_update(self, update: dict, session_factory) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        if "id" not in chat:
            return
        chat_id = int(chat.get("id"))
        text = message.get("text", "").strip()

        if self.settings.telegram_allowed_chat_ids and chat_id not in self.settings.telegram_allowed_chat_ids:
            await self.notifier.send_message(chat_id, "This chat is not allowed.")
            return

        if text and text.split()[0].lower() == "/chatid":
            await self.notifier.send_message(chat_id, f"chat_id: {chat_id}")
            return

        async with session_factory() as db:
            reply = await self.commands.handle(text, db)
        parse_mode = "HTML" if "<b>" in reply or "<code>" in reply else None
        await self.notifier.send_message(chat_id, reply, parse_mode=parse_mode)


def _heartbeat_age(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    seconds = max(int((datetime.now(timezone.utc) - value).total_seconds()), 0)
    if seconds < 60:
        return f"{seconds} сек назад"
    return f"{seconds // 60} мин назад"


def _aware_heartbeat(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _heartbeat_status_line(item, *, settings, now: datetime, stale_seconds: int) -> str:
    detail = getattr(item, "detail", None) or {}
    age_seconds = max(int((now - _aware_heartbeat(item.last_seen_at)).total_seconds()), 0)
    healthy = worker_is_healthy(
        status=item.status,
        age_seconds=age_seconds,
        base_seconds=stale_seconds,
        detail=detail,
        startup_grace_seconds=getattr(settings, "worker_heartbeat_startup_grace_seconds", 600),
        long_task_grace_seconds=getattr(settings, "worker_heartbeat_long_task_grace_seconds", 900),
    )
    return (
        f"{'🟢' if healthy else '🔴'} {escape(item.worker_name)} · "
        f"<code>{escape(worker_status_label(item.status))}</code> · {_heartbeat_age(item.last_seen_at)}\n"
        f"   {escape(worker_detail_summary(detail), quote=False)}"
    )
