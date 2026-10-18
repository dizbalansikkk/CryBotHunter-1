from types import SimpleNamespace
import ccxt
import pytest
from app.services import exchange_throttle as throttle


class Store:
    def __init__(self): self.values = {}
    def get(self, key): return self.values.get(key)
    def setex(self, key, ttl, value): self.values[key] = value
    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values: return False
        self.values[key] = value
        return True
    def eval(self, script, count, key, *args):
        if key == 'binance:rest:weight':
            now, cost, cap = args
            used = self.values.get(key, 0)
            if used + cost > cap: return 0
            self.values[key] = used + cost
            return 1
        if key.endswith(':loading'):
            if self.values.get(key) == args[0]: self.values.pop(key)
            return 1
        self.values[key] = max(float(self.values.get(key, 0)), args[0])
        return 1


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


def test_public_responses_shared_across_clients(monkeypatch):
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
    assert len(calls) == 2


def test_local_budget_does_not_trigger_ban_or_leave_lock(monkeypatch):
    store = Store()
    store.values['binance:rest:weight'] = 100000
    monkeypatch.setattr(throttle, 'shared_store', lambda: store)
    client = SimpleNamespace(fetch=lambda *args: pytest.fail('HTTP must not run'))
    throttle.install_binance_guard(client)
    with pytest.raises(ccxt.RateLimitExceeded, match='Local Binance'):
        client.fetch('https://fapi.binance.com/fapi/v1/klines')
    assert 'binance:cooldown:until' not in store.values
    assert not any(k.endswith(':loading') for k in store.values)


def test_signed_responses_never_cached(monkeypatch):
    store = Store()
    monkeypatch.setattr(throttle, 'shared_store', lambda: store)
    calls = []
    client = SimpleNamespace(fetch=lambda *args: calls.append(1) or {})
    throttle.install_binance_guard(client)
    for _ in range(2):
        client.fetch('https://fapi.binance.com/fapi/v2/balance?signature=secret')
    assert len(calls) == 2
    assert not any(k.startswith('binance:metadata:') for k in store.values)


def test_private_requests_have_reserved_capacity(monkeypatch):
    from app.services import request_budget
    store = Store()
    monkeypatch.setattr('app.core.config.get_settings', lambda: SimpleNamespace(
        binance_rest_public_budget=600, binance_rest_total_budget=900))
    store.values['binance:rest:weight'] = 600
    with pytest.raises(ccxt.RateLimitExceeded):
        request_budget.reserve_request(store, 'https://fapi.binance.com/fapi/v1/time', 'GET')
    request_budget.reserve_request(store, 'https://fapi.binance.com/fapi/v1/order', 'POST')
    assert store.values['binance:rest:weight'] == 700


def test_cache_coordination_failure_sends_no_http(monkeypatch):
    from redis.exceptions import ConnectionError
    store = Store()
    def get(key):
        if key.startswith('binance:metadata:'): raise ConnectionError('offline')
        return None
    store.get = get
    monkeypatch.setattr(throttle, 'shared_store', lambda: store)
    client = SimpleNamespace(fetch=lambda *args: pytest.fail('HTTP must not run'))
    throttle.install_binance_guard(client)
    with pytest.raises(ccxt.ExchangeNotAvailable):
        client.fetch('https://fapi.binance.com/fapi/v1/klines')
