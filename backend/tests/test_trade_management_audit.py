from types import SimpleNamespace as Row
from unittest.mock import AsyncMock, Mock
import pytest
from app.models.entities import Position, Order
from app.services.trading_engine import TradingEngine
from app.services.execution import ExecutionService
from app.services.risk_manager import RiskManager


def position(side="LONG", **changes):
    fields = dict(id=1, symbol="ETH/USDT", side=side, entry_price=100,
        current_price=100, volume=1, stop=98 if side == "LONG" else 102,
        take=104 if side == "LONG" else 96, initial_risk=2,
        partial_taken=False, partial_take_profit_r=1, partial_close_percent=25,
        breakeven_applied=False, breakeven_trigger_r=1, breakeven_offset_percent=.05,
        highest_price=100, lowest_price=100, trailing_stop_percent=.8, status="OPEN",
        entry_context={"entry_execution": {"fee": .04, "volume": 1}})
    return Position(**{**fields, **changes})


@pytest.mark.parametrize("side,direction", [("LONG", 1), ("SHORT", -1)])
def test_first_take_respects_configured_r_and_legacy_cost_only_plan(side, direction):
    engine = TradingEngine()
    p = position(side, partial_take_profit_r=1.5)
    p.entry_context["scale_out"] = {"mode": "CASCADE", "tp1_price": 100 + direction * .1}
    assert not engine._partial_take_profit_reached(p, 100 + direction * .2)
    assert not engine._partial_take_profit_reached(p, 100 + direction * 2.9)
    assert engine._partial_take_profit_reached(p, 100 + direction * 3)


@pytest.mark.parametrize("side,direction", [("LONG", 1), ("SHORT", -1)])
def test_be_requires_confirmed_partial_and_configured_trigger(side, direction):
    engine = TradingEngine()
    p = position(side, current_price=100 + direction * 4, breakeven_trigger_r=2)
    assert not engine._apply_breakeven(p)
    p.partial_taken = True
    p.current_price = 100 + direction * 3
    assert not engine._apply_breakeven(p)
    p.current_price = 100 + direction * 4
    assert engine._apply_breakeven(p)
    # Actual paper fill at protected price covers both fees and slippage.
    fill = p.stop * (1 - direction * engine.settings.paper_slippage_bps / 10000)
    net = direction * (fill - 100) - .04 - fill * .0004
    assert net >= -1e-8
    assert p.entry_context["stop_origin"] == "BREAKEVEN_STOP"


def test_requested_be_offset_is_honored_without_widening_existing_stop():
    engine = TradingEngine()
    p = position(current_price=103, partial_taken=True, breakeven_offset_percent=.5)
    assert engine._apply_breakeven(p)
    assert p.stop == pytest.approx(100.5)
    p = position(current_price=103, partial_taken=True, stop=101,
                 entry_context={"stop_origin": "TRAILING_STOP"})
    assert engine._apply_breakeven(p)
    assert p.stop == 101
    assert engine._stop_exit_reason(p) == "TRAILING_STOP"


@pytest.mark.parametrize("side,direction", [("LONG", 1), ("SHORT", -1)])
def test_trailing_trigger_monotonicity_and_reason(side, direction):
    engine = TradingEngine()
    p = position(side)
    p.highest_price, p.lowest_price = (101.9, 100) if direction == 1 else (100, 98.1)
    before = p.stop
    engine._apply_trailing_stop(p)
    assert p.stop == before
    p.highest_price, p.lowest_price = (103, 100) if direction == 1 else (100, 97)
    engine._apply_trailing_stop(p)
    protected = p.stop
    p.current_price = protected
    assert engine._exit_reason(p) == "TRAILING_STOP"
    engine._apply_trailing_stop(p)
    assert p.stop == protected


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_invalid_partial_target_order_uses_full_exit_without_changing_final_target(side):
    engine = TradingEngine()
    target = 104 if side == "LONG" else 96
    plan = engine._initial_scale_out_plan(side=side, entry_price=100, strategy_take=target,
        entry_volume=10, entry_fee=.4, tp1_percent=25, initial_risk=2, tp1_r=1)
    assert plan["mode"] == "MONOLITHIC"  # TP2 fixed 1.25% would precede TP1=2%.
    assert plan["tp3_price"] == target


@pytest.mark.asyncio
@pytest.mark.parametrize("side,quote,expected", [
    ("LONG", 97, "STOP_LOSS"), ("LONG", 105, "TAKE_PROFIT"),
    ("SHORT", 103, "STOP_LOSS"), ("SHORT", 95, "TAKE_PROFIT"),
])
async def test_existing_exit_precedes_partial_and_stop_replacement(side, quote, expected):
    engine = TradingEngine()
    p = position(side)
    engine.settings = Row(paper_starting_balance=1000, uses_live_market_data=True)
    engine.exchange.get_balance = AsyncMock(return_value={"USDT": 1000})
    engine.exchange.fetch_tickers = AsyncMock(return_value={p.symbol: {"last": quote}})
    engine._enforce_drawdown_limit = AsyncMock(return_value=Row(emergency=False))
    engine._position_total_pnl = AsyncMock(return_value=0)
    engine._capture_position_microstructure = AsyncMock()
    engine._pending_order_symbols = AsyncMock(return_value=set())
    engine._process_filled_protective_stop = AsyncMock(return_value=False)
    engine._apply_partial_take_profit = AsyncMock()
    engine._replace_protective_stop = AsyncMock()
    engine._handle_dynamic_take_profit = AsyncMock(return_value="CLOSE")
    engine._close_position = AsyncMock()
    db = Row(execute=AsyncMock(return_value=Row(scalars=lambda: Row(all=lambda: [p]))),
             add=Mock(), commit=AsyncMock())
    await engine.manage_open_positions(db)
    assert engine._close_position.call_args.args[3] == expected
    engine._apply_partial_take_profit.assert_not_awaited()
    engine._replace_protective_stop.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("side,direction", [("LONG", 1), ("SHORT", -1)])
