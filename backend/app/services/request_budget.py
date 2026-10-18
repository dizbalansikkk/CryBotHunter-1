"""Conservative shared rolling-minute REST admission, with an execution reserve."""
from urllib.parse import urlparse, parse_qs
import time
import ccxt


RESERVE = """
local now = tonumber(ARGV[1])
local cost = tonumber(ARGV[2])
local cap = tonumber(ARGV[3])
local total = 0
for second=now-59,now do
  total = total + tonumber(redis.call('hget', KEYS[1], tostring(second)) or '0')
end
if total + cost > cap then return 0 end
redis.call('hincrby', KEYS[1], tostring(now), cost)
for _, second in ipairs(redis.call('hkeys', KEYS[1])) do
  if tonumber(second) < now-59 then redis.call('hdel', KEYS[1], second) end
end
redis.call('expire', KEYS[1], 120)
return 1
"""


def request_policy(url, method, headers=None):
    parsed = urlparse(url)
    path, query = parsed.path, parse_qs(parsed.query)
    private = method != "GET" or "signature" in query or any(
        k.lower() == "x-mbx-apikey" for k in (headers or {}))
    try:
        limit = int(query.get("limit", [500])[0])
    except ValueError:
        limit = 1500
    ttl = 0
    # Upper bounds for the public endpoints used here; unknown endpoints are
    # deliberately expensive. This is a local budget, not a Binance limit.
    cost = 100
    if path.endswith('/exchangeInfo'):
        cost, ttl = 20, 1800
    elif path.endswith('/klines'):
        cost, ttl = (2 if limit < 100 else 5 if limit <= 500 else 10 if limit <= 1000 else 20), 30
    elif '/ticker/' in path:
        cost, ttl = (80 if 'symbol' not in query else 5), 2
    elif path.endswith('/depth'):
        cost, ttl = (5 if limit <= 50 else 10 if limit <= 100 else 20 if limit <= 500 else 50), 1
    elif path.endswith('/trades') or path.endswith('/aggTrades'):
        cost, ttl = 25, 2
    elif path.endswith('/time'):
        cost, ttl = 1, 0
    elif path.endswith('/premiumIndex') or path.endswith('/openInterest'):
        cost, ttl = 10, 30
    return cost, 0 if private else ttl, private


def reserve_request(store, url, method, headers=None):
    from app.core.config import get_settings
    settings = get_settings()
    cost, _, private = request_policy(url, method, headers)
    cap = settings.binance_rest_total_budget if private else settings.binance_rest_public_budget
    cap = min(cap, settings.binance_rest_total_budget)
    if not store.eval(RESERVE, 1, 'binance:rest:weight', int(time.time()), cost, cap):
        raise ccxt.RateLimitExceeded('Local Binance request budget exhausted; no HTTP request sent')
