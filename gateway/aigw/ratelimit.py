"""Per-team sliding-window rate limiter on the shared store (ADR-0003)."""

from __future__ import annotations

from .store import MemoryStore, Store


class RateLimiter:
    def __init__(self, store: Store | None = None, window_seconds: float = 60.0) -> None:
        self._store = store or MemoryStore()
        self._window = window_seconds

    async def check(self, key: str, limit: int) -> tuple[bool, int]:
        """Record a hit if allowed. Returns (allowed, retry_after_seconds)."""
        return await self._store.hit(f"ratelimit:{key}", limit, self._window)
