from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, or_, select

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.models.entities import TelegramOutboxMessage
from app.services.log_retention import cleanup_technical_logs


class TelegramOutboxService:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def enqueue_many(
        self,
        chat_ids: list[int],
        *,
        text: str,
        parse_mode: str | None,
        photo: bytes | None,
        photo_filename: str | None,
        photo_caption: str | None,
        dedupe_key: str | None = None,
    ) -> list[int]:
        if not self.settings.telegram_outbox_enabled or not chat_ids:
            return []
        queued: list[TelegramOutboxMessage] = []
        async with AsyncSessionLocal() as db:
            for chat_id in chat_ids:
                chat_dedupe_key = dedupe_key[:255] if dedupe_key else None
                if chat_dedupe_key:
                    existing = (
                        await db.execute(
                            select(TelegramOutboxMessage.id).where(
                                TelegramOutboxMessage.chat_id == chat_id,
                                TelegramOutboxMessage.dedupe_key == chat_dedupe_key,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        continue
                item = TelegramOutboxMessage(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    photo=photo,
                    photo_filename=photo_filename,
                    photo_caption=photo_caption,
                    dedupe_key=chat_dedupe_key,
                    status="PENDING",
                    attempts=0,
                )
                db.add(item)
                queued.append(item)
            if not queued:
                return []
            await db.commit()
            return [item.id for item in queued]

    async def claim(
        self,
        *,
        limit: int | None = None,
        message_ids: list[int] | None = None,
    ) -> list[TelegramOutboxMessage]:
        now = datetime.now(timezone.utc)
        lease_cutoff = now - timedelta(minutes=5)
        batch_size = max(int(limit or self.settings.telegram_outbox_batch_size), 1)
        ready = or_(
            and_(
                TelegramOutboxMessage.status.in_(("PENDING", "RETRY")),
                or_(
                    TelegramOutboxMessage.next_attempt_at.is_(None),
                    TelegramOutboxMessage.next_attempt_at <= now,
                ),
            ),
            and_(
                TelegramOutboxMessage.status == "SENDING",
                TelegramOutboxMessage.updated_at < lease_cutoff,
            ),
        )
        statement = (
            select(TelegramOutboxMessage)
            .where(ready)
            .order_by(TelegramOutboxMessage.created_at.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        if message_ids:
            statement = statement.where(TelegramOutboxMessage.id.in_(message_ids))
        async with AsyncSessionLocal() as db:
            messages = (await db.execute(statement)).scalars().all()
            for item in messages:
                item.status = "SENDING"
                item.attempts = int(item.attempts or 0) + 1
                item.updated_at = now
            await db.commit()
            return list(messages)

    async def mark_delivered(self, message_id: int) -> None:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            item = await db.get(TelegramOutboxMessage, message_id)
            if item is None:
                return
            item.status = "DELIVERED"
            item.text_sent = True
            item.photo_sent = bool(item.photo_sent or item.photo is None)
            item.delivered_at = now
            item.next_attempt_at = None
            item.last_error = None
            item.photo = None
            await db.commit()

    async def mark_progress(
        self,
        message_id: int,
        *,
        text_sent: bool = False,
        photo_sent: bool = False,
    ) -> None:
        async with AsyncSessionLocal() as db:
            item = await db.get(TelegramOutboxMessage, message_id)
            if item is None:
                return
            item.text_sent = bool(item.text_sent or text_sent)
            item.photo_sent = bool(item.photo_sent or photo_sent)
            await db.commit()

    async def mark_failed(self, message_id: int, error_type: str) -> None:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            item = await db.get(TelegramOutboxMessage, message_id)
            if item is None:
                return
            retry_limit = max(int(self.settings.telegram_outbox_retry_limit), 1)
            attempts = int(item.attempts or 0)
            item.last_error = error_type[:128]
            if attempts >= retry_limit:
                item.status = "FAILED"
                item.next_attempt_at = None
            else:
                item.status = "RETRY"
                item.next_attempt_at = now + timedelta(seconds=retry_delay_seconds(attempts))
            await db.commit()

    async def cleanup(self) -> int:
        retention_days = max(int(self.settings.telegram_outbox_retention_days), 1)
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        async with AsyncSessionLocal() as db:
            await cleanup_technical_logs(db)
            result = await db.execute(
                delete(TelegramOutboxMessage).where(
                    TelegramOutboxMessage.status == "DELIVERED",
                    TelegramOutboxMessage.delivered_at < cutoff,
                )
            )
            await db.commit()
            return int(result.rowcount or 0)


def retry_delay_seconds(attempts: int) -> int:
    return min(5 * (2 ** max(int(attempts) - 1, 0)), 300)
