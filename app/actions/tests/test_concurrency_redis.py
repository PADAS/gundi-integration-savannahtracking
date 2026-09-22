"""The acquire script against a real Redis, started on an ephemeral port for the
session. Skipped where no redis-server binary is available (CI); run locally
whenever the Lua changes, since test_concurrency.py's fake only mirrors the
script's contract."""
import asyncio
import shutil
import socket
import subprocess
import time

import pytest
import pytest_asyncio
import redis.asyncio as redis

from app.actions.concurrency import ProviderSemaphore

pytestmark = pytest.mark.skipif(shutil.which("redis-server") is None, reason="redis-server not installed")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def redis_server():
    port = _free_port()
    process = subprocess.Popen(
        ["redis-server", "--port", str(port), "--save", "", "--appendonly", "no", "--loglevel", "warning"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        else:
            raise RuntimeError("redis-server did not start")
        yield port
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest_asyncio.fixture
async def redis_client(redis_server):
    client = redis.Redis(host="127.0.0.1", port=redis_server, db=0)
    await client.flushdb()
    yield client
    await client.aclose()


def make_semaphore(client, capacity=1, **overrides):
    options = dict(
        capacity=capacity, holder_ttl_seconds=60, max_wait_seconds=0,
        poll_interval_seconds=(0.001, 0.002), redis_client=client,
    )
    options.update(overrides)
    return ProviderSemaphore(**options)


@pytest.mark.asyncio
async def test_script_admits_up_to_capacity_then_refuses(redis_client):
    semaphore = make_semaphore(redis_client, capacity=2)

    assert await semaphore.acquire("api.example.com", "a") is True
    assert await semaphore.acquire("api.example.com", "b") is True
    assert await semaphore.acquire("api.example.com", "c") is False
    assert await redis_client.zcard("provider_semaphore.api.example.com") == 2


@pytest.mark.asyncio
async def test_script_refuses_a_late_arriving_older_stamp(redis_client, mocker):
    semaphore = make_semaphore(redis_client, capacity=1)
    clock = mocker.patch("app.actions.concurrency.time.time")
    clock.return_value = 1_000.01
    assert await semaphore.acquire("api.example.com", "newer") is True

    clock.return_value = 1_000.00
    assert await semaphore.acquire("api.example.com", "older") is False
    assert await redis_client.zrange("provider_semaphore.api.example.com", 0, -1) == [b"newer"]


@pytest.mark.asyncio
async def test_script_evicts_stale_holders_and_sets_a_ttl_on_the_set(redis_client, mocker):
    semaphore = make_semaphore(redis_client, capacity=1, holder_ttl_seconds=60)
    clock = mocker.patch("app.actions.concurrency.time.time")
    clock.return_value = 1_000.0
    assert await semaphore.acquire("api.example.com", "crashed") is True

    clock.return_value = 1_061.0
    assert await semaphore.acquire("api.example.com", "next") is True
    assert await redis_client.zrange("provider_semaphore.api.example.com", 0, -1) == [b"next"]
    assert 0 < await redis_client.ttl("provider_semaphore.api.example.com") <= 60


@pytest.mark.asyncio
async def test_concurrent_acquirers_never_exceed_capacity(redis_client):
    semaphore = make_semaphore(redis_client, capacity=3)

    results = await asyncio.gather(*(semaphore.acquire("api.example.com", f"h{i}") for i in range(20)))

    assert sum(results) == 3
    assert await redis_client.zcard("provider_semaphore.api.example.com") == 3
