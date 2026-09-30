from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

from app.api.routes.positions import _chart_levels, _chart_markers, _close_position_locked, _downsample_candles
from app.models.entities import Candle, Order, OrderStatus, Position, Trade


class _Scalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return list(self.values)

    def first(self):
        return self.values[0] if self.values else None


class _Result:
    def __init__(self, values, *, position=None):
        self.values = values
        self.position = position

    def scalar_one_or_none(self):
        return self.position

    def scalars(self):
        return _Scalars(self.values)


class _Db:
    def __init__(self, position, trades):
        self.position = position
        self.trades = list(trades)
        self.items = []
        self.committed = False

    async def execute(self, _statement):
        if not self.items:
            return _Result([], position=self.position)
        return _Result(self.trades)

    def add(self, item):
        self.items.append(item)
        if isinstance(item, Trade):
            self.trades.append(item)

    async def commit(self):
        self.committed = True

    async def refresh(self, _item):
        return None


@pytest.mark.asyncio
async def test_manual_close_keeps_unfilled_remainder_open_and_auditable():
    position = Position(
        id=7,
        symbol="BTC/USDT",
        side="LONG",
        entry_price=100,
        current_price=100,
        volume=1,
        stop=96,
        take=108,
        status="OPEN",
        entry_context={"entry_execution": {"fee": 0.1}},
    )
    entry_trade = Trade(
        position_id=7,
        symbol="BTC/USDT",
        side="LONG",
        entry_price=100,
        exit_price=None,
        profit=-0.1,
    )
    db = _Db(position, [entry_trade])
    execution = SimpleNamespace(
        execute_market=_partial_market_exit(
            Order(
                status=OrderStatus.PARTIAL.value,
                requested_amount=1,
                filled_amount=0.4,
                average_price=101,
                fee=0.01,
                slippage=0.02,
            )
        )
    )

    result = await _close_position_locked(7, db, execution)

    assert result.status == "OPEN"
    assert result.volume == 0.6
    assert result.current_price == 101
    assert result.pnl == 0.89
    partial_trade = next(item for item in db.items if isinstance(item, Trade))
    assert partial_trade.profit == 0.39
    event = next(item for item in db.items if getattr(item, "context", {}).get("event") == "POSITION_EXIT_PARTIALLY_FILLED")
    assert event.context["requested_volume"] == 1
    assert event.context["filled_volume"] == 0.4
    assert event.context["remaining_volume"] == 0.6
    assert db.committed is True


def _partial_market_exit(order):
    async def execute_market(*_args, **_kwargs):
        return order

    return execute_market


def test_trade_chart_returns_recorded_levels_and_only_actual_entry_exit_markers():
    entered_at = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    position = Position(
        id=8,
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
            "scale_out": {"tp1_price": 2503, "tp2_price": 2535, "tp1_fill_price": 2503.2, "tp2_fill_price": 2534.9},
            "breakeven_protection": {"price_bu": 2503},
            "stop_execution": {"actual_price": 2505.4},
        },
    )

    levels = _chart_levels(position)
    markers = _chart_markers(position)

    assert {(level.key, level.price) for level in levels} >= {
        ("entry", 2500),
        ("stop", 2505),
        ("take", 2600),
        ("tp1_price", 2503),
        ("tp2_price", 2535),
        ("tp1_fill", 2503.2),
        ("tp2_fill", 2534.9),
        ("stop_fill", 2505.4),
    }
    assert [(marker.key, marker.price) for marker in markers] == [("entry", 2500), ("exit", 2575)]
    assert markers[1].timestamp == entered_at + timedelta(hours=2)


def test_trade_chart_downsampling_preserves_first_and_last_saved_candle():
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    candles = [
        Candle(
            symbol="BTC/USDT",
            timeframe="1h",
            timestamp=start + timedelta(hours=index),
            open=100 + index,
            high=101 + index,
            low=99 + index,
            close=100.5 + index,
            volume=10,
            source="ccxt",
        )
        for index in range(11)
    ]

    sampled, note = _downsample_candles(candles, limit=4)

    assert len(sampled) == 4
    assert sampled[0].timestamp == candles[0].timestamp
    assert sampled[-1].timestamp == candles[-1].timestamp
    assert note and "4 из 11" in note
