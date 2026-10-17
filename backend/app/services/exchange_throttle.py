"""Shared Binance cooldown at the HTTP boundary, including metadata requests."""
import json
import math
import re
import time
from functools import lru_cache
from hashlib import sha256

import ccxt
from redis import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings


@lru_cache(maxsize=1)
def shared_store():
    return Redis.from_url(get_settings().redis_url, decode_responses=True,
                          socket_connect_timeout=2, socket_timeout=2)


def cooldown_until(error, headers, now):
    match = re.search(r"banned until\s+(\d{13})", str(error), re.I)
    deadline = float(match.group(1)) / 1000 if match else now + 120
    try:
        retry = float(next((v for k, v in (headers or {}).items() if k.lower() == "retry-after"), 0))
        if math.isfinite(retry):
            deadline = max(deadline, now + retry)
    except (TypeError, ValueError):
        pass
    return max(deadline, now + 60) + 5


def install_binance_guard(client):
    original = client.fetch
    local_until = 0.0

    def guarded(url, method="GET", headers=None, body=None):
        nonlocal local_until
        now = time.time()
        store = shared_store()
        try:
            deadline = max(local_until, float(store.get("binance:cooldown:until") or 0))
        except RedisError as exc:
            # Do not resume independently when the shared coordination is down.
            raise ccxt.ExchangeNotAvailable("Binance request coordination unavailable") from exc
        if deadline > now:
            raise ccxt.RateLimitExceeded(f"Binance cooldown active until {int(deadline * 1000)}; no HTTP request sent")
        # Metadata is public and unchanged between adjacent scan cycles.
        cache_key = None
        if method == "GET" and "exchangeInfo" in url and not body:
            cache_key = "binance:metadata:" + sha256(url.encode()).hexdigest()
            cached = store.get(cache_key)
            if cached:
                return json.loads(cached)
        try:
            result = original(url, method, headers, body)
        except (ccxt.DDoSProtection, ccxt.RateLimitExceeded) as exc:
            local_until = cooldown_until(exc, getattr(client, "last_response_headers", {}), time.time())
            script = """local old = tonumber(redis.call('get', KEYS[1]) or '0')
            local new = tonumber(ARGV[1])
            if new > old then redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[2]) end
            return 1"""
            try:
                store.eval(script, 1, "binance:cooldown:until", local_until,
                           max(math.ceil(local_until - time.time()), 1))
            except RedisError:
                pass
            raise
        if cache_key:
            try:
                store.setex(cache_key, 1800, json.dumps(result))
            except RedisError:
                pass
        return result

    client.fetch = guarded
