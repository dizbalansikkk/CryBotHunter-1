"""Read-only operational evidence. Missing history is never reported as zero."""
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.models.entities import LogEntry, Position, LearningRule, StrategyRelease, RlModel


def finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def position_economics(position) -> dict:
    context = position.entry_context or {}
    execution = context.get("entry_execution") or {}
    venue = execution.get("venue_setup") or {}
    volume = finite(execution.get("volume"))
    # Remaining position.volume is NOT the entry volume after scale-out.
    price = finite(position.entry_price)
    notional = price * volume if price is not None and volume is not None else None
    leverage = finite(venue.get("leverage"))
    risk = finite((context.get("exit_plan") or {}).get("initial_risk"))
    return {
        "id": position.id, "symbol": position.symbol, "side": position.side,
        "status": position.status, "entered_at": position.entered_at, "closed_at": position.closed_at,
        "entry_price": price, "entry_volume": volume, "notional": notional,
        "leverage": leverage, "margin_mode": venue.get("margin_mode"),
        "estimated_margin": notional / leverage if notional is not None and leverage and leverage > 0 else None,
        "initial_stop_risk": risk * volume if risk is not None and volume is not None else None,
        "entry_fee": finite(execution.get("fee")), "entry_slippage": finite(execution.get("slippage")),
        "pnl": finite(position.pnl), "virtual_subminimum": bool(execution.get("paper_learning_subminimum")),
        "reason": context.get("decision_reason"),
        "mode": context.get("execution_mode"),
        "note": "Маржа расчётная; риск до начального стопа без комиссии выхода, проскальзывания и funding. PnL открытой позиции не финальный.",
    }


