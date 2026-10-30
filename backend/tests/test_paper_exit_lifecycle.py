"""Execute the production paper lifecycle against a real SQLite ledger.

Only market data/venue metadata and unrelated external services are replaced.
No exchange order submission is allowed.
"""
from types import SimpleNamespace as Row
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.entities import Order, Position, Trade, LogEntry
from app.services.exchange import PreparedOrder
from app.services.risk_manager import RiskSettings
from app.services.trading_engine import TradingEngine


class Ledger:
    """Async-shaped adapter over synchronous SQLite, without mocking SQL."""
    def __init__(self, session):
        self.session = session

    def add(self, obj):
        self.session.add(obj)

    def get_bind(self):
        return self.session.get_bind()

    async def execute(self, query):
        return self.session.execute(query)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()


@pytest.mark.parametrize("side,direction", [("LONG", 1), ("SHORT", -1)])
def test_tp1_is_exact_r_even_below_costs_or_with_stale_stored_target(side, direction):
    engine = TradingEngine()
    plan = engine._initial_scale_out_plan(side=side, entry_price=100,
        strategy_take=100 + direction * 2, entry_volume=1, entry_fee=.04,
        tp1_percent=25, initial_risk=.01, tp1_r=.5)
    target = 100 + direction * .005
    assert plan["tp1_price"] == pytest.approx(target)
    p = Position(side=side, entry_price=100, initial_risk=.01,
        partial_take_profit_r=.5, entry_context={"scale_out": {"tp1_price": 100 + direction}},
        stop=100 - direction * .01)
    assert not engine._partial_take_profit_reached(p, target - direction * .000001)
    assert engine._partial_take_profit_reached(p, target)


