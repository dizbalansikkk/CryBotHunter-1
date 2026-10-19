import pytest
from types import SimpleNamespace
from datetime import datetime, timezone
from app.services.trade_costs import estimate_costs
from app.services.trading_engine import TradingEngine


def costs(**changes):
    args = dict(price=100, stop=98, take=104, volume=1, fee_rate=.001,
                spread_bps=2, slippage_bps=3, funding_rate=.0001)
    return estimate_costs(**{**args, **changes})


def test_round_trip_costs_use_exit_notional_and_do_not_book_funding():
    result = costs()
    assert result['total_cost_before_funding'] == pytest.approx(.1+.104+.02+.0612)
    assert result['loss_at_stop_with_costs'] == pytest.approx(2+.1+.098+.02+.0594)
    assert result['one_adverse_funding_payment'] == pytest.approx(.01)
    assert result['net_target_funding_stress'] == pytest.approx(result['net_target_before_funding']-.01)


def test_short_exit_fees_and_invalid_inputs():
    assert costs(stop=102,take=96)['target_exit_fee'] == pytest.approx(.096)
    assert costs(price=float('nan'))['available'] is False
    assert costs(volume=0)['available'] is False
    assert costs(take=100.01)['net_target_before_funding'] < 0


@pytest.mark.parametrize('status,amount,price,confirmed',[
    ('FILLED',1,100,True), ('PARTIAL',.2,100,True),
    ('NEW',0,0,False), ('FAILED',1,100,False), ('FILLED',0,100,False),
    ('FILLED',1,float('nan'),False)])
def test_fill_audit_requires_execution_evidence(status,amount,price,confirmed):
    engine=TradingEngine()
    order=SimpleNamespace(status=status,filled_amount=amount,average_price=price,
        fee=.01,id=1,exchange_order_id='abc',updated_at=datetime.now(timezone.utc))
    audit=engine._fill_audit(order,'TP1')
    assert audit['fill_confirmed'] is confirmed
    assert bool(audit['executed_at']) is confirmed
