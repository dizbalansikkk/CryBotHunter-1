from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import pstdev
from time import perf_counter
from typing import Any

from app.core.config import get_settings
from app.schemas.dto import MarketCoin
from app.services.exchange import ExchangeClient


@dataclass(frozen=True)
class EntryGateAssessment:
    allowed: bool
    reason: str
    risk_multiplier: float
    consensus: float
    snapshot: dict[str, Any]


class MicrostructureService:
    def __init__(self, exchange: ExchangeClient) -> None:
        self.exchange = exchange
        self.settings = get_settings()

    async def capture(self, symbol: str) -> dict[str, Any]:
        if not self.settings.entry_microstructure_enabled:
            return self.unavailable("microstructure disabled")
        started = perf_counter()
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    self.exchange.fetch_order_book(symbol, self.settings.entry_microstructure_depth),
                    self.exchange.fetch_trades(symbol, self.settings.entry_microstructure_trade_limit),
                    self.exchange.fetch_ohlcv(symbol, timeframe="1m", limit=31),
                    return_exceptions=True,
                ),
                timeout=max(float(self.settings.entry_microstructure_timeout_seconds), 1.0),
            )
        except TimeoutError:
            return self.unavailable("microstructure timeout", started)

        order_book, trades, candles = results
        order_metrics = self.order_book_metrics(order_book if isinstance(order_book, dict) else {})
        tape_metrics = self.tape_metrics(trades if isinstance(trades, list) else [])
        momentum_metrics = self.momentum_metrics(candles if isinstance(candles, list) else [])
        available_sources = sum(
            (
                bool(order_metrics.get("order_book_available")),
                bool(tape_metrics.get("tape_available")),
                bool(momentum_metrics.get("momentum_available")),
            )
        )
        errors = [type(item).__name__ for item in results if isinstance(item, Exception)]
        return {
            "status": "READY" if available_sources >= 2 else "PARTIAL" if available_sources else "UNAVAILABLE",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "latency_ms": round((perf_counter() - started) * 1000, 2),
            "available_sources": available_sources,
            "errors": errors,
            **order_metrics,
            **tape_metrics,
            **momentum_metrics,
        }

    def unavailable(self, reason: str, started: float | None = None) -> dict[str, Any]:
        return {
            "status": "UNAVAILABLE",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "latency_ms": round((perf_counter() - started) * 1000, 2) if started else 0.0,
            "available_sources": 0,
            "reason": reason,
        }

    def order_book_metrics(self, payload: dict[str, Any]) -> dict[str, Any]:
        bids = self._levels(payload.get("bids"))
        asks = self._levels(payload.get("asks"))
        if not bids or not asks:
            return {"order_book_available": False}
        bid_depth = sum(price * amount for price, amount in bids)
        ask_depth = sum(price * amount for price, amount in asks)
        total_depth = bid_depth + ask_depth
        midpoint = (bids[0][0] + asks[0][0]) / 2
        spread_bps = (asks[0][0] - bids[0][0]) / midpoint * 10_000 if midpoint > 0 else 0.0
        bid_wall_ratio = self._wall_ratio(bids)
        ask_wall_ratio = self._wall_ratio(asks)
        iceberg_proxy = "BID" if bid_wall_ratio >= 4 else "ASK" if ask_wall_ratio >= 4 else None
        return {
            "order_book_available": True,
            "spread_bps": round(max(spread_bps, 0.0), 4),
            "bid_depth_quote": round(bid_depth, 2),
            "ask_depth_quote": round(ask_depth, 2),
            "order_book_imbalance": round((bid_depth - ask_depth) / total_depth, 4) if total_depth else 0.0,
            "bid_wall_ratio": round(bid_wall_ratio, 3),
            "ask_wall_ratio": round(ask_wall_ratio, 3),
            # A single snapshot cannot prove a hidden order. This is deliberately
            # exposed as a proxy so dashboards never present it as certainty.
            "iceberg_proxy": iceberg_proxy,
        }

    def tape_metrics(self, trades: list[dict[str, Any]]) -> dict[str, Any]:
        buy_volume = 0.0
        sell_volume = 0.0
        priced = 0
        for trade in trades:
            amount = self._number(trade.get("amount"))
            price = self._number(trade.get("price"))
            if amount <= 0:
                continue
            notional = amount * price if price > 0 else amount
            side = str(trade.get("side") or "").lower()
            if side == "buy":
                buy_volume += notional
                priced += 1
            elif side == "sell":
                sell_volume += notional
                priced += 1
        total = buy_volume + sell_volume
        if not priced or total <= 0:
            return {"tape_available": False}
        return {
            "tape_available": True,
            "tape_trades": priced,
            "buy_volume_quote": round(buy_volume, 2),
            "sell_volume_quote": round(sell_volume, 2),
            "trade_flow_imbalance": round((buy_volume - sell_volume) / total, 4),
        }

    def momentum_metrics(self, candles: list[list[float]]) -> dict[str, Any]:
        closes = [self._number(row[4]) for row in candles if isinstance(row, (list, tuple)) and len(row) >= 5]
        closes = [value for value in closes if value > 0]
        if len(closes) < 3:
            return {"momentum_available": False}
        returns = [math.log(current / previous) for previous, current in zip(closes, closes[1:]) if previous > 0]
        change_10 = self._change(closes[-11:] if len(closes) >= 11 else closes)
        change_30 = self._change(closes)
        return {
            "momentum_available": True,
            "price_change_10m_percent": round(change_10, 4),
            "price_change_30m_percent": round(change_30, 4),
            "realized_volatility_1m_percent": round(pstdev(returns) * 100 if len(returns) > 1 else 0.0, 4),
        }

    def _levels(self, value: Any) -> list[tuple[float, float]]:
        levels: list[tuple[float, float]] = []
        if not isinstance(value, list):
            return levels
        for row in value:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            price, amount = self._number(row[0]), self._number(row[1])
            if price > 0 and amount > 0:
                levels.append((price, amount))
        return levels

    def _wall_ratio(self, levels: list[tuple[float, float]]) -> float:
        notionals = [price * amount for price, amount in levels]
        average = sum(notionals) / len(notionals) if notionals else 0.0
        return max(notionals) / average if average > 0 else 0.0

    def _change(self, closes: list[float]) -> float:
        return (closes[-1] / closes[0] - 1) * 100 if len(closes) >= 2 and closes[0] > 0 else 0.0

    def _number(self, value: Any) -> float:
        try:
            number = float(value)
            return number if math.isfinite(number) else 0.0
        except (TypeError, ValueError):
            return 0.0


