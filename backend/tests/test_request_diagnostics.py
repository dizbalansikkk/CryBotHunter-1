from types import SimpleNamespace
from redis.exceptions import ConnectionError
from app.services import request_diagnostics as diagnostics

class Store:
    def pipeline(self, **kw): return self
    def hmget(self, key, seconds):
        assert len(seconds)==60
        return self
    def mget(self, keys): return self
    def execute(self): return ([10,20,None], [1100,999,990])

def setup(monkeypatch):
    monkeypatch.setattr(diagnostics.time, 'time', lambda:1000)
    monkeypatch.setattr(diagnostics,'get_settings',lambda:SimpleNamespace(
        binance_rest_public_budget=600,binance_rest_total_budget=900,binance_market_stream_enabled=True))

def test_shared_budget_and_stream_freshness_without_exchange(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(diagnostics,'shared_store',lambda:Store())
    result=diagnostics.request_diagnostics()
    assert result['used_weight']==30
    assert result['public_remaining']==570
    assert result['total_remaining']==870
    assert result['cooldown_seconds']==101
    assert [r['fresh'] for r in result['streams']]==[True,False]

def test_unavailable_redis_is_not_zero_usage(monkeypatch):
    setup(monkeypatch)
    def fail(): raise ConnectionError('offline')
    monkeypatch.setattr(diagnostics,'shared_store',fail)
    result=diagnostics.request_diagnostics()
    assert result['available'] is False
    assert 'used_weight' not in result
