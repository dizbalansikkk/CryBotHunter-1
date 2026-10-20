"""Offline same-entry comparison; bar-close trailing, adverse-first ambiguity."""
import math


def replay_exit(*, bars, entry_time_ms, end_time_ms, entry, volume, side,
                initial_risk, target, entry_fee, exit_fee_rate, slippage_bps,
                trailing_percent=None, activation_r=1.0, interval_ms=300_000):
    values = (entry, volume, initial_risk, target, entry_fee, exit_fee_rate, slippage_bps)
    if not all(math.isfinite(v) for v in values) or min(entry, volume, initial_risk, target) <= 0:
        raise ValueError("Invalid replay parameters")
    direction = 1 if side == "LONG" else -1 if side == "SHORT" else 0
    if not direction or min(entry_fee, exit_fee_rate, slippage_bps) < 0:
        raise ValueError("Invalid replay side or costs")
    # The entry candle has unknown pre-entry extrema; omit it explicitly.
    first = math.ceil(entry_time_ms / interval_ms) * interval_ms
    selected = [bar for bar in bars if first <= bar[0] and bar[0] + interval_ms <= end_time_ms]
    if not selected or selected[0][0] != first:
        return {"status": "INSUFFICIENT_DATA"}
    stop = entry - direction * initial_risk
    favorable = entry
    ambiguous = 0
    previous = first - interval_ms
    for timestamp, opening, high, low, closing in selected:
        if timestamp != previous + interval_ms:
            return {"status": "DATA_GAP"}
        previous = timestamp
        stop_hit = low <= stop if direction == 1 else high >= stop
        target_hit = high >= target if direction == 1 else low <= target
        ambiguous += int(stop_hit and target_hit)
        if stop_hit:
            price = min(opening, stop) if direction == 1 else max(opening, stop)
            reason = "TRAILING_STOP" if direction * (stop - entry) > -initial_risk + 1e-9 else "STOP_LOSS"
        elif target_hit:
            price, reason = target, "TAKE_PROFIT"
        else:
            if trailing_percent is not None:
                favorable = max(favorable, high) if direction == 1 else min(favorable, low)
                if direction * (favorable - entry) >= activation_r * initial_risk:
                    candidate = favorable * (1 - direction * trailing_percent / 100)
                    stop = max(stop, candidate) if direction == 1 else min(stop, candidate)
            continue
        fill = price * (1 - direction * slippage_bps / 10_000)
        net = direction * (fill - entry) * volume - entry_fee - fill * volume * exit_fee_rate
        return {"status": "CLOSED", "reason": reason, "net_pnl": net,
                "exit_bar_ms": timestamp, "fill": fill, "ambiguous_bars": ambiguous}
    return {"status": "OPEN_AT_HORIZON", "last_price": selected[-1][4],
            "mark_pnl_before_exit_costs": direction * (selected[-1][4] - entry) * volume - entry_fee}
