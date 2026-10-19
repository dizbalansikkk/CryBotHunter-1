from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from sqlalchemy.dialects import postgresql
from app.services.history import HistoricalDataService
from app.services.pretrade_quality import PreTradeQualityGate

class Db:
    async def execute(self, statement):
        self.statement=statement
        return SimpleNamespace(scalars=lambda:SimpleNamespace(all=lambda:[]))

@pytest.mark.asyncio
async def test_closed_history_filters_before_limit_and_preserves_source():
    db=Db()
    before=datetime.now(timezone.utc)
    await HistoricalDataService().load(db,'ETH/USDT','1h',limit=250,source='ccxt',closed_only=True)
    query=db.statement.compile(dialect=postgresql.dialect())
    assert 'candles.timestamp <=' in str(query)
    assert 'candles.source =' in str(query)
    assert 'ccxt' in query.params.values()
    cutoff=next(v for v in query.params.values() if isinstance(v,datetime))
    assert 3599 <= (before-cutoff).total_seconds() <= 3601

@pytest.mark.asyncio
@pytest.mark.parametrize('live,source',[(True,'ccxt'),(False,'synthetic')])
async def test_pretrade_history_never_mixes_real_and_synthetic(live,source):
    gate=PreTradeQualityGate()
    gate.settings=SimpleNamespace(uses_live_market_data=live)
    db=Db()
    await gate._recent_candles(db,'ETH/USDT','1h',500)
    query=db.statement.compile(dialect=postgresql.dialect())
    assert 'candles.source =' in str(query)
    assert source in query.params.values()
    assert 'candles.timestamp <=' in str(query)
