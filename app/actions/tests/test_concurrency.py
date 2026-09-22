import asyncio

import pytest

from app.actions.concurrency import ProviderSemaphore


class _FakePipeline:
    def __init__(self, store):
        self._store = store
        self._ops = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self._ops.append((name, args, kwargs))
            return self
        return queue

    async def execute(self):
        return [await getattr(self._store, name)(*args, **kwargs) for name, args, kwargs in self._ops]


class _FakeRedis:
    """The five sorted-set commands the semaphore relies on, with real semantics."""

    def __init__(self):
        self.zsets = {}

    def pipeline(self):
        return _FakePipeline(self)

    def _ordered(self, key):
        return sorted(self.zsets.get(key, {}).items(), key=lambda item: (item[1], item[0]))

    async def zremrangebyscore(self, key, min_score, max_score):
        low = float("-inf") if min_score == "-inf" else float(min_score)
        high = float("inf") if max_score == "+inf" else float(max_score)
        zset = self.zsets.get(key, {})
        stale = [m for m, s in zset.items() if low <= s <= high]
        for m in stale:
            del zset[m]
        return len(stale)

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def zrank(self, key, member):
        for rank, (m, _) in enumerate(self._ordered(key)):
            if m == member:
                return rank
        return None

    async def zrem(self, key, member):
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0

    async def expire(self, key, seconds):
        return True


def make_semaphore(redis, capacity=2, **overrides):
    options = dict(
        capacity=capacity,
        holder_ttl_seconds=60,
        max_wait_seconds=0,
        poll_interval_seconds=(0.001, 0.002),
        redis_client=redis,
    )
    options.update(overrides)
    return ProviderSemaphore(**options)


@pytest.mark.asyncio
async def test_admits_holders_up_to_capacity_and_rejects_the_next():
    semaphore = make_semaphore(_FakeRedis(), capacity=2)

    assert await semaphore.acquire("api.example.com", "collar-1") is True
    assert await semaphore.acquire("api.example.com", "collar-2") is True
    assert await semaphore.acquire("api.example.com", "collar-3") is False


@pytest.mark.asyncio
async def test_rejected_acquirer_leaves_no_trace_in_the_slot_set():
    redis = _FakeRedis()
    semaphore = make_semaphore(redis, capacity=1)
    await semaphore.acquire("api.example.com", "collar-1")

    await semaphore.acquire("api.example.com", "collar-2")

    (members,) = redis.zsets.values()
    assert set(members) == {"collar-1"}


@pytest.mark.asyncio
async def test_release_frees_a_slot_for_the_next_acquirer():
    semaphore = make_semaphore(_FakeRedis(), capacity=1)
    await semaphore.acquire("api.example.com", "collar-1")

    await semaphore.release("api.example.com", "collar-1")

    assert await semaphore.acquire("api.example.com", "collar-2") is True


@pytest.mark.asyncio
async def test_scopes_are_independent():
    semaphore = make_semaphore(_FakeRedis(), capacity=1)
    await semaphore.acquire("api.example.com", "collar-1")

    assert await semaphore.acquire("api.other.com", "collar-1") is True


@pytest.mark.asyncio
async def test_stale_holders_are_evicted_so_a_dead_runner_frees_its_slot(mocker):
    redis = _FakeRedis()
    semaphore = make_semaphore(redis, capacity=1, holder_ttl_seconds=60)
    clock = mocker.patch("app.actions.concurrency.time.time")
    clock.return_value = 1_000.0
    await semaphore.acquire("api.example.com", "crashed-holder")

    clock.return_value = 1_000.0 + 61
    assert await semaphore.acquire("api.example.com", "collar-2") is True


@pytest.mark.asyncio
async def test_waits_for_a_slot_released_during_the_wait():
    semaphore = make_semaphore(_FakeRedis(), capacity=1, max_wait_seconds=1.0)
    await semaphore.acquire("api.example.com", "collar-1")

    async def release_soon():
        await asyncio.sleep(0.02)
        await semaphore.release("api.example.com", "collar-1")

    releaser = asyncio.ensure_future(release_soon())
    acquired = await semaphore.acquire("api.example.com", "collar-2")
    await releaser

    assert acquired is True


@pytest.mark.asyncio
async def test_gives_up_after_the_maximum_wait():
    semaphore = make_semaphore(_FakeRedis(), capacity=1, max_wait_seconds=0.02)
    await semaphore.acquire("api.example.com", "collar-1")

    assert await semaphore.acquire("api.example.com", "collar-2") is False


@pytest.mark.asyncio
async def test_slot_context_manager_releases_even_when_the_body_raises():
    semaphore = make_semaphore(_FakeRedis(), capacity=1)

    with pytest.raises(RuntimeError):
        async with semaphore.slot("api.example.com", "collar-1") as acquired:
            assert acquired is True
            raise RuntimeError("boom")

    assert await semaphore.acquire("api.example.com", "collar-2") is True


@pytest.mark.asyncio
async def test_slot_context_manager_yields_false_and_releases_nothing_when_full():
    semaphore = make_semaphore(_FakeRedis(), capacity=1)
    await semaphore.acquire("api.example.com", "collar-1")

    async with semaphore.slot("api.example.com", "collar-2") as acquired:
        assert acquired is False

    # collar-1's slot must still be held: the loser must not release it
    assert await semaphore.acquire("api.example.com", "collar-3") is False
