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


def test_exit_ledger_replaces_cumulative_acknowledgements_without_duplicates():
    engine = TradingEngine()
    position = SimpleNamespace(entry_context={"exit_fill_ledger_version": 1})
    order = SimpleNamespace(status='PARTIAL', filled_amount=.2, average_price=102,
        fee=.02, slippage=.1, id=7, exchange_order_id='ex7', updated_at=datetime.now(timezone.utc))
    engine._record_exit_fill(position, order)
    engine._record_exit_fill(position, order)
    assert len(position.entry_context['exit_fills']) == 1
    order.filled_amount = .4
    order.fee = .04
    engine._record_exit_fill(position, order)
    assert position.entry_context['exit_fills'][0]['volume'] == .4
    order.filled_amount = .1
    engine._record_exit_fill(position, order)
    assert position.entry_context['exit_fills'][0]['volume'] == .4


def test_postmortem_includes_partial_costs_without_double_counting_final():
    from app.services.post_mortem import PostMortemService
    from app.models.entities import Order
    service = PostMortemService(SimpleNamespace())
    context = {'entry_execution': {'fee': .2, 'slippage': .1, 'volume': 2},
        'exit_fill_ledger_version': 1,
        'exit_fills': [{'order_id': 3, 'fee': .1, 'slippage': .1, 'volume': 1},
                       {'order_id': 4, 'fee': .2, 'slippage': .1, 'volume': 1}]}
    report = service.execution_snapshot(context, Order(id=4, fee=.2, slippage=.1, filled_amount=1), 1, 2)
    assert report['total_cost'] == pytest.approx(.9)
    assert report['exit_fee'] == pytest.approx(.3)
    assert report['partial_exit_fees'] == pytest.approx(.1)
    assert report['ledger_complete_from_entry'] is True
    assert report['pnl_adjustment_applied'] is False
    assert service.execution_snapshot({}, Order(id=9, fee=0, slippage=0, filled_amount=1), 1, 2)['ledger_complete_from_entry'] is False
