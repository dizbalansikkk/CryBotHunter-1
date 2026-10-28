"""Fixed user-approved experiment. No agent-adjustable financial parameters."""
import math

MAX_LOSS = 0.50
MIN_PROFIT = 0.50
TARGET_PROFIT = 0.60


def projection(side, entry, volume, stop, take, fee_rate, slippage_bps, entry_fee=None):
    values = (entry, volume, stop, take, fee_rate, slippage_bps)
    if not all(math.isfinite(float(v)) for v in values):
        raise ValueError("Nonfinite experiment inputs")
    if min(entry, volume, stop, take) <= 0 or not 0 <= fee_rate < 1 or not 0 <= slippage_bps < 10000:
        raise ValueError("Invalid experiment inputs")
    direction = 1 if side == "LONG" else -1
    if direction * (take - entry) <= 0 or direction * (entry - stop) <= 0:
        raise ValueError("Invalid SL/TP geometry")
    paid = entry * volume * fee_rate if entry_fee is None else entry_fee
    def net(level):
        fill = level * (1 - direction * slippage_bps / 10000)
        return direction * (fill - entry) * volume - paid - fill * volume * fee_rate
    return {"loss": max(-net(stop), 0), "profit": net(take)}


def compliant(plan):
    return plan["loss"] <= MAX_LOSS + 1e-10 and plan["profit"] >= TARGET_PROFIT


def active(position):
    return bool((position.entry_context or {}).get("exit_experiment"))
