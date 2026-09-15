import pytest
from redis.exceptions import RedisError

from app.services.control import TradingControlService
from app.services.locks import RedisLockManager


class UnavailableRedis:
    async def get(self, *_args, **_kwargs):
        raise RedisError("unavailable")

    async def set(self, *_args, **_kwargs):
        raise RedisError("unavailable")

    async def delete(self, *_args, **_kwargs):
        raise RedisError("unavailable")


class OwnedRedis:
    def __init__(self):
        self.value = None
        self.closed = False

    async def set(self, _key, value, *, nx, ex):
        assert nx
        assert ex >= 5
        if self.value is not None:
            return False
        self.value = value
        return True

    async def eval(self, script, _keys, _key, token, *_args):
        if self.value != token:
            return 0
        if 'redis.call("del"' in script:
            self.value = None
        return 1

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_control_fails_closed_when_redis_is_unavailable():
    control = TradingControlService()
    control.redis = UnavailableRedis()

    assert await control.is_paused() == (True, "redis_unavailable")
    assert await control.panic("test") is False
    assert await control.resume() is False


@pytest.mark.asyncio
async def test_distributed_lock_skips_operation_when_redis_is_unavailable():
    locks = RedisLockManager()
    locks.redis = UnavailableRedis()

    async with locks.lock("trader-worker-loop") as acquired:
        assert acquired is False


@pytest.mark.asyncio
async def test_distributed_lock_never_deletes_a_new_owners_lease():
    locks = RedisLockManager()
    redis = OwnedRedis()
    locks.redis = redis

    async with locks.lock("trading-cycle") as acquired:
        assert acquired is True
        original_token = redis.value
        assert original_token and original_token != "1"
        redis.value = "new-owner-token"

    assert redis.value == "new-owner-token"
    await locks.close()
    assert redis.closed
