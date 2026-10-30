"""Stream replay through production paper execution and a real SQL ledger."""
import time
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as Row
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import Base
from app.models.entities import Order, Position, Trade
from app.services.exchange import PreparedOrder
from app.services.market_stream import normalize_event
from app.services.reconciliation import OrderReconciliationService
from app.services.scalping import ScalpingEngine, scalp_features, scalp_levels, scalp_reward
from app.services.trading_engine import TradingEngine
from test_paper_exit_lifecycle import Ledger


def snapshots(direction=1):
    now = time.time()*1000
    book = {"timestamp": now,
        "bids": [[100-i*.01, 30 if direction == 1 else 12] for i in range(10)],
        "asks": [[100.01+i*.01, 12 if direction == 1 else 30] for i in range(10)]}
    tape = {"timestamp": now, "trades": [
        {"id": i+1, "timestamp": now-(19000-i*1000), "price": 100 + (i%2)*(.001 if i < 10 else .08),
         "amount": 100, "side": "buy" if direction == 1 else "sell"} for i in range(20)]}
    return book, tape


@pytest.mark.parametrize("direction", [1, -1])
def test_features_micro_stops_and_net_speed_reward(direction):
    s = get_settings()
    book, tape = snapshots(direction)
    f = scalp_features(book, tape, time.time()*1000, s)
    assert f["direction"] == direction
    stop, take = scalp_levels(100, direction, f, s)
    assert stop == pytest.approx(100-direction*.2)
    assert take == pytest.approx(100+direction*.3)
    assert scalp_reward(1, .2, 1, 10, direction*.5, direction) > scalp_reward(1, .2, 1, 100, direction*.5, direction)
    assert scalp_reward(-1, .2, 1, 100, direction*.5, direction) < scalp_reward(-1, .2, 1, 10, direction*.5, direction)
    assert scalp_reward(1, .2, 1, 10, -direction*.5, direction) < scalp_reward(1, .2, 1, 10, direction*.5, direction)


@pytest.mark.parametrize("fault", ["stale", "thin", "flat", "spread", "opposed", "warmup"])
def test_regime_veto(fault):
    book, tape = snapshots()
    if fault == "stale": book["timestamp"] -= 3000
    if fault == "thin": book["asks"] = [[100.01, 1]]
    if fault == "flat":
        for trade in tape["trades"]: trade["price"] = 100
    if fault == "spread": book["asks"] = [[100.2+i*.01, 12] for i in range(10)]
    if fault == "opposed":
        for trade in tape["trades"]: trade["side"] = "sell"
    if fault == "warmup": tape["trades"] = tape["trades"][-4:]
    try:
        assert scalp_features(book, tape, time.time()*1000, get_settings())["direction"] == 0
    except ValueError:
        assert fault in {"stale", "warmup"}


def test_aggtrade_aggressor_and_timestamp():
    event = {"e": "aggTrade", "E": 10000, "T": 9999, "s": "ETHUSDT", "a": 17, "p": "100", "q": "2", "m": True}
    kind, symbol, trade = normalize_event(event, 10000)
    assert (kind, symbol, trade["side"], trade["timestamp"]) == ("trade", "ETH/USDT", "sell", 9999)
    assert normalize_event({**event, "T": 1}, 10000) is None


@pytest.fixture
def rig():
    engine = TradingEngine()
    engine.settings = engine.settings.model_copy(update={"scalping_enabled": True, "paper_trading": True,
        "live_trading_enabled": False, "market_scan_symbols_raw": "ETH/USDT", "paper_starting_balance": 1000,
        "telegram_trade_reports_enabled": False, "equity_snapshot_enabled": False})
    engine.execution.settings = engine.portfolio_accounting.settings = engine.settings
    async def prepare(symbol, amount, price):
        return PreparedOrder(amount=amount, fee_rate=.0004, min_amount=.001, min_cost=5, metadata_available=True)
    exchange = Row(exchange="binance", is_derivatives_market=lambda: True,
        supports_native_protective_stops=lambda: False, prepare_order=AsyncMock(side_effect=prepare),
        fetch_tickers=AsyncMock(side_effect=AssertionError("REST ticker forbidden")),
        create_order=AsyncMock(side_effect=AssertionError("Live order forbidden")))
    engine.exchange = engine.execution.exchange = exchange
    engine.control.is_paused = AsyncMock(return_value=(False, None))
    engine.control.panic = AsyncMock()
    engine.post_mortem.analyze_trade = AsyncMock(side_effect=AssertionError("Candle post mortem forbidden"))
    engine.learning.record_closed_position = AsyncMock(return_value={})
    engine.agents.competition.record_closed_position = AsyncMock()
    engine.context.remember_trade = AsyncMock()
    scalp = ScalpingEngine(engine)
    book, tape = snapshots()
    scalp.snapshots = AsyncMock(return_value=({"ETH/USDT": book}, {"ETH/USDT": tape}))
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    with Session(database, expire_on_commit=False) as session:
        yield engine, scalp, Ledger(session), session, book, tape
    database.dispose()


