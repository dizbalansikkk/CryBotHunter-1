from app.schemas.dto import MarketCoin
from app.services.market_quality import MarketQualityGate


def coin(**overrides):
    data = {
        "symbol": "BTC/USDT",
        "price": 100.0,
        "volume_24h": 500_000_000.0,
        "price_change_percent": 4.0,
        "atr": 2.0,
        "rsi": 60.0,
        "ema20": 105.0,
        "ema50": 100.0,
        "ema200": 90.0,
        "macd": 5.0,
        "funding_rate": 0.0,
        "open_interest": 500_000_000.0,
        "bid": 99.95,
        "ask": 100.05,
        "spread_bps": 10.0,
        "rating": 88,
    }
    data.update(overrides)
    return MarketCoin(**data)


def test_market_quality_allows_clean_market():
    decision = MarketQualityGate().assess(coin())

    assert decision.allowed is True
    assert decision.reason == "market quality passed"
    assert decision.risk_multiplier == 1.0


def test_market_quality_reduces_risk_for_wide_but_tradable_spread():
    decision = MarketQualityGate().assess(coin(spread_bps=30.0))

    assert decision.allowed is True
    assert "reduced risk" in decision.reason
    assert 0 < decision.risk_multiplier < 1


def test_market_quality_blocks_untradable_market():
    decision = MarketQualityGate().assess(coin(volume_24h=20_000_000.0, spread_bps=80.0))

    assert decision.allowed is False
    assert decision.risk_multiplier == 0.0
    assert "market quality blocked" in decision.reason


def test_market_quality_reduces_risk_instead_of_blocking_mid_liquidity_market():
    decision = MarketQualityGate().assess(coin(volume_24h=12_500_000.0, spread_bps=8.0))

    assert decision.allowed is True
    assert decision.risk_multiplier == 0.5
    assert "quote volume" in decision.reason


def test_market_quality_keeps_hard_floor_for_thin_market():
    decision = MarketQualityGate().assess(coin(volume_24h=2_000_000.0, spread_bps=8.0))

    assert decision.allowed is False
    assert "hard limit" in decision.reason
