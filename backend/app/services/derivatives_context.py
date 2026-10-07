from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import MarketDerivativeSnapshot
from app.schemas.dto import MarketCoin
from app.services.exchange import ExchangeClient


class DerivativesContextService:
    """Enriches spot or futures candidates with explicit derivatives context."""

    def __init__(self, exchange: ExchangeClient) -> None:
        self.exchange = exchange
        self.settings = get_settings()

    async def enrich(self, db: AsyncSession, coins: list[MarketCoin]) -> list[MarketCoin]:
        if not self.settings.derivatives_context_enabled or not coins:
            return coins
        semaphore = asyncio.Semaphore(max(int(self.settings.derivatives_context_concurrency), 1))

        async def fetch(coin: MarketCoin) -> dict[str, Any]:
            async with semaphore:
                try:
                    return await asyncio.wait_for(
                        self.exchange.fetch_derivatives_context(coin.symbol),
                        timeout=max(float(self.settings.derivatives_context_timeout_seconds), 1.0),
                    )
                except Exception as exc:
                    return {"status": "UNKNOWN", "errors": [type(exc).__name__]}

        # Network requests can run concurrently, but one AsyncSession must not
        # execute overlapping database operations. Persist sequentially after
        # all exchange responses have arrived.
        payloads = await asyncio.gather(*(fetch(coin) for coin in coins))
        enriched: list[MarketCoin] = []
        for coin, payload in zip(coins, payloads, strict=True):
            enriched.append(await self._persist_context(db, coin, payload))
        return enriched

    async def _persist_context(
        self,
        db: AsyncSession,
        coin: MarketCoin,
        payload: dict[str, Any],
    ) -> MarketCoin:
        funding = self._optional(payload.get("funding_rate"))
        interest = self._optional(payload.get("open_interest"))
        previous = (
            await db.execute(
                select(MarketDerivativeSnapshot)
                .where(
                    MarketDerivativeSnapshot.symbol == coin.symbol,
                    MarketDerivativeSnapshot.open_interest.is_not(None),
                )
                .order_by(MarketDerivativeSnapshot.captured_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        oi_change = None
        if previous and interest is not None and float(previous.open_interest or 0.0) > 0:
            oi_change = (interest / float(previous.open_interest) - 1.0) * 100
        snapshot = MarketDerivativeSnapshot(
            symbol=coin.symbol,
            funding_rate=funding,
            open_interest=interest,
            open_interest_change_percent=oi_change,
            long_short_ratio=self._optional(payload.get("long_short_ratio")),
            liquidation_notional=self._optional(payload.get("liquidation_notional")),
            status=str(payload.get("status") or "UNKNOWN"),
            captured_at=datetime.now(timezone.utc),
        )
        db.add(snapshot)
        context: dict[str, Any] = dict(coin.market_context or {})
        context["derivatives"] = {
            "status": snapshot.status,
            "funding_rate_available": funding is not None,
            "open_interest_available": interest is not None,
            "open_interest_change_percent": round(oi_change, 4) if oi_change is not None else None,
            "long_short_ratio": snapshot.long_short_ratio,
            "liquidation_notional": snapshot.liquidation_notional,
            "errors": list(payload.get("errors") or []),
            "observed_at": snapshot.captured_at.isoformat(),
        }
        # Keep compatibility fields numeric while the context records whether
        # zero is real or merely unavailable.
        return coin.model_copy(
            update={
                "funding_rate": funding if funding is not None else 0.0,
                "open_interest": interest if interest is not None else 0.0,
                "market_context": context,
            }
        )

    def _optional(self, value: object) -> float | None:
        try:
            parsed = float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        if parsed is None or parsed != parsed or parsed in {float("inf"), float("-inf")}:
            return None
        return parsed