async def fill(rig):
    engine, scalp, db, session, book, tape = rig
    await scalp.enter(db)
    order = session.scalars(select(Order)).one()
    assert order.status == "NEW"
    await scalp.enter(db)  # Touching the quote cannot fill it.
    assert order.status == "NEW"
    tape["trades"].append({"id": 100, "timestamp": time.time()*1000, "price": order.requested_price,
        "amount": order.raw["queue_remaining"]+1, "side": "sell" if order.side == "buy" else "buy"})
    await scalp.enter(db)
    assert order.status == "FILLED"
    return session.scalars(select(Position)).one(), order


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("reason", ["TAKE_PROFIT", "STOP_LOSS", "SCALP_TIME_STOP", "SCALP_TAPE_REVERSAL"])
async def test_full_maker_to_taker_lifecycle(rig, reason, direction):
    engine, scalp, db, session, book, tape = rig
    new_book, new_tape = snapshots(direction)
    book.update(new_book)
    tape.update(new_tape)
    p, entry = await fill(rig)
    assert direction*(p.entry_price-p.stop) > 0 and direction*(p.take-p.entry_price) > 0
    initial_volume = p.volume
    if reason == "TAKE_PROFIT": price = p.take+direction*.01
    elif reason == "STOP_LOSS": price = p.stop-direction*.01
    elif reason == "SCALP_TIME_STOP":
        price = p.entry_price
        p.entered_at = datetime.now(timezone.utc)-timedelta(seconds=76)
    else:
        price = p.entry_price
        for t in tape["trades"]: t["side"] = "sell" if direction == 1 else "buy"
    book["bids"] = [[price-(.01 if direction == -1 else 0)-i*.01, 30] for i in range(10)]
    book["asks"] = [[price+(.01 if direction == 1 else 0)+i*.01, 12] for i in range(10)]
    await scalp.manage(db)
    assert p.status == "CLOSED" and p.exit_reason == reason
    orders = session.scalars(select(Order).order_by(Order.id)).all()
    exit = orders[-1]
    expected = direction*(exit.average_price-entry.average_price)*initial_volume-entry.fee-exit.fee
    assert p.pnl == pytest.approx(expected)
    assert sum(session.scalars(select(Trade.profit)).all()) == pytest.approx(expected)
    assert "scalp_reward" in p.entry_context
    assert (p.pnl > 0) == (reason == "TAKE_PROFIT")
    engine.exchange.create_order.assert_not_awaited()
    engine.exchange.fetch_tickers.assert_not_awaited()
    engine.post_mortem.analyze_trade.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_expiry_and_reconciliation_ignore(rig):
    engine, scalp, db, session, book, tape = rig
    await scalp.enter(db)
    order = session.scalars(select(Order)).one()
    result = await OrderReconciliationService(engine.exchange).reconcile(db)
    assert result["checked"] == 0
    order.raw = {**order.raw, "created_seconds": time.time()-11}
    await scalp.enter(db)
    assert order.status == "CANCELLED"
    assert not session.scalars(select(Position)).all()


@pytest.mark.asyncio
async def test_stale_exit_never_invents_fill_and_pause_cancels_limit(rig):
    engine, scalp, db, session, book, tape = rig
    p, order = await fill(rig)
    book["timestamp"] -= 3000
    await scalp.manage(db)
    await scalp.manage(db)
    assert p.status == "OPEN" and p.entry_context["scalp_data_alarm"]
    assert len(session.scalars(select(Order)).all()) == 1


