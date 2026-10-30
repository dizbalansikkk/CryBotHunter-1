"""Features are joined by availability time, never backfilled from the future."""
from datetime import datetime, timezone
import math
import numpy as np
import pandas as pd
from sqlalchemy import select
from app.models.entities import MarketFeatureSnapshot

MICRO_FEATURES = ["order_book_imbalance", "funding_rate", "open_interest_change",
                  "liquidation_notional", "liquidation_long_distance", "liquidation_short_distance"]


def finite(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def record_market_features(db, coin, microstructure):
    derivatives = (coin.market_context or {}).get("derivatives", {})
    values = {
        "order_book_imbalance": finite(microstructure.get("order_book_imbalance")) if microstructure.get("order_book_available") else None,
        "funding_rate": finite(coin.funding_rate) if derivatives.get("funding_rate_available") else None,
        "open_interest_change": finite(derivatives.get("open_interest_change_percent")),
        "liquidation_notional": finite(derivatives.get("liquidation_notional")),
        # These must come from an observed liquidation-zone provider. Do not
        # fabricate maps from OHLC or call leverage-based guesses actual zones.
        "liquidation_long_distance": finite(derivatives.get("liquidation_long_distance")),
        "liquidation_short_distance": finite(derivatives.get("liquidation_short_distance")),
    }
    db.add(MarketFeatureSnapshot(symbol=coin.symbol, available_at=datetime.now(timezone.utc), values=values))


def join_market_features(frame, snapshots):
    result = frame.copy()
    result["decision_at"] = pd.to_datetime(result["timestamp"], utc=True) + pd.Timedelta(hours=1)
    if snapshots:
        right = pd.DataFrame([{"available_at": s.available_at, **s.values} for s in snapshots])
        right["available_at"] = pd.to_datetime(right["available_at"], utc=True)
        result = pd.merge_asof(result.sort_values("decision_at"), right.sort_values("available_at"),
            left_on="decision_at", right_on="available_at", direction="backward", tolerance=pd.Timedelta(hours=1))
    for name in MICRO_FEATURES:
        raw = pd.to_numeric(result[name], errors="coerce") if name in result else pd.Series(np.nan, index=result.index)
        raw = raw.replace([np.inf, -np.inf], np.nan)
        result[name + "_available"] = raw.notna().astype(float)
        if name == "funding_rate":
            raw = raw * 1000
        elif name == "liquidation_notional":
            raw = np.log1p(raw.clip(lower=0)) / 10
        elif name.endswith("distance"):
            raw = raw * 10
        result[name] = raw.fillna(0).clip(-5, 5)
    return result


async def load_market_features(db, symbol, frame):
    snapshots = list((await db.execute(select(MarketFeatureSnapshot).where(
        MarketFeatureSnapshot.symbol == symbol,
        MarketFeatureSnapshot.available_at >= pd.Timestamp(frame["timestamp"].iloc[0]).to_pydatetime(),
        MarketFeatureSnapshot.available_at <= (pd.Timestamp(frame["timestamp"].iloc[-1]) + pd.Timedelta(hours=1)).to_pydatetime(),
    ).order_by(MarketFeatureSnapshot.available_at))).scalars().all())
    # Drop placeholders built by build_feature_frame before joining real rows.
    frame = frame.drop(columns=[c for c in frame if c in MICRO_FEATURES or c.endswith("_available") or c == "decision_at"])
    return join_market_features(frame, snapshots)


async def collect_hourly_features(db, coins, exchange, microstructure):
    now = datetime.now(timezone.utc)
    hour = now.replace(minute=0, second=0, microsecond=0)
    for coin in coins:
        existing = (await db.execute(select(MarketFeatureSnapshot.id).where(
            MarketFeatureSnapshot.symbol == coin.symbol, MarketFeatureSnapshot.available_at >= hour).limit(1))).scalar_one_or_none()
        if existing is not None:
            continue
        try:
            book = await exchange.fetch_order_book(coin.symbol, limit=20)
            metrics = microstructure.order_book_metrics(book)
        except Exception:
            metrics = {"order_book_available": False}
        record_market_features(db, coin, metrics)
