from types import SimpleNamespace as Row
from datetime import datetime, timedelta, timezone
import pytest
from app.services.stop_coverage import assess_stop_coverage, summarize_coverage


def position():
    return Row(volume=2, stop=90, side='LONG', entry_context={})


def order(**changes):
    raw=dict(status='open', amount=2, filled=0, stopPrice=90, reduceOnly=True, side='sell')
    return Row(id=1, raw={'exchange_response':{**raw, **changes}})


def test_only_fresh_complete_venue_evidence_counts_as_protection():
    assert assess_stop_coverage(position(),order(),True)['status']=='COVERED'
    assert assess_stop_coverage(position(),order(),False)['status']=='UNKNOWN'
    assert assess_stop_coverage(position(),None,False)['status']=='MISSING'

@pytest.mark.parametrize('change,reason',[
    ({'amount':1},'stop_quantity_differs_from_position'),
    ({'amount':3},'stop_quantity_differs_from_position'),
    ({'stopPrice':85},'stop_trigger_differs_from_plan'),
    ({'side':'buy'},'wrong_exit_side'),
    ({'reduceOnly':False},'reduce_only_not_confirmed'),
    ({'status':'canceled'},'stop_not_open')])
def test_mismatched_or_inactive_stop_never_appears_covered(change,reason):
    result=assess_stop_coverage(position(),order(**change),True)
    assert result['status']!='COVERED'
    assert result['reason']==reason


def test_remaining_volume_after_partial_fill_and_invalid_data():
    assert assess_stop_coverage(position(),order(amount=3,filled=1),True)['status']=='COVERED'
    assert assess_stop_coverage(position(),order(amount=float('nan')),True)['status']=='UNKNOWN'


def test_report_does_not_reuse_stale_protection_confirmation():
    now=datetime.now(timezone.utc)
    pos=position()
    pos.entry_context={'stop_coverage':{'status':'COVERED','checked_at':(now-timedelta(seconds=121)).isoformat()}}
    assert summarize_coverage([pos],now)['UNKNOWN']==1
    pos.entry_context['stop_coverage']['checked_at']=now.isoformat()
    assert summarize_coverage([pos],now)['COVERED']==1


@pytest.mark.asyncio
async def test_live_failure_pauses_entries_and_logs_only_on_transition():
    from unittest.mock import AsyncMock, Mock
    from app.services.trading_engine import TradingEngine
    engine=TradingEngine.__new__(TradingEngine)
    engine.settings=Row(paper_trading=False,live_trading_enabled=True)
    engine.exchange=Row(is_derivatives_market=lambda:True)
    engine.control=Row(panic=AsyncMock())
    engine._log_trading_event=Mock()
    pos=position()
    pos.id=7
    pos.symbol='ETH/USDT'
    await engine._audit_stop_coverage(None,pos,None,False)
    await engine._audit_stop_coverage(None,pos,None,False)
    assert engine._log_trading_event.call_count==1
    assert engine.control.panic.await_count==2
    assert pos.entry_context['stop_coverage']['status']=='MISSING'
    engine.settings.paper_trading=True
    engine.control.panic.reset_mock()
    await engine._audit_stop_coverage(None,pos,None,False)
    engine.control.panic.assert_not_awaited()