class EntryGatekeeper:
    def __init__(self) -> None:
        self.settings = get_settings()

    def assess(self, coin: MarketCoin, signal: str, snapshot: dict[str, Any]) -> EntryGateAssessment:
        direction = 1.0 if signal == "BUY" else -1.0
        if signal not in {"BUY", "SELL"}:
            return EntryGateAssessment(False, "gatekeeper requires directional signal", 0.0, 0.0, snapshot)
        if coin.regime in {"LOW_LIQUIDITY", "HIGH_VOLATILITY"}:
            return EntryGateAssessment(False, f"macro gate blocked regime={coin.regime}", 0.0, 0.0, snapshot)
        if (signal == "BUY" and coin.regime == "TRENDING_DOWN") or (
            signal == "SELL" and coin.regime == "TRENDING_UP"
        ):
            return EntryGateAssessment(False, f"macro gate blocked {signal} against {coin.regime}", 0.0, 0.0, snapshot)

        if snapshot.get("status") == "UNAVAILABLE":
            multiplier = max(min(float(self.settings.entry_microstructure_fail_open_risk_multiplier), 1.0), 0.0)
            return EntryGateAssessment(
                True,
                f"macro gate passed; micro data unavailable, risk reduced to {multiplier:.2f}x",
                multiplier,
                0.0,
                snapshot,
            )
        spread = float(snapshot.get("spread_bps") or 0.0)
        if spread > float(self.settings.entry_microstructure_max_spread_bps):
            return EntryGateAssessment(
                False,
                f"micro gate blocked spread {spread:.2f} bps",
                0.0,
                0.0,
                snapshot,
            )

        supports: list[float] = []
        labels: list[str] = []
        if snapshot.get("order_book_available"):
            supports.append(direction * float(snapshot.get("order_book_imbalance") or 0.0))
            labels.append("book")
        if snapshot.get("tape_available"):
            supports.append(direction * float(snapshot.get("trade_flow_imbalance") or 0.0))
            labels.append("tape")
        if snapshot.get("momentum_available"):
            momentum = direction * float(snapshot.get("price_change_10m_percent") or 0.0)
            supports.append(0.08 if momentum > 0 else -0.08 if momentum < 0 else 0.0)
            labels.append("momentum")
        if not supports:
            multiplier = max(min(float(self.settings.entry_microstructure_fail_open_risk_multiplier), 1.0), 0.0)
            return EntryGateAssessment(True, "macro gate passed; no usable micro votes", multiplier, 0.0, snapshot)

        agreeing = sum(value >= 0.05 for value in supports)
        opposing = sum(value <= -0.15 for value in supports)
        consensus = agreeing / len(supports)
        detail = ", ".join(f"{name}={value:+.2f}" for name, value in zip(labels, supports))
        if opposing >= 2:
            return EntryGateAssessment(False, f"micro gate blocked strong opposing flow ({detail})", 0.0, consensus, snapshot)
        if consensus < float(self.settings.entry_microstructure_min_consensus):
            multiplier = max(min(float(self.settings.entry_microstructure_neutral_risk_multiplier), 1.0), 0.0)
            return EntryGateAssessment(
                True,
                f"micro gate neutral {consensus:.0%}; risk reduced to {multiplier:.2f}x ({detail})",
                multiplier,
                consensus,
                snapshot,
            )
        return EntryGateAssessment(True, f"macro + micro gate passed {consensus:.0%} ({detail})", 1.0, consensus, snapshot)
