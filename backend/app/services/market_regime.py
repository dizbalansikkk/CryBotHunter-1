from dataclasses import dataclass

from app.core.config import get_settings
from app.schemas.dto import MarketCoin


@dataclass(frozen=True)
class MarketRegime:
    name: str
    score: int
    reason: str


class MarketRegimeDetector:
    def detect(self, row: dict[str, float | str] | MarketCoin) -> MarketRegime:
        price = self._value(row, "price")
        atr = self._value(row, "atr")
        ema20 = self._value(row, "ema20")
        ema50 = self._value(row, "ema50")
        ema200 = self._value(row, "ema200")
        rsi = self._value(row, "rsi")
        volume = self._value(row, "volume_24h")
        open_interest = self._value(row, "open_interest")
        funding = self._value(row, "funding_rate")

        atr_percent = atr / max(price, 1) * 100
        open_interest_too_thin = 0 < open_interest < 100_000_000
        hard_volume_floor = max(float(get_settings().market_quality_hard_min_quote_volume), 0.0)
        if volume < hard_volume_floor or open_interest_too_thin:
            return MarketRegime("LOW_LIQUIDITY", 20, "Available volume or open interest is too thin.")
        if atr_percent > 8 or abs(funding) > 0.08:
            return MarketRegime("HIGH_VOLATILITY", 25, "ATR or funding risk is too high.")
        if ema20 > ema50 > ema200 and price > ema50 and 50 <= rsi <= 76:
            return MarketRegime("TRENDING_UP", min(100, int(70 + atr_percent * 4)), "Bullish EMA stack with controlled momentum.")
        if ema20 < ema50 < ema200 and price < ema50 and 24 <= rsi <= 50:
            return MarketRegime("TRENDING_DOWN", min(100, int(70 + atr_percent * 4)), "Bearish EMA stack with controlled momentum.")
        if atr_percent < 1.2 or 45 <= rsi <= 55:
            return MarketRegime("RANGING", 45, "Low directional pressure or compressed volatility.")
        return MarketRegime("MIXED", 55, "Signals are not aligned enough for a clean trend regime.")

    def _value(self, row: dict[str, float | str] | MarketCoin, key: str) -> float:
        value = row[key] if isinstance(row, dict) else getattr(row, key)
        return float(value or 0)