@pytest.mark.asyncio
@pytest.mark.parametrize("side,direction", [("LONG", 1), ("SHORT", -1)])
@pytest.mark.parametrize("exploration", [False, True])
@pytest.mark.parametrize("experiment_enabled", [False, True])
@pytest.mark.parametrize("exit_reason", ["STOP_LOSS", "BREAKEVEN_STOP", "TRAILING_STOP", "TAKE_PROFIT", "GAP"])
async def test_paper_lifecycle_persists_fills_fees_stop_changes_and_final_pnl(side, direction, exploration, exit_reason, experiment_enabled):
    engine = TradingEngine()
    # Isolate config: never modify the running bot or the cached settings.
    engine.settings = engine.settings.model_copy(update={
        "paper_trading": True, "live_trading_enabled": False, "market_data_mode": "ccxt",
        "paper_fee_rate": .0004, "paper_slippage_bps": 2,
        "telegram_trade_reports_enabled": False, "post_mortem_enabled": False,
        "trailing_activation_r": 1.5, "dynamic_take_profit_enabled": False,
        "min_entry_notional_usdt": 5, "min_exit_notional_usdt": 5,
        "experimental_exit_limits_enabled": experiment_enabled,
    })
    engine.execution.settings = engine.settings
    quote = {"price": 100.0}
    async def prepare(symbol, amount, price):
        return PreparedOrder(amount=amount, fee_rate=.0004,
            min_amount=.000001, min_cost=5, metadata_available=True)
    exchange = Row(
        is_derivatives_market=lambda: True,
        supports_native_protective_stops=lambda: False,
        prepare_order=prepare,
        prepare_derivatives_symbol=AsyncMock(return_value={"market_type": "future"}),
        get_balance=AsyncMock(return_value={"USDT": 1000}),
        fetch_tickers=AsyncMock(side_effect=lambda symbols: {"ETH/USDT": {"last": quote["price"]}}),
        create_order=AsyncMock(side_effect=AssertionError("Live order forbidden")),
    )
    engine.exchange = engine.execution.exchange = exchange
    engine.learning.entry_context = Mock(return_value={})
    engine._paper_exploration_votes = Mock(return_value=(4, 1))
    engine._enforce_drawdown_limit = AsyncMock(return_value=Row(emergency=False))
    engine.post_mortem.analyze_trade = AsyncMock(return_value=None)
    engine.learning.record_closed_position = AsyncMock(return_value={})
    engine.agents.competition.record_closed_position = AsyncMock()
    engine.context.remember_trade = AsyncMock()
    engine.control.panic = AsyncMock()
    settings = RiskSettings(balance=1000, risk_percent=1, daily_risk_percent=3,
        max_positions=3, min_rating=70, stop_loss_percent=1, take_profit_percent=2,
        trailing_stop_percent=.3, risk_reward_ratio=2, partial_take_profit_r=.75,
        breakeven_trigger_r=1.2, partial_close_percent=25)
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    try:
        with Session(database, expire_on_commit=False) as session:
            db = Ledger(session)
            p = await engine._open_position(db, Row(symbol="ETH/USDT", price=100, atr=.5, spread_bps=0, funding_rate=0),
                "BUY" if direction == 1 else "SELL", [], 1000, settings, paper_exploration=exploration)
            assert p is not None
            await db.commit()
            entry, risk, size = p.entry_price, p.initial_risk, p.volume
            original_stop = p.stop
            assert entry == pytest.approx(100 * (1 + direction * .0002))
            assert p.take == pytest.approx(entry + direction * 2 * risk)
            assert p.stop == pytest.approx(entry - direction * risk)
            assert p.entry_context["scale_out"]["mode"] == ("MONOLITHIC" if experiment_enabled else "CASCADE")
            assert p.entry_context["protective_stop"]["mode"] == "LOCAL_MONITOR"
            if experiment_enabled:
                assert p.entry_context["exit_experiment"]["post_fill"]["loss"] <= .5
                assert p.entry_context["exit_experiment"]["post_fill"]["profit"] >= .6

            async def tick(price):
                quote["price"] = price
                await engine.manage_open_positions(db)

            async def at_r(multiple):
                await tick(entry + direction * risk * multiple)

            if exit_reason == "GAP":
                await at_r(-1.2)
            elif exit_reason == "STOP_LOSS":
                await tick(original_stop)
            else:
                await at_r(.749)
                assert not p.partial_taken and p.stop == original_stop
                await at_r(.75)
                assert p.partial_taken is (not experiment_enabled)
                assert p.volume == pytest.approx(size * (1 if experiment_enabled else .75), abs=1e-8)
                assert not p.breakeven_applied and p.stop == original_stop
                partials = session.scalars(select(Order).where(Order.raw["reason"].as_string() == "PARTIAL_TAKE_PROFIT")).all()
                assert len(partials) == (0 if experiment_enabled else 1)
                if partials:
                    assert partials[0].requested_price == pytest.approx(entry + direction * risk * .75)
                    assert partials[0].status == "FILLED"
                if experiment_enabled:
                    await engine._close_position(db, p, p.current_price, "MANUAL")
                    assert p.status == "OPEN"  # No voluntary small-profit escape.
                await at_r(1.199)
                assert not p.breakeven_applied and p.stop == original_stop
                await at_r(1.201)
                assert p.breakeven_applied
                assert direction * (p.stop - original_stop) > 0
                be_stop = p.stop
                assert p.entry_context["stop_origin"] == "BREAKEVEN_STOP"
                if exit_reason == "BREAKEVEN_STOP":
                    await tick(be_stop)
                else:
                    await at_r(1.51)
                    assert direction * (p.stop - be_stop) > 0
                    assert p.entry_context["stop_origin"] == "TRAILING_STOP"
                    if exit_reason == "TRAILING_STOP":
                        await tick(p.stop)
                    else:
                        await at_r(1.8)  # TP2 also executes; final exit uses the remaining inventory.
                        await tick(p.take)
            assert p.status == "CLOSED"
            expected_reason = ("EXPERIMENT_LOSS_LIMIT" if experiment_enabled else "STOP_LOSS") if exit_reason == "GAP" else exit_reason
            assert p.exit_reason == expected_reason
            orders = session.scalars(select(Order).order_by(Order.id)).all()
            assert all(o.status == "FILLED" for o in orders)
            opening, *exits = orders
            assert sum(o.filled_amount for o in exits) == pytest.approx(opening.filled_amount, abs=1e-8)
            expected = sum(direction * (o.average_price - entry) * o.filled_amount - o.fee for o in exits) - opening.fee
            assert p.pnl == pytest.approx(expected, abs=.0002)
            trades = session.scalars(select(Trade).where(Trade.position_id == p.id)).all()
            assert sum(t.profit for t in trades) == pytest.approx(p.pnl, abs=.0001)
            if exit_reason in {"STOP_LOSS", "GAP"}:
                assert p.pnl < 0
            elif exit_reason == "BREAKEVEN_STOP" and experiment_enabled:
                assert p.pnl >= -.0001  # Four-decimal journal rounding at exact cost coverage.
            else:
                assert p.pnl > 0
            if exit_reason == "TAKE_PROFIT":
                assert len(exits) == (1 if experiment_enabled else 3)
                if experiment_enabled:
                    assert p.pnl > .5
            if experiment_enabled:
                audit = p.entry_context["experiment_result"]
                assert audit["fees"] == pytest.approx(sum(o.fee for o in orders))
                assert audit["slippage_cost"] == pytest.approx(sum(o.slippage * o.filled_amount for o in orders))
                assert audit["profit_below_target"] == (0 < p.pnl < .6)
                if exit_reason == "GAP":
                    assert audit["loss_breach"]
                    engine.control.panic.assert_awaited_once()
            logs = session.scalars(select(LogEntry)).all()
            assert logs
            await db.commit()
            position_id, final_pnl = p.id, p.pnl
            session.expunge_all()
            persisted = session.get(Position, position_id)
            assert persisted.exit_reason == expected_reason
            assert persisted.pnl == final_pnl
            exchange.create_order.assert_not_awaited()
    finally:
        database.dispose()

