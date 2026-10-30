"""Persistent day-boundary kill switch. Exit orders are deliberately exempt."""
import math
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select, func, or_
from app.models.entities import DailyRiskState, EquitySnapshot, Position, Trade, LogEntry


@dataclass(frozen=True)
class DailyRiskDecision:
    allowed: bool
    balance: float
    starting_day_balance: float
    daily_pnl: float
    reason: str
    gross_exposure: float = 0.0


class DailyRiskService:
    def __init__(self, settings, exchange):
        self.settings, self.exchange = settings, exchange

    async def check(self, db, now=None, quotes=None):
        now = now or datetime.now(timezone.utc)
        local = now.astimezone(ZoneInfo(self.settings.trading_day_timezone))
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        mode = "PAPER" if self.settings.paper_trading else "LIVE"
        scope = mode + ":" + self.settings.trading_day_timezone
        existing = (await db.execute(select(DailyRiskState).where(DailyRiskState.scope == scope,
            DailyRiskState.trading_day == local.date()).with_for_update())).scalar_one_or_none()
        positions = list((await db.execute(select(Position).where(Position.status == "OPEN"))).scalars().all())
        if self.settings.paper_trading:
            ledger = float((await db.execute(select(func.coalesce(func.sum(Trade.profit), 0)))).scalar_one())
            unrealized = 0.0
            gross_exposure = 0.0
            if positions:
                if quotes is None:
                    quotes = await self.exchange.fetch_tickers(list({p.symbol for p in positions}))
                for p in positions:
                    ticker = quotes.get(p.symbol, {})
                    price = float(ticker.get("bid" if p.side == "LONG" else "ask") or ticker.get("last") or 0)
                    if not math.isfinite(price) or price <= 0:
                        raise ValueError("Fresh position quote unavailable for daily risk")
                    unrealized += (price - p.entry_price) * p.volume * (1 if p.side == "LONG" else -1)
                    gross_exposure += abs(price * p.volume)
            balance = self.settings.paper_starting_balance + ledger + unrealized
        else:
            # Keep live entries blocked in the new policy until live account
            # equity/funding semantics and native protection have been validated.
            raise ValueError("ATR policy is paper-validated only; live entry veto")
        if not math.isfinite(balance) or balance <= 0:
            raise ValueError("Invalid account equity")
        snapshot = (await db.execute(select(EquitySnapshot).where(
            EquitySnapshot.mode == mode, EquitySnapshot.captured_at <= day_start)
            .order_by(EquitySnapshot.captured_at.desc()).limit(1))).scalar_one_or_none()
        if snapshot:
            captured = snapshot.captured_at
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=timezone.utc)
            if day_start - captured > timedelta(seconds=max(120, self.settings.equity_snapshot_interval_seconds * 2)):
                snapshot = None
        if existing:
            baseline, source = existing.starting_day_balance, existing.baseline_source
        elif snapshot:
            baseline = float(snapshot.total_equity)
            source = "LAST_PRE_DAY_EQUITY_SNAPSHOT"
        else:
            carry = (await db.execute(select(Position.id).where(Position.entered_at < day_start,
                or_(Position.status == "OPEN", Position.closed_at >= day_start)).limit(1))).scalar_one_or_none()
            if carry is not None:
                raise ValueError("Starting-day equity unavailable for carried positions; entry veto")
            prior = float((await db.execute(select(func.coalesce(func.sum(Position.pnl), 0)).where(
                Position.status == "CLOSED", Position.closed_at < day_start))).scalar_one())
            baseline = self.settings.paper_starting_balance + prior
            source = "BOOTSTRAP_PRIOR_CLOSED_PNL_NO_CARRY"
        if not math.isfinite(baseline) or baseline <= 0:
            raise ValueError("Invalid starting-day equity")
        # Atomic create + locked read: restarts and concurrent workers cannot
        # reset the baseline or clear a latched loss after an equity recovery.
        if db.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        await db.execute(insert(DailyRiskState).values(scope=scope, trading_day=local.date(),
            starting_day_balance=baseline, daily_pnl=0, tripped=False, baseline_source=source)
            .on_conflict_do_nothing(index_elements=["scope", "trading_day"]))
        state = (await db.execute(select(DailyRiskState).where(DailyRiskState.scope == scope,
            DailyRiskState.trading_day == local.date()).with_for_update())).scalar_one()
        state.daily_pnl = balance - state.starting_day_balance
        breach = state.daily_pnl <= -state.starting_day_balance * .025
        if breach and not state.tripped:
            state.tripped = True
            db.add(LogEntry(level="ERROR",
                message="Daily equity loss reached 2.5%; new entries locked until next trading day",
                context={"event_type": "DAILY_RISK_KILL_SWITCH", "day": str(local.date()), "starting_day_balance": state.starting_day_balance,
                         "daily_pnl": state.daily_pnl, "baseline_source": state.baseline_source}))
        await db.flush()
        if state.tripped:
            await db.commit()  # Persist veto even when caller aborts entry.
        return DailyRiskDecision(not state.tripped, balance, state.starting_day_balance,
            state.daily_pnl, "daily_loss_limit" if state.tripped else "daily_risk_accepted", gross_exposure)
