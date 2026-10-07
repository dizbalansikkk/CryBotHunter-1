from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import SignalObservation
from app.schemas.dto import MarketCoin, TradingDecision


class CounterfactualService:
    """Learns from opened, skipped and WAIT decisions on the same horizon."""

    def __init__(self) -> None:
        self.settings = get_settings()

    async def resolve_due(self, db: AsyncSession, coins: list[MarketCoin]) -> int:
        if not self.settings.counterfactual_tracking_enabled:
            return 0
        now = datetime.now(timezone.utc)
        due = list(
            (
                await db.execute(
                    select(SignalObservation)
                    .where(
                        SignalObservation.resolved.is_(False),
                        SignalObservation.horizon_at <= now,
                    )
                    .order_by(SignalObservation.horizon_at.asc())
                    .limit(500)
                )
            ).scalars().all()
        )
        prices = {coin.symbol: float(coin.price) for coin in coins if float(coin.price) > 0}
        resolved = 0
        for observation in due:
            price = prices.get(observation.symbol)
            if price is None or float(observation.reference_price or 0.0) <= 0:
                continue
            direction = 1.0 if observation.signal == "BUY" else -1.0 if observation.signal == "SELL" else 0.0
            raw_return = (price / float(observation.reference_price) - 1.0) * 100
            observation.outcome_price = round(price, 8)
            observation.outcome_return_percent = round(raw_return * direction, 6) if direction else round(abs(raw_return), 6)
            observation.resolved = True
            resolved += 1
        return resolved

    def record_cycle(
        self,
        db: AsyncSession,
        *,
        cycle_id: str,
        decisions: list[TradingDecision],
        coins: list[MarketCoin],
    ) -> int:
        if not self.settings.counterfactual_tracking_enabled:
            return 0
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(minutes=max(int(self.settings.counterfactual_horizon_minutes), 1))
        market = {coin.symbol: coin for coin in coins}
        written = 0
        for decision in decisions:
            coin = market.get(decision.symbol)
            if coin is None or float(coin.price) <= 0:
                continue
            context = dict(coin.market_context or {})
            db.add(
                SignalObservation(
                    cycle_id=cycle_id,
                    symbol=decision.symbol,
                    signal=decision.signal,
                    strategy_score=int(decision.score),
                    final_score=float(decision.score),
                    action=decision.action,
                    reason=str(decision.reason),
                    reference_price=float(coin.price),
                    regime=coin.regime,
                    context={
                        "rating": coin.rating,
                        "regime_score": coin.regime_score,
                        "funding_rate": coin.funding_rate,
                        "open_interest": coin.open_interest,
                        "derivatives": context.get("derivatives"),
                        "strategy_router": context.get("strategy_router"),
                    },
                    horizon_at=horizon,
                    observed_at=now,
                )
            )
            written += 1
        return written
