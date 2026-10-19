"""Pre-entry scenarios, not predicted returns or booked funding payments."""
import math


def estimate_costs(*, price, stop, take, volume, fee_rate, spread_bps, slippage_bps, funding_rate=0.0):
    values = (price, stop, take, volume, fee_rate, spread_bps, slippage_bps, funding_rate)
    if not all(math.isfinite(float(v)) for v in values) or min(price, stop, take, volume) <= 0:
        return {"available": False, "reason": "invalid cost inputs"}
    # Both legs are market orders. Use target/stop notionals for exit fees.
    entry_fee = price * volume * max(fee_rate, 0)
    target_fee = take * volume * max(fee_rate, 0)
    stop_fee = stop * volume * max(fee_rate, 0)
    spread = price * volume * max(spread_bps, 0) / 10000
    slippage = (price + take) * volume * max(slippage_bps, 0) / 10000
    stop_slippage = (price + stop) * volume * max(slippage_bps, 0) / 10000
    costs = entry_fee + target_fee + spread + slippage
    net_target = abs(take - price) * volume - costs
    net_stop_loss = abs(price - stop) * volume + entry_fee + stop_fee + spread + stop_slippage
    funding_stress = price * volume * abs(funding_rate)
    return {"available": True, "estimate_only": True,
        "fee_source": "configured_taker_rate", "entry_fee": entry_fee,
        "target_exit_fee": target_fee, "round_trip_spread": spread,
        "round_trip_slippage": slippage, "total_cost_before_funding": costs,
        "net_target_before_funding": net_target, "loss_at_stop_with_costs": net_stop_loss,
        "net_reward_risk": net_target / net_stop_loss if net_stop_loss else None,
        "one_adverse_funding_payment": funding_stress,
        "net_target_funding_stress": net_target - funding_stress,
        "funding_assumption": "one adverse payment stress scenario; holding time and next funding unknown",
        "target_assumption": "entire volume exits at final target; scale-outs change realized outcome"}
