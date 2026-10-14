import math

import pandas as pd
import pytest

from app.services.context_manager import ContextManager
from app.services.market_scanner import MarketScanner


@pytest.mark.parametrize("mode", ["mixed", "up", "down", "flat"])
def test_backtest_indicators_match_production_context_at_every_prefix(mode):
    values = {
        "mixed": [100 + i * .02 + math.sin(i / 3) * 3 for i in range(260)],
        "up": [100 + i for i in range(260)],
        "down": [400 - i for i in range(260)],
        "flat": [100] * 260,
    }[mode]
    frame = pd.DataFrame({"close": values, "high": [v + 1 for v in values],
                          "low": [v - 1 for v in values]})
    indicators = MarketScanner().calculate_indicators(frame.copy())
    context = ContextManager()
    for end in range(200, len(frame) + 1):
        production = context._calculate_market_context(frame.iloc[:end])
        historical = indicators.iloc[end - 1]
        assert historical.rsi == pytest.approx(production.rsi, abs=1e-7)
        assert historical.atr == pytest.approx(production.atr, abs=1e-7)
