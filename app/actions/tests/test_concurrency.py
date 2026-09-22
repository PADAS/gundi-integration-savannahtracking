import asyncio

import pytest

from app.actions.concurrency import ProviderSemaphore


class _FakeRedis:
    """Stand-in for the one Redis call the semaphore makes: EVAL of its acquire
    script. Implements the script's contract (evict stale holders, refuse when
    the set is at capacity, otherwise insert) over an in-memory sorted set.
    The real Lua is exercised in test_concurrency_redis.py."""

    def __init__(self):
        self.zsets = {}

    async def eval(self, script, numkeys, key, now, ttl, capacity, holder_id):
        assert numkeys == 1
        now, ttl, capacity = float(now), float(ttl), int(capacity)
        zset = self.zsets.setdefault(key, {})
        for member in [m for m, score in zset.items() if score <= now - ttl]:
            del zset[member]
        if len(zset) >= capacity:
            return 0
        zset[holder_id] = now
        return 1

    async def zrem(self, key, member):
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0


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


@pytest.mark.asyncio
async def test_a_late_arriving_older_acquisition_cannot_squeeze_past_the_cap(mocker):
    # Review on PR 10: the score is stamped client-side before Redis sees the
    # request. A request stamped 1000.00 that reaches Redis after one stamped
    # 1000.01 took the last slot must still be refused; ranking by score let
    # both keep their slots for the whole run.
    redis = _FakeRedis()
    semaphore = make_semaphore(redis, capacity=1)
    clock = mocker.patch("app.actions.concurrency.time.time")
    clock.return_value = 1_000.01
    assert await semaphore.acquire("api.example.com", "newer-request") is True

    clock.return_value = 1_000.00
    assert await semaphore.acquire("api.example.com", "older-request") is False

    (members,) = redis.zsets.values()
    assert set(members) == {"newer-request"}
