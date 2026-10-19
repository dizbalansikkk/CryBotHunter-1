from types import SimpleNamespace
from app.services.inventory_reconciliation import compare_inventory


def test_contract_size_and_side_are_reconciled_without_netting_hedges():
    local=[SimpleNamespace(symbol='ETH/USDT',side='LONG',volume=2)]
    venue=[dict(symbol='ETH/USDT:USDT',side='long',contracts=20,contractSize=.1)]
    assert compare_inventory(local,venue)==[]
    venue.append(dict(symbol='ETH/USDT:USDT',side='short',contracts=1,contractSize=1))
    assert 'SHORT' in compare_inventory(local,venue)[0]


def test_orphan_missing_and_invalid_positions_are_not_assumed_flat():
    local=[SimpleNamespace(symbol='ETH/USDT',side='LONG',volume=2)]
    assert compare_inventory(local,[])
    assert compare_inventory([], [dict(symbol='ETH/USDT',side='long',contracts=1,contractSize=1)])
    assert compare_inventory([], [dict(contracts=float('nan'))]) == ['invalid_venue_quantity']
    assert compare_inventory([], [dict(symbol='ETH/USDT',side='long',contracts=1)]) == ['incomplete_venue_position']


import pytest
from unittest.mock import AsyncMock
from app.services.trading_engine import TradingEngine
from app.models.entities import Order

@pytest.mark.asyncio
async def test_cancel_race_returns_existing_fill_instead_of_new_market_exit():
    engine = TradingEngine()
    order = Order(id=4, symbol='ETH/USDT', status='NEW', exchange_order_id='stop4', requested_amount=1)
    engine._active_protective_stop = AsyncMock(return_value=order)
    async def cancel(existing):
        existing.status='FILLED'
        existing.filled_amount=1
        existing.average_price=100
        return False
    engine.execution.cancel_protective_stop=cancel
    result=await engine._cancel_active_protective_stop(SimpleNamespace(), SimpleNamespace(), cycle_id=None)
    assert result is order
