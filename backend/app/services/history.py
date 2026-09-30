from datetime import datetime, timedelta, timezone
from hashlib import sha256
from random import Random

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import Candle
from app.services.exchange import ExchangeClient


class HistoricalDataService:
    def __init__(self, exchange: ExchangeClient | None = None) -> None:
        self.exchange = exchange or ExchangeClient()

    async def ingest_many(self, db: AsyncSession, symbols: list[str], timeframes: list[str], limit: int = 500) -> dict[str, int]:
        inserted: dict[str, int] = {}
        for symbol in symbols:
            for timeframe in timeframes:
                key = f"{symbol}:{timeframe}"
                inserted[key] = await self.ingest(db, symbol=symbol, timeframe=timeframe, limit=limit)
        return inserted

    async def ingest(self, db: AsyncSession, symbol: str, timeframe: str = "1h", limit: int = 500) -> int:
        candles = await self._fetch(symbol, timeframe, limit)
        if not candles:
            return 0
        rows = [
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "timestamp": candle["timestamp"],
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
                "volume": candle["volume"],
                "source": candle["source"],
            }
            for candle in candles
        ]
        affected = 0
        for start in range(0, len(rows), 750):
            batch = rows[start : start + 750]
            statement = insert(Candle).values(batch)
            statement = statement.on_conflict_do_update(
                constraint="uq_candle_symbol_timeframe_timestamp",
                set_={
                    "open": statement.excluded.open,
                    "high": statement.excluded.high,
                    "low": statement.excluded.low,
                    "close": statement.excluded.close,
                    "volume": statement.excluded.volume,
                    "source": statement.excluded.source,
                },
            )
            result = await db.execute(statement)
            affected += int(result.rowcount or 0)
        await db.commit()
        return affected

    async def load(
        self,
        db: AsyncSession,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 500,
        source: str | None = None,
    ) -> list[Candle]:
        statement = select(Candle).where(Candle.symbol == symbol, Candle.timeframe == timeframe)
        if source:
            statement = statement.where(Candle.source == source)
        result = await db.execute(statement.order_by(Candle.timestamp.desc()).limit(limit))
        return list(reversed(result.scalars().all()))

    async def readiness(self, db: AsyncSession, symbols: list[str], timeframes: list[str], target: int = 100_000) -> list[dict]:
        rows: list[dict] = []
        for symbol in symbols:
            for timeframe in timeframes:
                result = await db.execute(
                    select(
                        func.count(Candle.id),
                        func.count(Candle.id).filter(Candle.source == "ccxt"),
                        func.count(Candle.id).filter(Candle.source == "synthetic"),
                        func.min(Candle.timestamp),
                        func.max(Candle.timestamp),
                    ).where(Candle.symbol == symbol, Candle.timeframe == timeframe)
                )
                count, real_count, synthetic_count, first_timestamp, last_timestamp = result.one()
                effective_count = int(real_count or 0) if get_settings().uses_live_market_data else int(synthetic_count or 0)
                coverage = round(min(effective_count / max(target, 1) * 100, 100), 2)
                rows.append(
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "candles": int(count or 0),
                        "real_candles": int(real_count or 0),
                        "synthetic_candles": int(synthetic_count or 0),
                        "target": target,
                        "coverage_percent": coverage,
                        "ready": effective_count >= target,
                        "first_timestamp": first_timestamp,
                        "last_timestamp": last_timestamp,
                    }
                )
        return rows

    async def _fetch(self, symbol: str, timeframe: str, limit: int) -> list[dict]:
        if get_settings().uses_live_market_data:
            raw = await self._fetch_ccxt_pages(symbol, timeframe, limit)
            return [
                {
                    "timestamp": datetime.fromtimestamp(item[0] / 1000, tz=timezone.utc),
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                    "source": "ccxt",
                }
                for item in raw
            ]
        return self._paper_candles(symbol, timeframe, limit)

    async def _fetch_ccxt_pages(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        target = max(1, limit)
        page_size = min(target, 1000)
        if target <= page_size:
            return await self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=page_size)

        interval_ms = self._timeframe_milliseconds(timeframe)
        since = int(datetime.now(timezone.utc).timestamp() * 1000) - interval_ms * (target + 5)
        candles: dict[int, list[float]] = {}
        while len(candles) < target:
            page = await self.exchange.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                limit=min(1000, target - len(candles)),
                since=since,
            )
            if not page:
                break
            for item in page:
                candles[int(item[0])] = item
            next_since = int(page[-1][0]) + interval_ms
            if next_since <= since or len(page) < min(1000, target - len(candles) + len(page)):
                break
            since = next_since
        return [candles[key] for key in sorted(candles)][-target:]

    def _timeframe_milliseconds(self, timeframe: str) -> int:
        units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
        try:
            return max(int(timeframe[:-1]), 1) * units[timeframe[-1].lower()]
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Unsupported candle timeframe: {timeframe}") from None

    def _paper_candles(self, symbol: str, timeframe: str, limit: int) -> list[dict]:
        minutes = {
            "1m": 1,
            "5m": 5,
            "15m": 15,
            "1h": 60,
            "4h": 240,
            "12h": 720,
            "1d": 1_440,
        }.get(timeframe, 60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        base = {"BTC/USDT": 68000, "ETH/USDT": 3600, "SOL/USDT": 155, "BNB/USDT": 620}.get(symbol, 2.5)
        candles: list[dict] = []
        price = base
        for index in range(limit):
            timestamp = now - timedelta(minutes=minutes * (limit - index))
            digest = sha256(f"{symbol}:{timeframe}:{timestamp.isoformat()}".encode()).hexdigest()
            rng = Random(int(digest[:12], 16))
            drift = rng.uniform(-0.012, 0.014)
            open_price = price
            close = max(open_price * (1 + drift), 0.0001)
            high = max(open_price, close) * (1 + rng.uniform(0, 0.006))
            low = min(open_price, close) * (1 - rng.uniform(0, 0.006))
            volume = rng.uniform(10_000, 200_000)
            candles.append(
                {
                    "timestamp": timestamp,
                    "open": round(open_price, 6),
                    "high": round(high, 6),
                    "low": round(low, 6),
                    "close": round(close, 6),
                    "volume": round(volume, 6),
                    "source": "synthetic",
                }
            )
            price = close
        return candles
