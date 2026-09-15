import asyncio
import logging
import math
from datetime import datetime, timezone
from hashlib import sha256
from random import Random
from typing import Any

import pandas as pd

from app.core.config import get_settings
from app.schemas.dto import MarketCoin
from app.services.context_manager import ContextManager
from app.services.exchange import ExchangeClient
from app.services.market_regime import MarketRegimeDetector


logger = logging.getLogger(__name__)


class MarketScanner:
    def __init__(self, exchange: ExchangeClient | None = None) -> None:
        self.regime_detector = MarketRegimeDetector()
        self.exchange = exchange or ExchangeClient()
        self.context_manager = ContextManager()

    async def scan(self, symbols: list[str] | None = None) -> list[MarketCoin]:
        settings = get_settings()
        requested_symbols = settings.market_scan_symbols if symbols is None else symbols
        is_excluded = getattr(settings, "is_symbol_excluded", lambda _symbol: False)
        symbols = [
            symbol
            for symbol in dict.fromkeys(str(item).strip().upper() for item in requested_symbols if item)
            if not is_excluded(symbol)
        ]
        if not symbols:
            return []
        if settings.uses_live_market_data:
            return await self._scan_ccxt(symbols)
        rows = [self._synthetic_row(symbol) for symbol in symbols]
        return [self._coin_from_row(row) for row in rows]

    async def _scan_ccxt(self, symbols: list[str]) -> list[MarketCoin]:
        tickers = await self.exchange.fetch_tickers(symbols)
        semaphore = asyncio.Semaphore(max(int(getattr(get_settings(), "market_scan_concurrency", 3)), 1))

        async def scan_symbol(symbol: str) -> MarketCoin | None:
            async with semaphore:
                try:
                    return await self._scan_ccxt_symbol(symbol, tickers.get(symbol, {}))
                except Exception as exc:
                    logger.warning("Market scan skipped symbol=%s error=%s", symbol, type(exc).__name__)
                    return None

        results = await asyncio.gather(*(scan_symbol(symbol) for symbol in symbols))
        return [coin for coin in results if coin is not None]

    async def _scan_ccxt_symbol(self, symbol: str, ticker: dict[str, Any]) -> MarketCoin | None:
        candles = await self.exchange.fetch_ohlcv(symbol, timeframe="1h", limit=250)
        if len(candles) < 200:
            return None
        frame = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
        market_context, indicator_frame = await asyncio.gather(
            self.context_manager.get_market_context(frame),
            asyncio.to_thread(self.calculate_indicators, frame.copy(deep=True)),
        )
        indicators = indicator_frame.iloc[-1]
        bid = self._optional_float(ticker.get("bid"))
        ask = self._optional_float(ticker.get("ask"))
        row = {
            "symbol": symbol,
            "price": float(ticker.get("last") or indicators["close"]),
            "volume_24h": float(ticker.get("quoteVolume") or frame.tail(24)["volume"].sum()),
            "price_change_percent": float(ticker.get("percentage") or 0),
            "atr": market_context.atr,
            "rsi": market_context.rsi,
            "sma": market_context.sma,
            "market_context": market_context.as_dict(),
            "ema20": float(indicators["ema20"]),
            "ema50": float(indicators["ema50"]),
            "ema200": float(indicators["ema200"]),
            "macd": float(indicators["macd"]),
            "funding_rate": 0.0,
            "open_interest": 0.0,
            "bid": bid,
            "ask": ask,
            "spread_bps": self._spread_bps(bid, ask),
        }
        return self._coin_from_row(row)

    def _coin_from_row(self, row: dict[str, Any]) -> MarketCoin:
        if not row.get("market_context"):
            market_context = self.context_manager.from_values(
                close=float(row["price"]),
                rsi=float(row["rsi"]),
                atr=float(row["atr"]),
                sma=float(row.get("sma") or row["ema50"]),
            )
            row = {**row, "sma": market_context.sma, "market_context": market_context.as_dict()}
        regime = self.regime_detector.detect(row)
        return MarketCoin(
            **row,
            rating=self.rate_coin(row),
            regime=regime.name,
            regime_score=regime.score,
            regime_reason=regime.reason,
        )

    def rate_coin(self, row: dict[str, Any]) -> int:
        price = max(float(row["price"]), 1e-12)
        quote_volume = max(float(row["volume_24h"]), 0.0)
        open_interest = max(float(row.get("open_interest") or 0), 0.0)
        spread_bps = max(float(row.get("spread_bps") or 0), 0.0)
        atr_percent = max(float(row["atr"]), 0.0) / price * 100
        trend_separation = abs(float(row["ema50"]) - float(row["ema200"])) / price * 100

        # Spot tickers do not expose futures open interest. Missing optional
        # derivatives data must not make a high-quality spot setup impossible.
        settings = get_settings()
        hard_volume = max(float(settings.market_quality_hard_min_quote_volume), 1.0)
        target_volume = max(float(settings.market_quality_min_quote_volume), hard_volume * 1.01)
        if quote_volume < hard_volume:
            volume_score = 0.0
        else:
            volume_progress = math.log10(max(quote_volume / hard_volume, 1.0)) / math.log10(
                target_volume / hard_volume
            )
            volume_score = 5 + min(max(volume_progress, 0.0), 1.0) * 15
        trend_score = min(10 + trend_separation * 7.5, 25)
        volatility_score = max(0, 20 - abs(atr_percent - 2.5) * 5)
        momentum_score = min(abs(float(row["price_change_percent"])) * 2.5, 15)
        depth_score = min(open_interest / 1_000_000_000 * 5, 5) if open_interest > 0 else 5
        spread_score = 15 if spread_bps <= 0 else max(0, 15 - spread_bps / 2)
        score = round(volume_score + trend_score + volatility_score + momentum_score + depth_score + spread_score)
        return int(max(0, min(100, score)))

    def calculate_indicators(self, candles: pd.DataFrame) -> pd.DataFrame:
        close = candles["close"]
        candles["ema20"] = close.ewm(span=20).mean()
        candles["ema50"] = close.ewm(span=50).mean()
        candles["ema200"] = close.ewm(span=200).mean()
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = -delta.clip(upper=0).rolling(14).mean()
        rs = gain / loss.replace(0, pd.NA)
        candles["rsi"] = 100 - (100 / (1 + rs))
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        candles["macd"] = ema12 - ema26
        high_low = candles["high"] - candles["low"]
        high_close = (candles["high"] - close.shift()).abs()
        low_close = (candles["low"] - close.shift()).abs()
        candles["atr"] = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1).rolling(14).mean()
        return candles

    def _synthetic_row(self, symbol: str) -> dict[str, float | str]:
        minute_bucket = int(datetime.now(timezone.utc).timestamp() // 60)
        digest = sha256(f"{symbol}:{minute_bucket}".encode()).hexdigest()
        rng = Random(int(digest[:12], 16))
        base = {"BTC/USDT": 68000, "ETH/USDT": 3600, "SOL/USDT": 155, "BNB/USDT": 620}.get(symbol, 2.5)
        shift = rng.uniform(-0.04, 0.06)
        price = base * (1 + shift)
        spread_bps = rng.uniform(1.5, 18.0)
        half_spread = price * spread_bps / 20_000
        ema200 = price * rng.uniform(0.94, 1.03)
        ema50 = price * rng.uniform(0.97, 1.04)
        return {
            "symbol": symbol,
            "price": round(price, 4),
            "volume_24h": rng.uniform(300_000_000, 3_000_000_000),
            "price_change_percent": shift * 100,
            "atr": price * rng.uniform(0.01, 0.05),
            "rsi": rng.uniform(28, 74),
            "ema20": price * rng.uniform(0.98, 1.02),
            "ema50": ema50,
            "ema200": ema200,
            "macd": rng.uniform(-20, 20),
            "funding_rate": rng.uniform(-0.02, 0.02),
            "open_interest": rng.uniform(200_000_000, 2_000_000_000),
            "bid": round(price - half_spread, 8),
            "ask": round(price + half_spread, 8),
            "spread_bps": round(spread_bps, 2),
        }

    def _spread_bps(self, bid: float | None, ask: float | None) -> float:
        if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
            return 0.0
        midpoint = (bid + ask) / 2
        return round((ask - bid) / midpoint * 10_000, 2)

    def _optional_float(self, value: object) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
