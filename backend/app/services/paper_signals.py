from app.schemas.dto import MarketCoin, StrategySignal


def paper_exploration_signal(
    settings,
    coin: MarketCoin,
    signal: StrategySignal,
) -> tuple[StrategySignal, bool]:
    if (
        not settings.paper_trading
        or not settings.paper_exploration_enabled
        or signal.signal != "WAIT"
        or signal.score < settings.paper_exploration_min_score
    ):
        return signal, False

    hard_blocks = ("blocked by market regime", "volatility too low", "volatility too high")
    if any(marker in reason for marker in hard_blocks for reason in signal.reasons):
        return signal, False

    bullish_votes, bearish_votes = paper_exploration_votes(coin)
    strongest_votes = max(bullish_votes, bearish_votes)
    vote_margin = abs(bullish_votes - bearish_votes)
    if (
        strongest_votes < max(int(settings.paper_exploration_min_directional_votes), 1)
        or vote_margin < max(int(settings.paper_exploration_min_vote_margin), 1)
    ):
        return signal, False
    direction = "BUY" if bullish_votes > bearish_votes else "SELL"
    reasons = [
        (
            "paper exploration from WAIT: "
            f"bullish_votes={bullish_votes}, bearish_votes={bearish_votes}, margin={vote_margin}"
        ),
        *signal.reasons[:3],
    ]
    return StrategySignal(symbol=signal.symbol, signal=direction, score=signal.score, reasons=reasons), True


def paper_exploration_votes(coin: MarketCoin) -> tuple[int, int]:
    bullish_votes = sum(
        (
            coin.regime in {"TRENDING_UP", "UNKNOWN"},
            coin.ema20 > coin.ema50,
            coin.ema50 > coin.ema200,
            coin.price > coin.ema20,
            coin.rsi >= 50,
            coin.macd > 0,
            coin.price_change_percent >= 0,
        )
    )
    bearish_votes = sum(
        (
            coin.regime in {"TRENDING_DOWN", "UNKNOWN"},
            coin.ema20 < coin.ema50,
            coin.ema50 < coin.ema200,
            coin.price < coin.ema20,
            coin.rsi < 50,
            coin.macd < 0,
            coin.price_change_percent < 0,
        )
    )
    return bullish_votes, bearish_votes