@pytest.mark.asyncio
async def test_other_entry_paths_and_live_disabled(rig):
    engine, scalp, db, session, book, tape = rig
    with pytest.raises(ValueError, match="maker queue"):
        await engine.execution.execute_market(db, "ETH/USDT", "buy", 1, 100, "ENTRY")
    engine.settings.paper_trading = False
    assert (await scalp.enter(db)).opened == 0
    assert not session.scalars(select(Order)).all()


@pytest.mark.asyncio
async def test_partial_then_final_pnl_includes_each_fee_once(rig):
    engine, scalp, db, session, book, tape = rig
    p, entry = await fill(rig)
    size, original_stop = p.volume, p.stop
    book["bids"] = [[100.2-i*.01, 30] for i in range(10)]
    book["asks"] = [[100.21+i*.01, 500 if i == 1 else 12] for i in range(10)]
    await scalp.manage(db)
    assert p.volume == pytest.approx(size/2)
    assert p.entry_context["scalping"]["partial"] and p.stop == original_stop
    await scalp.manage(db)
    assert p.volume == pytest.approx(size/2)  # No repeated partial on same wall.
    book["bids"][0][0] = p.take
    book["asks"][0][0] = p.take+.01
    await scalp.manage(db)
    assert p.status == "CLOSED"
    exits = session.scalars(select(Order).where(Order.id != entry.id)).all()
    assert len(exits) == 2
    expected = sum((o.average_price-p.entry_price)*o.filled_amount-o.fee for o in exits)-entry.fee
    assert p.pnl == pytest.approx(expected, abs=2e-8)
    assert sum(session.scalars(select(Trade.profit)).all()) == pytest.approx(expected, abs=2e-8)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["submit", "fill"])
async def test_daily_limit_veto_including_waiting_order(rig, stage):
    engine, scalp, db, session, book, tape = rig
    if stage == "fill":
        await scalp.enter(db)
        order = session.scalars(select(Order)).one()
        tape["trades"].append({"id": 100, "timestamp": time.time()*1000, "price": 100,
            "amount": order.raw["queue_remaining"]+1, "side": "sell"})
    db.add(Trade(symbol="BTC/USDT", side="LONG", entry_price=100, profit=-30))
    await db.commit()
    await scalp.enter(db)
    assert not session.scalars(select(Position)).all()
    if stage == "fill": assert order.status == "CANCELLED"
    else: assert not session.scalars(select(Order)).all()


@pytest.mark.asyncio
async def test_wall_target_cannot_pay_costs(rig):
    engine, scalp, db, session, book, tape = rig
    book["asks"][1][1] = 60
    assert scalp_features(book, tape, time.time()*1000, engine.settings)["direction"] == 1
    await scalp.enter(db)
    assert not session.scalars(select(Order)).all()
    engine.exchange.prepare_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_cancels_pending_and_failed_exit_blocks_entries(rig):
    engine, scalp, db, session, book, tape = rig
    await scalp.enter(db)
    order = session.scalars(select(Order)).one()
    engine.control.is_paused.return_value = (True, "manual")
    await scalp.manage(db)
    assert order.status == "CANCELLED"
    engine.control.is_paused.return_value = (False, None)
    db.add(Position(symbol="BTC/USDT", side="LONG", entry_price=100, current_price=100,
        volume=1, stop=99, take=101, entry_context={"exit_failure": {"review_required": True}}))
    await db.commit()
    await scalp.enter(db)
    assert len(session.scalars(select(Order)).all()) == 1


@pytest.mark.asyncio
async def test_production_router_uses_stream_path_not_candles(rig, monkeypatch):
    from app.services.risk_manager import RiskSettings
    engine, scalp, db, session, book, tape = rig
    monkeypatch.setattr(ScalpingEngine, "snapshots", scalp.snapshots)
    engine.scanner.scan = AsyncMock(side_effect=AssertionError("Candles forbidden"))
    await engine.run_once(db, RiskSettings(balance=1000, risk_percent=1, daily_risk_percent=2.5,
        max_positions=3, min_rating=70, stop_loss_percent=1, take_profit_percent=2, trailing_stop_percent=0))
    assert session.scalars(select(Order)).one().status == "NEW"
    await engine.manage_open_positions(db)
    engine.scanner.scan.assert_not_awaited()
