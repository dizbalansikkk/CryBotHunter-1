"""One public USDT futures collector; consumers accept only fresh snapshots."""
import asyncio
import json
import logging
import math
import random
import time
from contextlib import suppress

import aiohttp
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.services.locks import RedisLockManager

logger = logging.getLogger(__name__)
PREFIX = 'binance:usdt:public:stream:'


def normalize_event(data, now_ms):
    """Partial depth snapshots need no diff reconstruction; reject delayed data."""
    timestamp = int(data.get('E', 0))
    if not 0 <= now_ms - timestamp <= 5000:
        return None
    symbol = str(data.get('s', ''))
    if not symbol.endswith('USDT'):
        return None
    symbol = symbol[:-4] + '/USDT'
    event = data.get('e')
    if event == '24hrTicker':
        payload = {'symbol': symbol, 'timestamp': timestamp, 'last': float(data['c']),
            'percentage': float(data['P']), 'baseVolume': float(data['v']),
            'quoteVolume': float(data['q']), 'high': float(data['h']), 'low': float(data['l'])}
        kind = 'ticker'
    elif event == 'depthUpdate':
        payload = {'symbol': symbol, 'timestamp': timestamp,
            'bids': [[float(p), float(q)] for p, q in data['b']],
            'asks': [[float(p), float(q)] for p, q in data['a']], 'nonce': data.get('u')}
        if not payload['bids'] or not payload['asks']:
            return None
        if payload['bids'][0][0] >= payload['asks'][0][0]:
            return None
        kind = 'book'
    else:
        return None
    numbers = ([payload['last'], payload['high'], payload['low'], payload['baseVolume'], payload['quoteVolume']]
               if kind == 'ticker' else [value for side in ('bids', 'asks') for level in payload[side] for value in level])
    if not all(math.isfinite(n) and n >= 0 for n in numbers):
        return None
    if kind == 'ticker' and (payload['last'] <= 0 or not math.isfinite(payload['percentage'])):
        return None
    if kind == 'book' and any(p <= 0 or q <= 0 for side in ('bids', 'asks') for p, q in payload[side]):
        return None
    return kind, symbol, payload


def read_snapshots(kind, symbols):
    from app.services.exchange_throttle import shared_store
    try:
        values = shared_store().mget([PREFIX + kind + ':' + symbol for symbol in symbols])
        now = time.time() * 1000
        result = {}
        for symbol, value in zip(symbols, values):
            if value:
                payload = json.loads(value)
                if 0 <= now - payload['timestamp'] <= 5000:
                    result[symbol] = payload
        return result
    except (RedisError, ValueError, KeyError, TypeError):
        return {}


async def _listen(redis, route, streams):
    delay = 5
    while True:
        try:
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
            async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=None, connect=20)) as session:
                url = f'wss://fstream.binance.com/{route}/stream?streams=' + '/'.join(streams)
                async with session.ws_connect(url, heartbeat=30, receive_timeout=45, max_msg_size=2**20) as ws:
                    connected = time.monotonic()
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(message.data).get('data', {})
                            try:
                                normalized = normalize_event(data, int(time.time() * 1000))
                            except (ValueError, KeyError, TypeError):
                                normalized = None
                            if normalized:
                                kind, symbol, payload = normalized
                                await redis.set(PREFIX + kind + ':' + symbol, json.dumps(payload), ex=5)
                                await redis.set(PREFIX + 'health:' + route, str(time.time()), ex=60)
                            if time.monotonic() - connected > 60:
                                delay = 5
                        elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('Market stream %s disconnected: %s; backoff=%ss', route, type(exc).__name__, delay)
        await asyncio.sleep(delay + random.uniform(0, 3))
        delay = min(delay * 2, 300)


async def run_market_collector():
    settings = get_settings()
    if not (settings.binance_market_stream_enabled and settings.default_exchange == 'binance'
            and settings.exchange_default_type in {'future', 'futures', 'swap'} and settings.uses_live_market_data):
        return
    symbols = list(dict.fromkeys(settings.candle_ingest_symbols + settings.market_scan_symbols))[:100]
    if not symbols:
        return
    ids = [s.replace('/', '').lower() for s in symbols if s.endswith('/USDT')]
    if not ids:
        return
    locks = RedisLockManager()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        while True:
            async with locks.lock('binance-market-collector', ttl_seconds=60) as acquired:
                if acquired:
                    tasks = [asyncio.create_task(_listen(redis, 'market', [s + '@ticker' for s in ids])),
                             asyncio.create_task(_listen(redis, 'public', [s + '@depth20@500ms' for s in ids]))]
                    try:
                        await asyncio.gather(*tasks)
                    finally:
                        for task in tasks:
                            task.cancel()
                        for task in tasks:
                            with suppress(asyncio.CancelledError):
                                await task
            await asyncio.sleep(30)
    finally:
        await redis.aclose()
        await locks.close()
