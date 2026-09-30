from datetime import datetime, timedelta, timezone

import pytest

from app.api.routes.positions import _aggregate_hourly_candles, _live_chart_candles, _merge_chart_candles, position_chart
from app.models.entities import Candle, Position


class _Scalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return list(self.values)


class _Result:
    def __init__(self, *, position=None, candles=None):
        self.position = position
        self.candles = candles or []

    def scalar_one_or_none(self):
        return self.position

    def scalars(self):
        return _Scalars(self.candles)


class _Db:
    def __init__(self, position, candles):
        self.position = position
        self.candles = candles
        self.calls = 0

    async def execute(self, _statement):
        self.calls += 1
        return _Result(position=self.position) if self.calls == 1 else _Result(candles=self.candles)


@pytest.mark.asyncio
async def test_trade_chart_uses_persisted_candles_and_recorded_trade_levels():
    entered_at = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    position = Position(
        id=18,
        symbol="ETH/USDT",
        side="LONG",
        entry_price=2500,
        current_price=2575,
        volume=1,
        stop=2505,
        take=2600,
        status="CLOSED",
        entered_at=entered_at,
        closed_at=entered_at + timedelta(hours=2),
        entry_context={
            "scale_out": {"tp1_price": 2503, "tp2_price": 2535, "tp1_fill_price": 2503.2},
            "stop_execution": {"actual_price": 2505.4},
        },
    )
    candles = [
        Candle(
            symbol="ETH/USDT",
            timeframe="1h",
            timestamp=entered_at + timedelta(hours=offset),
            open=2499 + offset,
            high=2504 + offset,
            low=2497 + offset,
            close=2501 + offset,
            volume=100,
            source="ccxt",
        )
        for offset in range(3)
    ]

    chart = await position_chart(18, "1h", None, _Db(position, candles))

    assert chart.symbol == "ETH/USDT"
    assert len(chart.candles) == 3
    assert {(level.key, level.price) for level in chart.levels} >= {
        ("entry", 2500),
        ("tp1_price", 2503),
        ("tp1_fill", 2503.2),
        ("stop_fill", 2505.4),
    }
    assert [(marker.key, marker.price) for marker in chart.markers] == [("entry", 2500), ("exit", 2575)]


@pytest.mark.asyncio
@pytest.mark.parametrize("timeframe", ("4h", "12h", "1d"))
async def test_trade_chart_accepts_requested_longer_timeframes(timeframe: str):
    entered_at = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    position = Position(
        id=19,
        symbol="ETH/USDT",
        side="LONG",
        entry_price=2500,
        current_price=2520,
        volume=1,
        stop=2475,
        take=2575,
        status="OPEN",
        entered_at=entered_at,
        entry_context={},
    )
    candle = Candle(
        symbol="ETH/USDT",
        timeframe=timeframe,
        timestamp=entered_at,
        open=2500,
        high=2525,
        low=2490,
        close=2520,
        volume=100,
        source="ccxt",
    )

    chart = await position_chart(19, timeframe, None, _Db(position, [candle]))

    assert chart.timeframe == timeframe
    assert chart.candles[0].timestamp == entered_at


def test_trade_chart_aggregates_persisted_hourly_ohlcv_without_inventing_prices():
    start = datetime(2026, 9, 28, 8, tzinfo=timezone.utc)
    hourly = [
        Candle(
            symbol="ETH/USDT",
            timeframe="1h",
            timestamp=start + timedelta(hours=index),
            open=100 + index,
            high=103 + index,
            low=98 + index,
            close=101 + index,
            volume=10 * (index + 1),
            source="ccxt",
        )
        for index in range(4)
    ]

    aggregated = _aggregate_hourly_candles(hourly, "4h")

    assert len(aggregated) == 1
    assert aggregated[0].timestamp == start
    assert (aggregated[0].open, aggregated[0].high, aggregated[0].low, aggregated[0].close) == (100, 106, 98, 104)
    assert aggregated[0].volume == 100
    assert aggregated[0].source == "aggregated_1h"


def test_live_chart_rows_replace_only_matching_persisted_candle_timestamps():
    timestamp = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    persisted = [
        Candle(
            symbol="ETH/USDT", timeframe="1h", timestamp=timestamp,
            open=100, high=101, low=99, close=100.5, volume=10, source="ccxt",
        ),
        Candle(
            symbol="ETH/USDT", timeframe="1h", timestamp=timestamp + timedelta(hours=1),
            open=100.5, high=102, low=100, close=101, volume=11, source="ccxt",
        ),
    ]
    live = _live_chart_candles(
        [[int((timestamp + timedelta(hours=1)).timestamp() * 1000), 100.5, 104, 100, 103, 17]],
        "ETH/USDT",
        "1h",
    )

    merged = _merge_chart_candles(persisted, live)

    assert len(merged) == 2
    assert merged[0].close == 100.5
    assert (merged[1].high, merged[1].close, merged[1].volume, merged[1].source) == (104, 103, 17, "exchange_live")
