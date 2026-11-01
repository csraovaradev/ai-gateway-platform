"""Shared state for rate limits, MCP session owners, and spend (ADR-0003).

`MemoryStore` keeps state per replica (the default). `RedisStore` shares it across replicas; its
rate-limit check is one Lua script using Redis's clock, so it is atomic and immune to replica skew.
"""

from __future__ import annotations

import math
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any, Protocol


class StoreError(Exception):
    """The shared store is unreachable. The gateway fails closed."""


class Store(Protocol):
    async def hit(self, key: str, limit: int, window_seconds: float) -> tuple[bool, int]:
        """Record a hit if under `limit` in the window. Returns (allowed, retry_after_seconds)."""
        ...

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl_seconds: int) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def incr(self, key: str, amount: float, ttl_seconds: int) -> float:
        """Add `amount` and return the new total. The TTL is set when the key is created."""
        ...


class MemoryStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._values: dict[str, tuple[Any, float]] = {}

    async def hit(self, key: str, limit: int, window_seconds: float) -> tuple[bool, int]:
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= window_seconds:
            hits.popleft()
        if len(hits) >= limit:
            return False, max(1, math.ceil(window_seconds - (now - hits[0])))
        hits.append(now)
        return True, 0

    def _live(self, key: str) -> Any:
        value, expires = self._values.get(key, (None, 0.0))
        if value is not None and self._clock() >= expires:
            del self._values[key]
            return None
        return value

    async def get(self, key: str) -> str | None:
        return self._live(key)

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._values[key] = (value, self._clock() + ttl_seconds)

    async def delete(self, key: str) -> None:
        self._values.pop(key, None)

    async def incr(self, key: str, amount: float, ttl_seconds: int) -> float:
        current = self._live(key)
        if current is None:
            total, expires = amount, self._clock() + ttl_seconds
        else:
            total, expires = float(current) + amount, self._values[key][1]
        self._values[key] = (total, expires)
        return total


# KEYS[1] sorted set of hit timestamps; ARGV: window ms, limit, unique member.
_SLIDING_WINDOW = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local window = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return {0, tonumber(oldest[2]) + window - now}
end
redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], window)
return {1, 0}
"""


class RedisStore:
    def __init__(self, url: str, key_prefix: str = "aigw:", client: Any = None) -> None:
        if client is None:
            import redis.asyncio as redis

            client = redis.from_url(url, decode_responses=True, socket_timeout=2)
        self._redis = client
        self._prefix = key_prefix
        self._window_script = client.register_script(_SLIDING_WINDOW)

    async def _call(self, coro: Any) -> Any:
        try:
            return await coro
        except Exception as exc:  # redis raises ConnectionError, TimeoutError, ...
            raise StoreError(f"store unavailable: {exc.__class__.__name__}") from exc

    async def hit(self, key: str, limit: int, window_seconds: float) -> tuple[bool, int]:
        window_ms = int(window_seconds * 1000)
        allowed, wait_ms = await self._call(self._window_script(
            keys=[self._prefix + key], args=[window_ms, limit, uuid.uuid4().hex]))
        return bool(allowed), 0 if allowed else max(1, math.ceil(int(wait_ms) / 1000))

    async def get(self, key: str) -> str | None:
        return await self._call(self._redis.get(self._prefix + key))

    async def set(self, key: str, value: str, ttl_seconds: int) -> None:
        await self._call(self._redis.set(self._prefix + key, value, ex=ttl_seconds))

    async def delete(self, key: str) -> None:
        await self._call(self._redis.delete(self._prefix + key))

    async def incr(self, key: str, amount: float, ttl_seconds: int) -> float:
        async def run() -> float:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incrbyfloat(self._prefix + key, amount)
                pipe.expire(self._prefix + key, ttl_seconds, nx=True)
                total, _ = await pipe.execute()
            return float(total)

        return await self._call(run())
