from datetime import datetime, timedelta, timezone
import json
import pytest
from app.services.market_stream import normalize_event, read_snapshots
from app.services.history import HistoricalDataService

NOW = 100000

def ticker():
    return dict(E=NOW, s='BTCUSDT', e='24hrTicker', c='100', P='-2', v='3', q='300', h='105', l='95')

def test_ticker_accepts_negative_change_but_rejects_delayed_or_invalid_price():
    assert normalize_event(ticker(), NOW)[2]['percentage'] == -2
    assert normalize_event(ticker(), NOW + 5001) is None
    assert normalize_event({**ticker(), 'c': 'nan'}, NOW) is None
    assert normalize_event({**ticker(), 'P': 'nan'}, NOW) is None
    assert normalize_event({**ticker(), 'c': '0'}, NOW) is None

def test_partial_depth_rejects_crossed_empty_or_zero_book():
    data = dict(E=NOW, s='BTCUSDT', e='depthUpdate', b=[['99','1']], a=[['101','2']], u=123)
    assert normalize_event(data, NOW)[2]['bids'] == [[99,1]]
    assert normalize_event({**data, 'a': []}, NOW) is None
    assert normalize_event({**data, 'a': [['98','1']]}, NOW) is None
    assert normalize_event({**data, 'b': [['99','0']]}, NOW) is None

def test_stale_redis_snapshot_not_returned(monkeypatch):
    from app.services import exchange_throttle, market_stream
    class Store:
        def mget(self, keys): return [json.dumps({'timestamp': NOW})]
    monkeypatch.setattr(exchange_throttle, 'shared_store', lambda: Store())
    monkeypatch.setattr(market_stream.time, 'time', lambda: (NOW + 5001) / 1000)
    assert read_snapshots('ticker', ['BTC/USDT']) == {}

@pytest.mark.asyncio
async def test_ticker_uses_fresh_stream_and_book_without_http(monkeypatch):
    from app.services import market_stream
    from app.services.exchange import ExchangeClient
    from types import SimpleNamespace
    client = ExchangeClient()
    client.settings = SimpleNamespace(exchange_default_type='future', binance_market_stream_enabled=True)
    client.exchange = 'binance'
    client._client = lambda **kw: pytest.fail('HTTP should not be needed')
    def snapshots(kind, symbols):
        return {'BTC/USDT': {'last': 100} if kind == 'ticker' else {'bids': [[99,1]], 'asks': [[101,1]]}}
    monkeypatch.setattr(market_stream, 'read_snapshots', snapshots)
    result = await client.fetch_tickers(['BTC/USDT'])
    assert result['BTC/USDT'] == {'last':100, 'bid':99, 'ask':101}

def test_incremental_candles_refresh_previous_bar_and_backfill_after_gap():
    service = HistoricalDataService()
    now = datetime(2026,10,18,12,10,tzinfo=timezone.utc)
    assert service.incremental_limit('1h',500,500,now.replace(minute=0),now) == 0
    assert service.incremental_limit('1h',500,500,now-timedelta(hours=1),now) == 3
    assert service.incremental_limit('1h',500,500,now-timedelta(hours=600),now) == 500
    assert service.incremental_limit('1h',500,10,now,now) == 500
