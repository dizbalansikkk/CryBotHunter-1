"""Rolling, after-cost realized outcomes; no directional guessing rewards."""
import math
from statistics import mean, pstdev


def rolling_trust(outcomes, window=50):
    values = [float(v) for v in outcomes[-window:] if math.isfinite(float(v))]
    n = len(values)
    if not n:
        return {"trust": .25, "samples": 0, "sharpe": None, "win_rate": None}
    average = mean(values)
    sharpe = average / max(pstdev(values), .05)  # Per-trade, not annualized.
    if (n >= 5 and average <= 0) or sum(values) <= -3:
        trust = 0.0
    elif n < 5:
        trust = .25
    else:
        trust = max(0, min(1, sharpe)) * n / (n + 10)
    return {"trust": round(trust, 6), "samples": n, "sharpe": round(sharpe, 6),
            "win_rate": sum(v > 0 for v in values) / n}
