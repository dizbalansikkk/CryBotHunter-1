"""Eight-hour closed-position results; no reconstructed equity or double-counted fees."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from app.models.entities import LogEntry, Position


def completed_slice(now, timezone_name):
    local = now.astimezone(ZoneInfo(timezone_name)) - timedelta(minutes=5)
    end = local.replace(hour=(local.hour // 8) * 8, minute=0, second=0, microsecond=0)
    return end - timedelta(hours=8), end


def position_lane(position):
    context = position.entry_context or {}
    if context.get("paper_exploration") is True:
        return "paper_exploration"
    if context.get("paper_exploration") is False:
        return "strategy"
    return "unknown"


def metrics(positions):
    def aware(value):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    positions = sorted(positions, key=lambda p: (aware(p.closed_at), p.id or 0))
    values = [float(p.pnl or 0) for p in positions]
    wins, losses = [v for v in values if v > 0], [v for v in values if v < 0]
    equity = peak = drawdown = 0.0
    breaches = unknown_risk = 0
    for position, value in zip(positions, values):
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        risk = float((position.entry_context or {}).get("planned_risk") or 0)
        if risk <= 0:
            unknown_risk += 1
        elif -value > risk + 0.0001:
            breaches += 1
    return {"closed": len(values), "net": sum(values),
            "average_win": sum(wins) / len(wins) if wins else None,
            "average_loss": sum(losses) / len(losses) if losses else None,
            "drawdown": drawdown, "risk_breaches": breaches, "unknown_risk": unknown_risk}


async def build_slice(db, start, end):
    positions = list((await db.execute(select(Position).where(
        Position.status == "CLOSED", Position.closed_at >= start, Position.closed_at < end,
    ))).scalars().all())
    event = LogEntry.context["event"].as_string()
    lane = LogEntry.context["lane"].as_string()
    reason = func.coalesce(LogEntry.context["error_type"].as_string(),
                           LogEntry.context["gate"].as_string(), "UNKNOWN")
    rows = (await db.execute(select(event, lane, reason, func.count()).where(
        LogEntry.created_at >= start, LogEntry.created_at < end,
        event.in_(["ENTRY_SKIPPED", "POSITION_CLOSE_FAILED", "EXIT_REVIEW_REQUIRED"]),
    ).group_by(event, lane, reason))).all()
    groups = {}
    for name in ("strategy", "paper_exploration", "unknown"):
        group = metrics([p for p in positions if position_lane(p) == name])
        skipped, failed = Counter(), Counter()
        for event_name, event_lane, why, count in rows:
            event_lane = event_lane if event_lane in {"strategy", "paper_exploration"} else "unknown"
            if event_lane == name:
                (skipped if event_name == "ENTRY_SKIPPED" else failed)[why] += count
        group.update(skipped=skipped, failed=failed)
        groups[name] = group
    return groups


def format_slice(groups, start, end):
    lines = ["<b>ТОРГОВЫЙ СРЕЗ · 8 часов</b>",
             f"{start:%d.%m %H:%M} — {end:%d.%m %H:%M} ({escape(str(end.tzinfo))})"]
    for name, title in (("strategy", "Основная стратегия"), ("paper_exploration", "Обучение"),
                        ("unknown", "Без метки режима")):
        data = groups[name]
        avg = lambda v: "нет" if v is None else f"{v:+.4f}"
        reasons = lambda counts: ", ".join(f"{escape(str(k))}: {v}" for k, v in counts.most_common(3)) or "нет"
        lines.extend([f"\n<b>{title}</b> · закрыто {data['closed']}",
            f"Чистый PnL закрытых позиций: {data['net']:+.4f} USDT",
            f"Средний плюс / минус: {avg(data['average_win'])} / {avg(data['average_loss'])}",
            f"Просадка по закрытиям: {data['drawdown']:.4f} USDT",
            f"Превышения планового риска: {data['risk_breaches']} · риск неизвестен: {data['unknown_risk']}",
            f"Пропуски входов ({sum(data['skipped'].values())}): {reasons(data['skipped'])}",
            f"Сбои выхода ({sum(data['failed'].values())}): {reasons(data['failed'])}"])
    lines.append("\nPnL — по журналу, комиссии повторно не вычитаются. Funding учитывается только если записан. "
                 "Просадка по закрытиям не отражает плавающие убытки открытых позиций. "
                 "Причины — топ-3; счётчики относятся к событиям, не уникальным сделкам.")
    return "\n".join(lines)
