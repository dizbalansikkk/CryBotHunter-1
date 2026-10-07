from __future__ import annotations

from dataclasses import dataclass

from app.schemas.dto import MarketCoin, StrategySignal
from app.services.strategy import StrategyCore


@dataclass(frozen=True)
class RoutedStrategies:
    live: StrategySignal
    regime_family: str
    shadow: list[StrategySignal]


class RegimeStrategyRouter:
    """Keeps new strategy families in shadow until evidence promotes them."""

    def __init__(self) -> None:
        self.trend = StrategyCore()

    def evaluate(self, coin: MarketCoin) -> RoutedStrategies:
        live = self.trend.evaluate(coin)
        if coin.regime in {"TRENDING_UP", "TRENDING_DOWN"}:
            family = "TREND_PULLBACK"
        elif coin.regime == "RANGING":
            family = "RANGE_MEAN_REVERSION"
        else:
            family = "VOLATILITY_BREAKOUT"
        shadow = [self._breakout(coin), self._mean_reversion(coin), self._compression(coin)]
        return RoutedStrategies(live=live, regime_family=family, shadow=shadow)

    def _breakout(self, coin: MarketCoin) -> StrategySignal:
        volume_ratio = coin.volume_24h / coin.volume_average_24h if coin.volume_average_24h > 0 else 0.0
        buy = coin.price_change_percent >= 1.5 and coin.macd > 0 and coin.price > coin.ema20 and volume_ratio >= 1.2
        sell = coin.price_change_percent <= -1.5 and coin.macd < 0 and coin.price < coin.ema20 and volume_ratio >= 1.2
        signal = "BUY" if buy else "SELL" if sell else "WAIT"
        score = min(100, round(45 + abs(coin.price_change_percent) * 8 + max(volume_ratio - 1, 0) * 15))
        return StrategySignal(
            symbol=coin.symbol,
            signal=signal,
            score=score,
            reasons=[f"shadow breakout volume={volume_ratio:.2f}x change={coin.price_change_percent:+.2f}%"],
        )

    def _mean_reversion(self, coin: MarketCoin) -> StrategySignal:
        distance_atr = (coin.price - coin.ema20) / max(coin.atr, coin.price * 0.0001)
        allowed = coin.regime == "RANGING"
        buy = allowed and distance_atr <= -0.6 and coin.rsi <= 45
        sell = allowed and distance_atr >= 0.6 and coin.rsi >= 55
        signal = "BUY" if buy else "SELL" if sell else "WAIT"
        score = min(100, round(50 + abs(distance_atr) * 15 + abs(coin.rsi - 50)))
        return StrategySignal(
            symbol=coin.symbol,
            signal=signal,
            score=score,
            reasons=[f"shadow mean reversion regime={coin.regime} distance={distance_atr:+.2f}ATR RSI={coin.rsi:.1f}"],
        )

    def _compression(self, coin: MarketCoin) -> StrategySignal:
        atr_percent = coin.atr / max(coin.price, 1e-9) * 100
        volume_ratio = coin.volume_24h / coin.volume_average_24h if coin.volume_average_24h > 0 else 0.0
        compressed = 0.25 <= atr_percent <= 1.2 and volume_ratio >= 1.15
        buy = compressed and coin.price > coin.ema20 and coin.macd > 0
        sell = compressed and coin.price < coin.ema20 and coin.macd < 0
        signal = "BUY" if buy else "SELL" if sell else "WAIT"
        score = min(100, round(55 + max(volume_ratio - 1, 0) * 25 + (1.2 - min(atr_percent, 1.2)) * 10))
        return StrategySignal(
            symbol=coin.symbol,
            signal=signal,
            score=score,
            reasons=[f"shadow compression ATR={atr_percent:.2f}% volume={volume_ratio:.2f}x"],
        )
