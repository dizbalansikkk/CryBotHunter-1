from types import SimpleNamespace as Row
from unittest.mock import AsyncMock, Mock
import pytest
from app.services.execution import ExecutionService
from app.services.exchange import PreparedOrder
from app.services.exit_experiment import projection, compliant


@pytest.mark.parametrize("side,direction", [("LONG", 1), ("SHORT", -1)])
def test_fixed_limits_include_fees_and_adverse_slippage(side, direction):
    plan = projection(side, 100, 1, 100-direction*.39, 100+direction*.8, .0004, 2)
    assert .39 < plan["loss"] <= .5
    assert .6 < plan["profit"] < .8
    assert compliant(plan)
    assert not compliant({"loss": .50001, "profit": 1})
    assert not compliant({"loss": .4, "profit": .59999})


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "live", "round_up", "fee", "profit", "averaging", "valid"])
async def test_execution_gate_rechecks_plan_after_venue_normalization(case):
    service = ExecutionService()
    service.settings = service.settings.model_copy(update={"experimental_exit_limits_enabled": True,
        "paper_trading": case != "live", "paper_fee_rate": .0004, "paper_slippage_bps": 2})
    service.exchange = Row(is_derivatives_market=lambda: True,
        prepare_derivatives_symbol=AsyncMock(return_value={}),
        prepare_order=AsyncMock(return_value=PreparedOrder(amount=2 if case == "round_up" else 1,
            fee_rate=.01 if case == "fee" else .0004, min_amount=None, min_cost=None, metadata_available=True)),
        create_order=AsyncMock(side_effect=AssertionError("Live forbidden")))
    db = Row(execute=AsyncMock(return_value=Row(scalar_one_or_none=lambda: None)), add=Mock(), flush=AsyncMock())
    if case == "averaging":
        db.execute.side_effect = [Row(scalar_one_or_none=lambda: None), Row(scalar_one_or_none=lambda: 7)]
    plan = None if case == "missing" else {"stop": 99.62, "take": 100.5 if case == "profit" else 100.82, "slippage_bps": 2}
    order = await service.execute_market(db, "ETH/USDT", "buy", 1, 100, "ENTRY", experiment_plan=plan)
    assert order.status == ("FILLED" if case == "valid" else "FAILED")
    service.exchange.create_order.assert_not_awaited()
