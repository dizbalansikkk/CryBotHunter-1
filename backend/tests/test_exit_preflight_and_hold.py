from types import SimpleNamespace as Row
from unittest.mock import AsyncMock, Mock
import pytest
from app.models.entities import Order, Position
from app.services.trading_engine import TradingEngine


def assessment(amount, allowed=True):
    return Row(amount=amount, allowed=allowed, reason="ok" if allowed else "below minimum",
               minimum_notional=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stage", [0, 1, 2, None])
async def test_preflight_checks_both_takes_and_normalized_remainder(failed_stage):
    engine = TradingEngine()
    plan = engine._initial_scale_out_plan(side="LONG", entry_price=100, strategy_take=110,
        entry_volume=1, entry_fee=.1, tp1_percent=25)
    results = [assessment(.25), assessment(.35), assessment(.40)]
    if failed_stage is not None:
        results[failed_stage].allowed = False
    engine.execution.assess_exit_size = AsyncMock(side_effect=results)
    result = await engine._assess_scale_out_plan("ETH/USDT", plan)
    assert result["mode"] == ("CASCADE" if failed_stage is None else "MONOLITHIC")
    assert result["entry_volume"] == 1
    if failed_stage in (2, None):
        assert engine.execution.assess_exit_size.call_args.args[1] == .4


@pytest.mark.asyncio
async def test_preflight_runs_before_entry_and_never_increases_size():
    engine = TradingEngine()
    engine.exchange.is_derivatives_market = Mock(return_value=False)
    engine._exit_plan = Mock(return_value=(98, 106, 2))
    engine.risk.calculate_position_size = Mock(return_value=.04)
    engine._entry_sizing = Mock(return_value={})
    calls = []
    async def assess(*args):
        calls.append("assess")
        return assessment(0, False)
    async def execute(*args, **kwargs):
        calls.append("entry")
        assert args[3] == .04
        return Order(status="FAILED")
    engine.execution.assess_exit_size = assess
    engine.execution.execute_market = execute
    settings = Row(risk_percent=1, max_position_size_percent=10,
                   max_risk_percent_per_trade=1, partial_close_percent=25)
    await engine._open_position(Row(), Row(symbol="ETH/USDT", price=100, atr=2),
        "BUY", [], 100, settings)
    assert calls == ["assess", "entry"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["NEW", "FAILED", "FILLED"])
async def test_unconfirmed_exit_blocks_repeats_and_notifies_once(monkeypatch, status):
    engine = TradingEngine()
    monkeypatch.setattr(engine.settings, "paper_trading", True)
    position = Position(id=5, symbol="ETH/USDT", side="LONG", volume=.1, entry_context={})
    # FILLED without an executed price must not be treated as confirmation.
    order = Order(id=9, status=status, raw={"error": "TimeoutError"}, filled_amount=0)
    engine._cancel_active_protective_stop = AsyncMock(return_value=None)
    engine.execution.execute_market = AsyncMock(return_value=order)
    engine.control.panic = AsyncMock(return_value=False)  # Redis failure must not defeat DB guard.
    engine.telegram.broadcast = AsyncMock()
    db = Row(commit=AsyncMock(), add=Mock())
    await engine._close_position(db, position, 100, "STOP_LOSS")
    await engine._close_position(db, position, 100, "STOP_LOSS")
    engine.execution.execute_market.assert_awaited_once()
    engine.telegram.broadcast.assert_awaited_once()
    assert position.entry_context["exit_failure"]["review_required"]
    assert position.volume == .1
    assert db.add.call_args.args[0].context["event"] == "POSITION_CLOSE_FAILED"


@pytest.mark.asyncio
async def test_live_review_queries_order_and_inventory_without_resubmitting(monkeypatch):
    engine = TradingEngine()
    monkeypatch.setattr(engine.settings, "paper_trading", False)
    position = Position(id=5, symbol="ETH/USDT", side="LONG", volume=.1,
        entry_context={"exit_failure": {"order_id": 9, "review_required": True}})
    order = Order(id=9, symbol="ETH/USDT", raw={"client_order_id": "stable-id"})
    engine.exchange.fetch_order_by_client_id = AsyncMock(return_value={"status": "closed", "filled": .1})
    engine.exchange.fetch_live_positions = AsyncMock(return_value=[])
    engine.execution.execute_market = AsyncMock()
    db = Row(get=AsyncMock(return_value=order), execute=AsyncMock(return_value=Row(
        scalars=lambda: Row(all=lambda: [position]))))
    await engine._review_failed_exit(db, position)
    await engine._review_failed_exit(db, position)
    engine.exchange.fetch_order_by_client_id.assert_awaited_once_with("stable-id", "ETH/USDT")
    engine.exchange.fetch_live_positions.assert_awaited_once()
    engine.execution.execute_market.assert_not_awaited()
    assert position.entry_context["exit_failure"]["inventory_differences"]
    assert position.entry_context["exit_failure"]["review_required"]


@pytest.mark.asyncio
async def test_pending_exit_blocks_entries_before_market_requests():
    engine = TradingEngine()
    engine.exchange.get_balance = AsyncMock()
    db = Row(execute=AsyncMock(return_value=Row(scalar_one_or_none=lambda: 5)))
    result = await engine.run_once(db, Row())
    assert result.opened == 0
    engine.exchange.get_balance.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_fill_with_unknown_remainder_is_held_before_booking(monkeypatch):
    engine = TradingEngine()
    monkeypatch.setattr(engine.settings, "paper_trading", True)
    position = Position(id=5, symbol="ETH/USDT", side="LONG", volume=1, entry_context={})
    order = Order(id=9, status="PARTIAL", filled_amount=.2, average_price=100,
                  raw={"reconciliation_required": True})
    engine._cancel_active_protective_stop = AsyncMock(return_value=None)
    engine.execution.execute_market = AsyncMock(return_value=order)
    engine._record_partial_exit = AsyncMock()
    engine.control.panic = AsyncMock()
    engine.telegram.broadcast = AsyncMock()
    db = Row(commit=AsyncMock(), add=Mock())
    await engine._close_position(db, position, 100, "STOP_LOSS")
    engine._record_partial_exit.assert_not_awaited()
    assert position.entry_context["exit_failure"]["review_required"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["ENTRY", "EXIT_MANUAL", "PARTIAL_TAKE_PROFIT"])
async def test_execution_guard_prevents_bypass_by_other_callers(reason):
    from app.services.execution import ExecutionService
    exchange = Row(create_order=AsyncMock())
    service = ExecutionService(exchange)
    db = Row(execute=AsyncMock(return_value=Row(scalar_one_or_none=lambda: 5)), add=Mock())
    with pytest.raises(RuntimeError, match="exit reconciliation"):
        await service.execute_market(db, "ETH/USDT", "sell", 1, 100, reason)
    db.add.assert_not_called()
    exchange.create_order.assert_not_awaited()
