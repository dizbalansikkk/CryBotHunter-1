from types import SimpleNamespace
import ccxt
import pytest
from app.services import exchange_throttle as throttle


class Store:
    def __init__(self): self.values = {}
    def get(self, key): return self.values.get(key)
    def setex(self, key, ttl, value): self.values[key] = value
    def eval(self, script, count, key, deadline, ttl):
        self.values[key] = max(float(self.values.get(key, 0)), deadline)


def test_ban_deadline_respects_exchange_timestamp_and_retry_after():
    assert throttle.cooldown_until('IP banned until 1792192069587', {}, 100) == 1792192074.587
    assert throttle.cooldown_until('429', {'Retry-After': '300'}, 100) == 405


def test_one_clients_ban_stops_other_clients_without_http(monkeypatch):
    store = Store()
    monkeypatch.setattr(throttle, 'shared_store', lambda: store)
    monkeypatch.setattr(throttle.time, 'time', lambda: 100)
    calls = []
    def banned(*args):
        calls.append(1)
        raise ccxt.DDoSProtection('IP banned until 1792192069587')
    first = SimpleNamespace(fetch=banned)
    second = SimpleNamespace(fetch=lambda *args: calls.append(2))
    throttle.install_binance_guard(first)
    throttle.install_binance_guard(second)
    with pytest.raises(ccxt.DDoSProtection): first.fetch('https://fapi.binance.com/fapi/v1/klines')
    with pytest.raises(ccxt.RateLimitExceeded): second.fetch('https://fapi.binance.com/fapi/v1/klines')
    assert calls == [1]


def test_metadata_shared_but_market_prices_not_cached(monkeypatch):
    monkeypatch.setattr(throttle, 'shared_store', lambda: store)
    store = Store()
    calls = []
    def fetch(url, *args):
        calls.append(url)
        return {'symbols': []}
    for _ in range(2):
        client = SimpleNamespace(fetch=fetch)
        throttle.install_binance_guard(client)
        client.fetch('https://fapi.binance.com/fapi/v1/exchangeInfo')
        client.fetch('https://fapi.binance.com/fapi/v1/ticker/price')
    assert len(calls) == 3
