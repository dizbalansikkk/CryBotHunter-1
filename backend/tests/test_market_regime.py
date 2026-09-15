from app.schemas.dto import MarketCoin
from app.services.market_regime import MarketRegimeDetector
from app.services.strategy import StrategyCore


def row(**overrides):
    data = {
        "symbol": "BTC/USDT",
        "price": 110,
        "volume_24h": 2_000_000_000,
        "price_change_percent": 4,
        "atr": 3,
        "rsi": 62,
        "ema20": 105,
        "ema50": 100,
        "ema200": 90,
        "macd": 10,
        "funding_rate": 0.01,
        "open_interest": 1_500_000_000,
        "rating": 88,
    }
    data.update(overrides)
    return data


def coin(**overrides):
    return MarketCoin(**row(**overrides))


def test_regime_detector_finds_trending_up():
    regime = MarketRegimeDetector().detect(row())
    assert regime.name == "TRENDING_UP"
    assert regime.score >= 70


def test_regime_detector_does_not_treat_missing_spot_open_interest_as_illiquid():
    regime = MarketRegimeDetector().detect(row(open_interest=0))

    assert regime.name == "TRENDING_UP"


def test_regime_detector_uses_hard_floor_not_soft_liquidity_target():
    assert MarketRegimeDetector().detect(row(volume_24h=20_000_000, open_interest=0)).name == "TRENDING_UP"
    assert MarketRegimeDetector().detect(row(volume_24h=2_000_000, open_interest=0)).name == "LOW_LIQUIDITY"


def test_regime_detector_blocks_high_volatility():
    regime = MarketRegimeDetector().detect(row(atr=12))
    assert regime.name == "HIGH_VOLATILITY"


def test_strategy_waits_in_blocked_regime():
    signal = StrategyCore().evaluate(coin(regime="HIGH_VOLATILITY", regime_score=25))
    assert signal.signal == "WAIT"
    assert "blocked by market regime" in signal.reasons[0]