class ControlDashboardService:
    async def entries(self, db: AsyncSession) -> dict:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        event = LogEntry.context["event"].as_string()
        symbol = LogEntry.context["symbol"].as_string()
        ranked = select(LogEntry.id, func.row_number().over(
            partition_by=symbol, order_by=LogEntry.id.desc()).label("rank")).where(
                LogEntry.created_at >= cutoff,
                event.in_(["ENTRY_OPENED", "ENTRY_SKIPPED", "ENTRY_REJECTED"]), symbol.is_not(None)).subquery()
        rows = (await db.execute(select(LogEntry).join(ranked, ranked.c.id == LogEntry.id)
            .where(ranked.c.rank == 1).order_by(LogEntry.created_at.desc()).limit(100))).scalars().all()
        keys = ("event", "symbol", "signal", "score", "lane", "gate", "reason", "risk_percent",
                "candidate_stop_risk", "daily_risk_limit", "reserved_stop_risk", "sizing")
        return {"generated_at": datetime.now(timezone.utc), "window_hours": 24,
            "entries": [{"id": row.id, "checked_at": row.created_at,
                         **{key: row.context.get(key) for key in keys}} for row in rows]}

    async def positions(self, db: AsyncSession) -> list[dict]:
        rows = (await db.execute(select(Position).order_by(Position.entered_at.desc(), Position.id.desc()).limit(50))).scalars().all()
        return [position_economics(row) for row in rows]

    async def lifecycle(self, db: AsyncSession, position_id: int) -> dict | None:
        position = await db.get(Position, position_id)
        if position is None:
            return None
        rows = (await db.execute(select(LogEntry).where(
            LogEntry.context["position_id"].as_string() == str(position_id))
            .order_by(LogEntry.created_at.asc(), LogEntry.id.asc()).limit(501))).scalars().all()
        keys = ("event", "reason", "exit_reason", "entry_price", "exit_price", "filled_volume",
                "closed_volume", "remaining_volume", "realized_pnl", "pnl", "fee", "stop", "take",
                "old_stop", "new_stop", "execution_status", "executed_at", "execution_time_source",
                "partial_profit", "exit_fee", "entry_fee", "stage", "fill_confirmed", "mode")
        return {"position": position_economics(position), "truncated": len(rows) > 500,
                "events": [{"id": row.id, "at": row.created_at, "message": row.message,
                            **{key: row.context.get(key) for key in keys}} for row in rows[:500]],
                "note": "Только записанные события. Отсутствие события не доказывает отсутствие исполнения; для старых сделок журнал может быть неполным."}

    async def learning(self, db: AsyncSession) -> dict:
        updates = (await db.execute(select(LogEntry).where(LogEntry.context["event"].as_string() == "LEARNING_UPDATED")
            .order_by(LogEntry.id.desc()).limit(30))).scalars().all()
        rules = (await db.execute(select(LearningRule).order_by(LearningRule.updated_at.desc()).limit(30))).scalars().all()
        releases = (await db.execute(select(StrategyRelease).order_by(StrategyRelease.deployed_at.desc()).limit(10))).scalars().all()
        changes = []
        for current, previous in zip(releases, releases[1:]):
            before, after = previous.parameters or {}, current.parameters or {}
            changes.append({"version": current.version, "at": current.deployed_at,
                "parameters": [{"name": key, "before": before.get(key), "after": after.get(key)}
                    for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]})
        models = (await db.execute(select(RlModel).options(load_only(RlModel.id, RlModel.symbol,
            RlModel.timeframe, RlModel.status, RlModel.is_active, RlModel.metrics, RlModel.created_at))
            .order_by(RlModel.created_at.desc()).limit(30))).scalars().all()
        return {"updates": [{"id": row.id, "at": row.created_at,
                **{k: row.context.get(k) for k in ("position_id", "symbol", "pnl", "applied", "rules_updated", "outcome_r", "penalty_delta", "learning_basis")}}
                    for row in updates],
            "rules": [{"id": r.id, "symbol": r.scope, "feature": f"{r.feature_key}={r.feature_value}",
            "observations": r.observations, "wins": r.wins, "losses": r.losses,
            "total_profit": r.total_profit, "penalty": r.penalty, "updated_at": r.updated_at,
            "reason": r.last_reason} for r in rules], "changes": changes,
            "models": [{"id": m.id, "symbol": m.symbol, "timeframe": m.timeframe,
                "status": m.status, "active": m.is_active,
                "historical": {k: v for k, v in (m.metrics or {}).items()
                    if k in {"profit_factor", "win_rate", "return_percent", "trades", "max_drawdown_percent", "backtest_passed"}},
                "forward": (m.metrics or {}).get("forward"),
                "comparison": (m.metrics or {}).get("comparison"),
                "forward_status": (m.metrics or {}).get("forward_status"),
                "promoted_after_forward_test": (m.metrics or {}).get("promoted_after_forward_test", False)} for m in models],
            "note": "Суммы по правилам перекрываются: одна сделка обучает несколько правил. Изменение параметров или прохождение порогов не доказывает превосходство над предыдущей версией."}

    async def storage(self, db: AsyncSession) -> dict:
        from app.core.config import get_settings
        settings = get_settings()
        rows = (await db.execute(text("""
            SELECT relname AS name, n_live_tup AS estimated_rows,
                   pg_total_relation_size(relid) AS bytes
            FROM pg_stat_user_tables WHERE schemaname = 'public'
            ORDER BY pg_total_relation_size(relid) DESC LIMIT 30
        """))).mappings().all()
        return {"tables": [dict(row) for row in rows],
                "database_bytes": (await db.execute(text("SELECT pg_database_size(current_database())"))).scalar_one(),
                "technical_log_days": max(settings.log_retention_days, 30),
                "cleanup_enabled": settings.technical_log_cleanup_enabled,
                "telegram_days": settings.telegram_outbox_retention_days,
                "note": "Размеры включают индексы; количество строк — оценка PostgreSQL. Очистка удаляет только старые технические INFO-сводки. Сделки, события, ошибки, обучение и свечи сохраняются. Освобождённые страницы база может использовать повторно без уменьшения файла."}
