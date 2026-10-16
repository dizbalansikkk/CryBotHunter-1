"""Compare virtual policies on an overlapping forward period, never live orders."""
from datetime import datetime, timezone
import math

from sqlalchemy import select
from app.models.entities import ShadowTrade


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def compare_trades(candidate, incumbent, min_trades=20, min_days=7) -> dict:
    if not candidate or not incumbent:
        return {"status": "PENDING", "reason": "Недостаточно параллельных Paper-сделок основной и новой модели"}
    start = max(min(aware(t.entered_at) for t in candidate), min(aware(t.entered_at) for t in incumbent))
    end = min(max(aware(t.closed_at) for t in candidate), max(aware(t.closed_at) for t in incumbent))
    groups = [[t for t in rows if aware(t.entered_at) >= start and aware(t.closed_at) <= end]
              for rows in (candidate, incumbent)]
    stats = []
    for rows in groups:
        returns = []
        for trade in sorted(rows, key=lambda t: (aware(t.closed_at), t.id)):
            notional = trade.entry_price * trade.volume
            if not math.isfinite(notional) or notional <= 0 or not math.isfinite(trade.pnl):
                return {"status": "PENDING", "reason": "Некорректные данные виртуальных исполнений"}
            returns.append(trade.pnl / notional * 100)
        equity = peak = drawdown = 0.0
        for value in returns:
            equity += value
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        loss = -sum(r for r in returns if r < 0)
        stats.append({"trades": len(rows), "mean_net_return_percent": sum(returns) / len(rows) if rows else None,
            "normalized_drawdown_points": drawdown,
            "profit_factor": sum(r for r in returns if r > 0) / loss if loss else None})
    result = {"start": start.isoformat(), "end": end.isoformat(), "candidate": stats[0], "incumbent": stats[1],
              "note": "Доходность нормирована на стоимость каждой Paper-позиции; просадка нормированной кривой — не просадка реального счёта. Только закрытые сделки, комиссии и проскальзывание симулятора; funding не моделируется."}
    if any(s["trades"] < min_trades for s in stats) or (end - start).total_seconds() < min_days * 86400:
        return {**result, "status": "PENDING", "reason": f"Нужно минимум {min_trades} закрытых сделок каждой модели и {min_days} дней общего периода"}
    better = (stats[0]["mean_net_return_percent"] > max(stats[1]["mean_net_return_percent"], 0)
              and stats[0]["normalized_drawdown_points"] <= stats[1]["normalized_drawdown_points"])
    return {**result, "status": "PASSED" if better else "FAILED",
            "reason": "На общей Paper-выборке доходность выше, просадка не хуже" if better else "Улучшение доходности при сохранении просадки не подтверждено"}


async def compare_models(db, candidate, incumbent, min_trades=20):
    rows = (await db.execute(select(ShadowTrade).where(
        ShadowTrade.model_id.in_([candidate.id, incumbent.id]), ShadowTrade.status == "CLOSED",
        ShadowTrade.entered_at >= candidate.created_at,
    ).order_by(ShadowTrade.closed_at.asc()))).scalars().all()
    return compare_trades([r for r in rows if r.model_id == candidate.id],
                          [r for r in rows if r.model_id == incumbent.id], min_trades=min_trades)