@pytest.mark.parametrize("movement", [-2, 4])
async def test_paper_win_and_loss_use_actual_fills_and_each_fee_once(side, direction, movement, monkeypatch):
    engine = TradingEngine()
    service = ExecutionService()
    monkeypatch.setattr(service.settings, "paper_trading", True)
    db = Row(execute=AsyncMock(return_value=Row(scalar_one_or_none=lambda: None)), add=Mock(), flush=AsyncMock())
    p = position(side)
    reference = 100 + direction * movement
    order = await service.execute_market(db, p.symbol, "sell" if direction == 1 else "buy", 1, reference,
        "EXIT_STOP_LOSS" if movement < 0 else "EXIT_TAKE_PROFIT")
    assert order.status == "FILLED"
    actual = engine._final_trade_profit(existing_trade_profit=-.04, position=p,
                                       exit_price=order.average_price, exit_fee=order.fee)
    expected = direction * (order.average_price - 100) - .04 - order.fee
    assert actual == pytest.approx(expected, abs=.00005)  # Journal stores four decimal places.
    assert actual < movement  # Costs reduce gains and increase losses.


def test_hard_risk_cap_applies_even_without_minimum_notional():
    volume = RiskManager().calculate_position_size(1000, 3, 100, 98,
        max_risk_percent=1, cost_per_unit=.2)
    assert volume * 2.2 <= 10


@pytest.mark.asyncio
@pytest.mark.parametrize("side,signal", [("LONG", "BUY"), ("SHORT", "SELL")])
@pytest.mark.parametrize("exploration", [False, True])
async def test_entry_preserves_price_rr_and_reports_net_rr_in_both_lanes(side, signal, exploration):
    from app.services.risk_manager import RiskSettings
    engine = TradingEngine()
    engine.exchange.is_derivatives_market = Mock(return_value=False)
    engine.learning.entry_context = Mock(return_value={})
    engine._paper_exploration_votes = Mock(return_value=(4, 1))
    engine._assess_scale_out_plan = AsyncMock(side_effect=lambda symbol, plan: plan)
    engine._replace_protective_stop = AsyncMock(return_value=False)
    order = Order(id=3, status="FILLED", average_price=100, filled_amount=1,
                  requested_amount=1, fee=.04, slippage=0, raw={})
    engine.execution.execute_market = AsyncMock(return_value=order)
    db = Row(add=Mock(), flush=AsyncMock())
    settings = RiskSettings(balance=1000, risk_percent=1, daily_risk_percent=3,
        max_positions=3, min_rating=70, stop_loss_percent=1, take_profit_percent=2,
        trailing_stop_percent=.8, risk_reward_ratio=2)
    p = await engine._open_position(db, Row(symbol="ETH/USDT", price=100, atr=2, spread_bps=0),
        signal, [], 1000, settings, paper_exploration=exploration)
    assert p.side == side
    assert abs(p.take - p.entry_price) / abs(p.entry_price - p.stop) == pytest.approx(2)
    c = p.entry_context
    assert c["paper_exploration"] == exploration
    assert c["configured_price_reward_risk"] == 2
    assert c["planned_risk"] > abs(p.entry_price - p.stop) * p.volume
    assert c["planned_reward"] < abs(p.take - p.entry_price) * p.volume
    assert c["risk_reward_ratio"] < 2
    assert c["actual_planned_risk_percent"] == pytest.approx(c["planned_risk"] / 10, abs=.00001)


@pytest.mark.asyncio
async def test_stop_filled_during_cancel_uses_actual_cause_not_take_intent(monkeypatch):
    from app.models.entities import Trade
    engine = TradingEngine()
    monkeypatch.setattr(engine.settings, "telegram_trade_reports_enabled", False)
    p = position()
    stop_fill = Order(id=2, status="FILLED", filled_amount=1, average_price=98,
        fee=.0392, slippage=0, raw={"stop_origin": "STOP_LOSS", "stop_price": 98})
    engine._cancel_active_protective_stop = AsyncMock(return_value=stop_fill)
    engine.execution.execute_market = AsyncMock()
    engine._position_profit_sum = AsyncMock(return_value=0)
    engine.post_mortem.analyze_trade = AsyncMock(return_value=None)
    engine.learning.record_closed_position = AsyncMock(return_value={})
    engine.agents.competition.record_closed_position = AsyncMock()
    engine.context.remember_trade = AsyncMock()
    trade = Trade(id=1, position_id=1, profit=-.04)
    db = Row(execute=AsyncMock(return_value=Row(scalars=lambda: Row(first=lambda: trade))), add=Mock())
    await engine._close_position(db, p, 104, "TAKE_PROFIT")
    assert p.exit_reason == "STOP_LOSS"
    assert p.pnl == pytest.approx(-2.0792)
    assert p.entry_context["stop_execution"]["expected_price"] == 98
    engine.execution.execute_market.assert_not_awaited()


def test_favorable_stop_fill_is_not_reported_as_adverse_stop_deviation():
    engine = TradingEngine()
    p = position()
    engine._record_stop_slippage_observation(Row(add=Mock()), p, expected_price=98,
        actual_price=98.1, exit_fee=.04, exit_slippage=0, reason="STOP_LOSS", cycle_id=None)
    assert p.entry_context["stop_execution"]["stop_adverse_deviation_bps"] == 0
