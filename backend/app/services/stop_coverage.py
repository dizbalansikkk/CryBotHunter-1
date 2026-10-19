"""Assess a freshly fetched stop response; local intent alone is not coverage."""
import math
from datetime import datetime, timezone


def assess_stop_coverage(position, order, refreshed):
    result = {"status": "UNKNOWN", "reason": "fresh_exchange_confirmation_missing",
              "checked_at": datetime.now(timezone.utc).isoformat(),
              "required_volume": float(position.volume), "order_id": getattr(order, "id", None)}
    if order is None:
        return {**result, "status": "MISSING", "reason": "no_recorded_protective_stop"}
    if not refreshed:
        return result
    raw = (order.raw or {}).get("exchange_response") or {}
    if str(raw.get("status", "")).lower() not in {"open", "new"}:
        return {**result, "status": "INACTIVE", "reason": "stop_not_open"}
    try:
        amount = float(raw["amount"])
        filled = float(raw["filled"])
        trigger = float(raw.get("stopPrice") or raw.get("triggerPrice") or 0)
        if not all(math.isfinite(v) for v in (amount, filled, trigger)) or amount <= 0 or filled < 0 or filled > amount or trigger <= 0:
            return result
    except (KeyError, TypeError, ValueError):
        return result
    remaining = amount - filled
    result.update(covered_volume=remaining, confirmed_stop=trigger)
    if raw.get("reduceOnly") is not True:
        return {**result, "status": "UNKNOWN", "reason": "reduce_only_not_confirmed"}
    if raw.get("side") != ("sell" if position.side == "LONG" else "buy"):
        return {**result, "status": "MISMATCH", "reason": "wrong_exit_side"}
    if not math.isclose(remaining, float(position.volume), rel_tol=1e-6, abs_tol=1e-8):
        return {**result, "status": "MISMATCH", "reason": "stop_quantity_differs_from_position"}
    if not math.isclose(trigger, float(position.stop), rel_tol=1e-6, abs_tol=1e-8):
        return {**result, "status": "MISMATCH", "reason": "stop_trigger_differs_from_plan"}
    return {**result, "status": "COVERED", "reason": "fresh_stop_matches_position"}


def summarize_coverage(positions, now):
    counts = {"COVERED": 0, "ATTENTION": 0, "UNKNOWN": 0}
    for position in positions:
        coverage = (getattr(position, "entry_context", None) or {}).get("stop_coverage", {})
        try:
            checked = datetime.fromisoformat(coverage["checked_at"])
            age = (now - checked).total_seconds()
            fresh = 0 <= age <= 120
        except (KeyError, TypeError, ValueError):
            fresh = False
        status = coverage.get("status") if fresh else "UNKNOWN"
        counts["COVERED" if status == "COVERED" else "UNKNOWN" if status == "UNKNOWN" else "ATTENTION"] += 1
    return counts
