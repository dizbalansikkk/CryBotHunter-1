from app.schemas.dto import MarketCoin
from app.services.strategy import StrategyCore


def coin(**overrides):
    data = {
        "symbol": "BTC/USDT",
        "price": 110,
        "volume_24h": 2_000_000_000,
        "volume_average_24h": 1_500_000_000,
        "price_change_percent": 4,
        "atr": 3,
        "rsi": 62,
        "ema20": 106,
        "ema50": 100,
        "ema200": 90,
        "macd": 10,
        "funding_rate": 0.01,
        "open_interest": 1_500_000_000,
        "rating": 88,
    }
    data.update(overrides)
    return MarketCoin(**data)


def test_strategy_returns_buy_when_long_rules_match():
    signal = StrategyCore().evaluate(coin())
    assert signal.signal == "BUY"
    assert signal.score >= 88


def test_strategy_returns_sell_when_short_rules_match():
    signal = StrategyCore().evaluate(coin(price=82, rsi=35, ema20=85, ema50=90, ema200=110, macd=-10, rating=90))
    assert signal.signal == "SELL"
    assert signal.score >= 90


def test_strategy_waits_when_rating_is_low():
    signal = StrategyCore().evaluate(coin(rating=40))
    assert signal.signal == "WAIT"


def test_strategy_blocks_overheated_long_entry():
    signal = StrategyCore().evaluate(coin(rsi=82))
    assert signal.signal == "WAIT"
    assert any("long missing" in reason for reason in signal.reasons)


def test_strategy_blocks_extreme_volatility():
    signal = StrategyCore().evaluate(coin(atr=12))
    assert signal.signal == "WAIT"
    assert "volatility too high" in signal.reasons[0]


def test_strategy_blocks_entry_without_real_volume_confirmation():
    signal = StrategyCore().evaluate(coin(volume_average_24h=0))

    assert signal.signal == "WAIT"
    assert any("rolling volume history unavailable" in reason for reason in signal.reasons)


def test_strategy_blocks_entry_that_chases_an_extended_move():
    signal = StrategyCore().evaluate(coin(price=112, ema20=106, atr=3))

    assert signal.signal == "WAIT"
    assert "too extended" in signal.reasons[0]
