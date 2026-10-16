from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, or_

from app.core.config import get_settings
from app.models.entities import LogEntry


def expired_technical_logs(cutoff):
    # Explicit allowlist: never delete fills, entry decisions, learning or
    # errors just because they are old. Limit batches to avoid long locks.
    return select(LogEntry.id).where(
        LogEntry.level == "INFO", LogEntry.created_at < cutoff,
        LogEntry.context["event"].as_string().is_(None),
        or_(LogEntry.message.startswith("Торговый цикл:"),
            LogEntry.message.startswith("Candle-worker обновил/добавил")),
    ).order_by(LogEntry.id).limit(1000)


async def cleanup_technical_logs(db) -> int:
    settings = get_settings()
    if not settings.technical_log_cleanup_enabled:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(settings.log_retention_days, 30))
    result = await db.execute(delete(LogEntry).where(LogEntry.id.in_(expired_technical_logs(cutoff))))
    return int(result.rowcount or 0)
