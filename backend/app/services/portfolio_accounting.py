from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import EquitySnapshot, Position
from app.services.exchange import ExchangeClient
from app.services.risk_manager import DrawdownAssessment


class PortfolioAccountingService:
    """Builds a time series of bot-controlled equity and risk.

    Spot equity deliberately includes only USDT plus inventory represented by
    open bot positions. Unrelated wallet assets are excluded from trading risk.
    """

    def __init__(self, exchange: ExchangeClient) -> None:
        self.exchange = exchange
        self.settings = get_settings()

    async def capture(self, db: AsyncSession, *, force: bool = False) -> EquitySnapshot | None:
        if not self.settings.equity_snapshot_enabled:
            return None
        paper = self.settings.paper_trading or not self.settings.live_trading_enabled
        mode = "PAPER" if paper else "LIVE"
        latest = (
            await db.execute(select(EquitySnapshot).where(EquitySnapshot.mode == mode).order_by(EquitySnapshot.captured_at.desc()).limit(1))
        ).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        if latest and not force:
            captured = self._aware(latest.captured_at)
            interval = timedelta(seconds=max(int(self.settings.equity_snapshot_interval_seconds), 1))
            if now - captured < interval:
                return latest

        positions = list((await db.execute(select(Position).where(Position.status == "OPEN"))).scalars().all())
        closed_pnl = float(
            (
                await db.execute(
                    select(func.coalesce(func.sum(Position.pnl), 0.0)).where(Position.status == "CLOSED")
                )
            ).scalar_one()
        )
        unrealized = sum(float(position.pnl or 0.0) for position in positions)
        gross = sum(abs(float(position.current_price or 0.0) * float(position.volume or 0.0)) for position in positions)
        net = sum(
            (1.0 if position.side == "LONG" else -1.0)
            * float(position.current_price or 0.0)
            * float(position.volume or 0.0)
            for position in positions
        )
        stop_risk = sum(self._stop_risk(position) for position in positions)
        derivatives_check = getattr(self.exchange, "is_derivatives_market", None)
        is_derivatives = bool(derivatives_check()) if callable(derivatives_check) else False
        if paper:
            total = float(self.settings.paper_starting_balance) + closed_pnl + unrealized
            leverage = max(min(self.settings.futures_leverage, self.settings.futures_max_leverage), 1)
            reserved = gross / leverage if is_derivatives else sum(
                abs(float(position.current_price or 0.0) * float(position.volume or 0.0))
                for position in positions if position.side == "LONG"
            )
            free = max(total - reserved, 0.0)
            source = "PAPER_LEDGER"
        else:
            balances = await self.exchange.get_balance()
            free_balances = await self.exchange.get_free_balance()
            quote_total = float(balances.get("USDT") or 0.0)
            quote_free = float(free_balances.get("USDT") or 0.0)
        if not paper and is_derivatives:
            total = quote_total
            free = quote_free
            source = "DERIVATIVES_BALANCE"
        elif not paper:
            tracked_inventory = sum(
                abs(float(position.current_price or 0.0) * float(position.volume or 0.0))
                for position in positions if position.side == "LONG"
            )
            total = quote_total + tracked_inventory
            free = quote_free
            source = "SPOT_BOT_LEDGER"

        historical_peak = float(
            (
                await db.execute(select(func.coalesce(func.max(EquitySnapshot.total_equity), 0.0)).where(EquitySnapshot.mode == mode))
            ).scalar_one()
        )
        peak = max(total, historical_peak)
        drawdown = max((peak - total) / peak * 100, 0.0) if peak > 0 else 100.0
        snapshot = EquitySnapshot(
            mode=mode,
            source=source,
            total_equity=round(total, 8),
            free_equity=round(free, 8),
            reserved_equity=round(max(total - free, 0.0), 8),
            realized_pnl=round(closed_pnl, 8),
            unrealized_pnl=round(unrealized, 8),
            open_stop_risk=round(stop_risk, 8),
            gross_exposure=round(gross, 8),
            net_exposure=round(net, 8),
            peak_equity=round(peak, 8),
            drawdown_percent=round(drawdown, 4),
            context={
                "market_type": getattr(self.exchange, "market_type", "spot"),
                "open_positions": len(positions),
                "included_assets": ["USDT", *sorted({position.symbol.split("/", 1)[0] for position in positions})],
            },
            captured_at=now,
        )
        db.add(snapshot)
        await db.flush()
        return snapshot

    def assessment(self, snapshot: EquitySnapshot, threshold_percent: float) -> DrawdownAssessment:
        return DrawdownAssessment(
            starting_equity=round(float(snapshot.peak_equity), 8),
            peak_equity=round(float(snapshot.peak_equity), 8),
            current_equity=round(float(snapshot.total_equity), 8),
            drawdown_percent=round(float(snapshot.drawdown_percent), 4),
            threshold_percent=round(float(threshold_percent), 4),
            emergency=float(snapshot.drawdown_percent) >= float(threshold_percent),
        )

    def _stop_risk(self, position: Position) -> float:
        price = float(position.current_price or position.entry_price or 0.0)
        stop = float(position.stop or 0.0)
        volume = max(float(position.volume or 0.0), 0.0)
        if price <= 0 or stop <= 0 or volume <= 0:
            return 0.0
        distance = price - stop if position.side == "LONG" else stop - price
        return max(distance, 0.0) * volume

    def _aware(self, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
