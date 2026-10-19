"""Read-only venue/local comparison. Never infer fills or modify venue inventory."""
import math
from sqlalchemy import select
from app.models.entities import Position


def compare_inventory(local, venue):
    expected, actual = {}, {}
    for row in local:
        key = (row.symbol.split(":")[0], row.side.upper())
        amount = float(row.volume)
        if not math.isfinite(amount) or amount < 0:
            return ["invalid_local_quantity"]
        expected[key] = expected.get(key, 0.0) + amount
    for row in venue:
        contracts = float(row.get("contracts") or 0)
        if not math.isfinite(contracts) or contracts < 0:
            return ["invalid_venue_quantity"]
        if contracts == 0:
            continue
        size = float(row.get("contractSize") or 0)
        side = str(row.get("side") or "").upper()
        symbol = str(row.get("symbol") or "").split(":")[0]
        if not math.isfinite(size) or size <= 0 or side not in {"LONG", "SHORT"} or not symbol:
            return ["incomplete_venue_position"]
        key = (symbol, side)
        actual[key] = actual.get(key, 0.0) + contracts * size
    return [f"{symbol}:{side}:local={expected.get((symbol, side), 0):.8f}:venue={actual.get((symbol, side), 0):.8f}"
            for symbol, side in sorted(expected.keys() | actual.keys())
            if not math.isclose(expected.get((symbol, side), 0), actual.get((symbol, side), 0), rel_tol=1e-6, abs_tol=1e-8)]


async def check_inventory(db, exchange):
    local = (await db.execute(select(Position).where(Position.status == "OPEN"))).scalars().all()
    try:
        venue = await exchange.fetch_live_positions()
        if not isinstance(venue, list):
            return ["invalid_venue_response"]
        return compare_inventory(local, venue)
    except Exception as exc:
        return ["inventory_check_unavailable:" + type(exc).__name__]
