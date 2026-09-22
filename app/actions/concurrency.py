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

    Acquisition is the ZADD-then-ZRANK pattern: the holder is added, and keeps
    the slot if it ranks inside `capacity`, else removes itself. Two acquirers
    racing at the exact same timestamp can both rank inside the cap for a
    moment, which is an acceptable overshoot for a soft limit on a provider
    that advertises none.
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

    async def _try_acquire(self, scope: str, holder_id: str) -> bool:
        key = self._key(scope)
        now = time.time()
        pipe = self.redis_client.pipeline()
        pipe.zremrangebyscore(key, "-inf", now - self.holder_ttl_seconds)
        pipe.zadd(key, {holder_id: now})
        pipe.zrank(key, holder_id)
        # The set as a whole outlives its oldest possible live holder, so an
        # idle scope does not linger in Redis forever.
        pipe.expire(key, self.holder_ttl_seconds)
        _, _, rank, _ = await pipe.execute()
        if rank is not None and rank < self.capacity:
            return True
        await self.redis_client.zrem(key, holder_id)
        return False

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
