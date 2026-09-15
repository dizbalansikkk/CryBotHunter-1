from dataclasses import dataclass

from app.core.config import get_settings
from app.schemas.dto import MarketCoin


@dataclass(frozen=True)
class MarketQualityAssessment:
    allowed: bool
    reason: str
    risk_multiplier: float = 1.0


class MarketQualityGate:
    def __init__(self) -> None:
        self.settings = get_settings()

    def assess(self, coin: MarketCoin) -> MarketQualityAssessment:
        hard_reasons: list[str] = []
        soft_reasons: list[str] = []

        hard_volume_floor = min(
            self.settings.market_quality_hard_min_quote_volume,
            self.settings.market_quality_min_quote_volume,
        )
        if coin.volume_24h < hard_volume_floor:
            hard_reasons.append(
                f"quote volume {coin.volume_24h:.0f} < {hard_volume_floor:.0f} hard limit"
            )
        elif coin.volume_24h < self.settings.market_quality_min_quote_volume:
            soft_reasons.append(
                f"quote volume {coin.volume_24h:.0f} < {self.settings.market_quality_min_quote_volume:.0f} target"
            )

        if coin.spread_bps > self.settings.market_quality_max_spread_bps * 2:
            hard_reasons.append(
                f"spread {coin.spread_bps:.2f} bps > {self.settings.market_quality_max_spread_bps * 2:.2f} hard limit"
            )
        elif coin.spread_bps > self.settings.market_quality_max_spread_bps:
            soft_reasons.append(
                f"spread {coin.spread_bps:.2f} bps > {self.settings.market_quality_max_spread_bps:.2f} bps"
            )

        absolute_change = abs(coin.price_change_percent)
        if absolute_change > self.settings.market_quality_max_price_change_percent * 1.5:
            hard_reasons.append(
                f"24h move {absolute_change:.2f}% > {self.settings.market_quality_max_price_change_percent * 1.5:.2f}% hard limit"
            )
        elif absolute_change > self.settings.market_quality_max_price_change_percent:
            soft_reasons.append(
                f"24h move {absolute_change:.2f}% > {self.settings.market_quality_max_price_change_percent:.2f}%"
            )

        if hard_reasons:
            return MarketQualityAssessment(False, f"market quality blocked: {'; '.join(hard_reasons)}", 0.0)
        if soft_reasons:
            multiplier = self._risk_multiplier(coin)
            return MarketQualityAssessment(
                True,
                f"market quality reduced risk to {multiplier:.2f}x: {'; '.join(soft_reasons)}",
                multiplier,
            )
        return MarketQualityAssessment(True, "market quality passed", 1.0)

    def _risk_multiplier(self, coin: MarketCoin) -> float:
        spread_ratio = 1.0
        if coin.spread_bps > 0:
            spread_ratio = self.settings.market_quality_max_spread_bps / coin.spread_bps
        change_ratio = self.settings.market_quality_max_price_change_percent / max(abs(coin.price_change_percent), 0.01)
        volume_ratio = coin.volume_24h / max(self.settings.market_quality_min_quote_volume, 1.0)
        multiplier = min(spread_ratio, change_ratio, volume_ratio, 1.0)
        return round(max(self.settings.market_quality_min_risk_multiplier, multiplier), 2)
