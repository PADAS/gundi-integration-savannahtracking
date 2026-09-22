import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class ProviderSemaphore:
    """A counting semaphore shared by every runner instance through Redis.

    Cloud Run scales this service to several instances, each serving several
    requests, so an in-process semaphore cannot bound what the provider sees.
    Each scope (the provider host) is a sorted set of holder ids scored by
    acquisition time. A holder older than `holder_ttl_seconds` is treated as
    dead and evicted, so an instance that is killed mid-run frees its slot.

    Acquisition runs as one Lua script (eviction, capacity check and insert
    in a single atomic step), so two acquirers racing for the last slot
    cannot both keep it, whatever order their requests reach Redis in. The
    score is the acquirer's clock; runner instances are NTP-synced and the
    TTL is minutes, so skew does not matter for eviction.
    """

    def __init__(
        self,
        *,
        capacity: int,
        holder_ttl_seconds: int,
        max_wait_seconds: float,
        redis_client,
        poll_interval_seconds: Tuple[float, float] = (0.5, 2.0),
        key_prefix: str = "provider_semaphore",
    ):
        self.capacity = capacity
        self.holder_ttl_seconds = holder_ttl_seconds
        self.max_wait_seconds = max_wait_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.redis_client = redis_client
        self.key_prefix = key_prefix

    def _key(self, scope: str) -> str:
        return f"{self.key_prefix}.{scope}"

    # KEYS[1] = scope set; ARGV = now, holder ttl, capacity, holder id.
    # Returns 1 when the holder took a slot, 0 when the scope was at capacity.
    ACQUIRE_SCRIPT = """
        local now = tonumber(ARGV[1])
        local ttl = tonumber(ARGV[2])
        local capacity = tonumber(ARGV[3])
        redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - ttl)
        if redis.call('ZCARD', KEYS[1]) >= capacity then
            return 0
        end
        redis.call('ZADD', KEYS[1], now, ARGV[4])
        -- The set as a whole outlives its oldest possible live holder, so an
        -- idle scope does not linger in Redis forever.
        redis.call('EXPIRE', KEYS[1], ttl)
        return 1
    """

    async def _try_acquire(self, scope: str, holder_id: str) -> bool:
        admitted = await self.redis_client.eval(
            self.ACQUIRE_SCRIPT, 1, self._key(scope),
            time.time(), self.holder_ttl_seconds, self.capacity, holder_id,
        )
        return bool(int(admitted))

    async def acquire(self, scope: str, holder_id: str, *, max_wait_seconds: Optional[float] = None) -> bool:
        """Take a slot in `scope`, waiting up to `max_wait_seconds` (the
        instance default when None). Returns False when none freed in time."""
        wait_budget = self.max_wait_seconds if max_wait_seconds is None else max_wait_seconds
        deadline = time.monotonic() + wait_budget
        while True:
            if await self._try_acquire(scope, holder_id):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(remaining, random.uniform(*self.poll_interval_seconds)))

    async def release(self, scope: str, holder_id: str) -> None:
        await self.redis_client.zrem(self._key(scope), holder_id)

    @asynccontextmanager
    async def slot(self, scope: str, holder_id: str):
        """Yield True holding a slot (released on exit), or False when the
        scope stayed full for the whole wait, in which case nothing is held."""
        acquired = await self.acquire(scope, holder_id)
        try:
            yield acquired
        finally:
            if acquired:
                await self.release(scope, holder_id)
