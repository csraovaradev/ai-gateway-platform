import asyncio
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from aigw.auth import hash_key
from aigw.config import GatewayConfig
from aigw.main import create_app
from aigw.store import MemoryStore, RedisStore

REDIS_URL = os.environ.get("AIGW_TEST_REDIS_URL")
needs_redis = pytest.mark.skipif(not REDIS_URL, reason="set AIGW_TEST_REDIS_URL to run")
KEY = "test-key"


def run(coro):
    return asyncio.run(coro)


def redis_store() -> RedisStore:
    # A fresh prefix per test keeps runs independent without flushing the database.
    return RedisStore(REDIS_URL or "", key_prefix=f"aigw-test:{uuid.uuid4().hex}:")


def test_memory_store_values_expire():
    now = [0.0]
    store = MemoryStore(clock=lambda: now[0])
    run(store.set("k", "v", ttl_seconds=10))
    assert run(store.get("k")) == "v"
    now[0] = 10
    assert run(store.get("k")) is None


def test_memory_store_incr_keeps_first_ttl():
    now = [0.0]
    store = MemoryStore(clock=lambda: now[0])
    assert run(store.incr("spend", 1.5, ttl_seconds=100)) == 1.5
    now[0] = 50
    assert run(store.incr("spend", 2.0, ttl_seconds=100)) == 3.5
    now[0] = 100  # expiry counts from the first increment, not the last
    assert run(store.incr("spend", 1.0, ttl_seconds=100)) == 1.0


@needs_redis
def test_redis_sliding_window():
    async def scenario():
        store = redis_store()
        results = [await store.hit("rl", 3, window_seconds=1) for _ in range(4)]
        await asyncio.sleep(1.05)
        return results, await store.hit("rl", 3, window_seconds=1)

    results, after_window = run(scenario())
    assert [allowed for allowed, _ in results] == [True, True, True, False]
    assert results[-1][1] == 1  # retry after ~1s
    assert after_window == (True, 0)


@needs_redis
def test_redis_values_and_incr():
    async def scenario():
        store = redis_store()
        await store.set("session", "data", ttl_seconds=60)
        got = await store.get("session")
        await store.delete("session")
        totals = [await store.incr("spend", 0.25, 60), await store.incr("spend", 0.5, 60)]
        return got, await store.get("session"), totals

    assert run(scenario()) == ("data", None, [0.25, 0.75])


def config(**store) -> GatewayConfig:
    return GatewayConfig.model_validate({
        "store": store,
        "models": [{"name": "mock-echo", "provider": "mock"}],
        "teams": [{"name": "t", "key_sha256": [hash_key(KEY)], "requests_per_minute": 3}],
    })


def chat(client: TestClient) -> int:
    return client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {KEY}"},
                       json={"model": "mock-echo",
                             "messages": [{"role": "user", "content": "hi"}]}).status_code


@needs_redis
def test_rate_limit_is_shared_across_replicas():
    prefix = f"aigw-test:{uuid.uuid4().hex}:"
    # Context managers keep each app on one event loop, as under uvicorn; the Redis
    # connection pool is bound to the loop that created it.
    with (TestClient(create_app(config(backend="redis", url=REDIS_URL, key_prefix=prefix)))
          as replica_a,
          TestClient(create_app(config(backend="redis", url=REDIS_URL, key_prefix=prefix)))
          as replica_b):
        codes = [chat(replica_a), chat(replica_b), chat(replica_a), chat(replica_b)]
    assert codes == [200, 200, 200, 429]


def test_unreachable_store_fails_closed():
    client = TestClient(create_app(config(backend="redis", url="redis://127.0.0.1:1/0")))
    resp = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {KEY}"},
                       json={"model": "mock-echo", "messages": [{"role": "user", "content": "x"}]})
    assert resp.status_code == 503
    assert resp.json()["error"]["type"] == "service_unavailable"
