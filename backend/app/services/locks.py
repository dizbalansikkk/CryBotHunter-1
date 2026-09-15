import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress
from collections.abc import AsyncGenerator
import logging
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings

logger = logging.getLogger(__name__)

TRADING_CYCLE_LOCK = "trading-cycle"

_RENEW_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


class RedisLockManager:
    def __init__(self) -> None:
        self.redis = Redis.from_url(get_settings().redis_url, decode_responses=True)

    @asynccontextmanager
    async def lock(self, name: str, ttl_seconds: int = 55) -> AsyncGenerator[bool, None]:
        key = f"lock:{name}"
        token = uuid4().hex
        ttl = max(int(ttl_seconds), 5)
        acquired = False
        renewal_task: asyncio.Task | None = None
        try:
            acquired = bool(await self.redis.set(key, token, nx=True, ex=ttl))
        except RedisError:
            logger.exception("Redis lock unavailable, skipping protected operation: %s", name)
        if acquired:
            renewal_task = asyncio.create_task(
                self._renew_lease(key, token, ttl, asyncio.current_task()),
                name=f"redis-lock-renew:{name}",
            )
        try:
            yield acquired
        finally:
            if renewal_task is not None:
                renewal_task.cancel()
                with suppress(asyncio.CancelledError):
                    await renewal_task
            if acquired:
                try:
                    released = await self.redis.eval(_RELEASE_SCRIPT, 1, key, token)
                    if not released:
                        logger.warning("Redis lock ownership changed before release: %s", name)
                except RedisError:
                    logger.exception("Redis lock release failed; TTL will expire the lock: %s", name)

    async def close(self) -> None:
        close = getattr(self.redis, "aclose", None)
        if callable(close):
            await close()

    async def _renew_lease(
        self,
        key: str,
        token: str,
        ttl_seconds: int,
        owner_task: asyncio.Task | None,
    ) -> None:
        interval = max(min(float(ttl_seconds) / 3, 30.0), 1.0)
        lease_deadline = asyncio.get_running_loop().time() + ttl_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self.redis.eval(
                    _RENEW_SCRIPT,
                    1,
                    key,
                    token,
                    ttl_seconds,
                )
            except RedisError:
                logger.exception("Redis lock renewal failed: %s", key)
                if asyncio.get_running_loop().time() + interval < lease_deadline:
                    continue
                renewed = 0
            if renewed:
                lease_deadline = asyncio.get_running_loop().time() + ttl_seconds
                continue
            logger.error("Redis lock lease lost; cancelling protected operation: %s", key)
            if owner_task is not None and not owner_task.done():
                owner_task.cancel()
            return
