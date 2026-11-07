"""Fallback across models, with a circuit breaker per model (ADR-0009).

A request for model A tries A, then A's `fallbacks` in order, when an attempt fails in a way another
model could fix: throttling, an outage, a timeout. Rejections of the request itself (a 400) are not
retried, since the next model would reject it too. A fallback is used only if the team may use it.

The circuit breaker remembers models that keep failing, so requests skip them for a cooldown
instead of paying a timeout each time. Its state is per gateway replica: it is a latency
optimisation, not a policy, so replicas do not need to agree.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from .config import CircuitBreakerSpec, ModelSpec, TeamSpec
from .providers import ProviderError

# Upstream statuses another model might not share: throttled, failed, unavailable, timed out.
RETRYABLE = frozenset({429, 500, 502, 503, 504})

T = TypeVar("T")


class CircuitBreaker:
    def __init__(self, spec: CircuitBreakerSpec,
                 clock: Callable[[], float] = time.monotonic) -> None:
        # A cooldown is in-process timing, so the monotonic clock is right here; if a laptop
        # sleeps through it, the cooldown just lasts longer.
        self.spec, self._clock = spec, clock
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def available(self, name: str) -> bool:
        opened = self._opened_at.get(name)
        # After the cooldown the breaker is half-open: requests try the model again, and the
        # first failure reopens it.
        return opened is None or self._clock() - opened >= self.spec.cooldown_seconds

    def succeeded(self, name: str) -> None:
        self._failures.pop(name, None)
        self._opened_at.pop(name, None)

    def failed(self, name: str) -> None:
        self._failures[name] = self._failures.get(name, 0) + 1
        if name in self._opened_at or self._failures[name] >= self.spec.failure_threshold:
            self._opened_at[name] = self._clock()


@dataclass
class Route:
    """The models to try for one request, in order, and what was skipped or failed on the way."""

    chain: list[ModelSpec]
    attempts: list[dict[str, str]] = field(default_factory=list)


class Router:
    def __init__(self, models: dict[str, ModelSpec], breaker: CircuitBreaker) -> None:
        self.models, self.breaker = models, breaker

    def route(self, team: TeamSpec, name: str,
              usable: Callable[[ModelSpec], bool] = lambda _: True) -> Route:
        primary = self.models[name]
        fallbacks = [self.models[f] for f in primary.fallbacks]
        chain = [primary] + [m for m in fallbacks if team.allows(m.name) and usable(m)]
        ready = [m for m in chain if self.breaker.available(m.name)]
        if not ready:
            return Route(chain)  # every breaker is open: try anyway rather than fail untried
        skipped = [{"model": m.name, "reason": "circuit_open"} for m in chain if m not in ready]
        return Route(ready, skipped)

    async def call(self, route: Route,
                   attempt: Callable[[ModelSpec], Awaitable[T]]) -> tuple[T, ModelSpec]:
        """Run `attempt` on each model in the route until one succeeds.

        Returns the result and the model that produced it. If every model fails, the last
        error is raised with `route.attempts` describing the earlier ones.
        """
        for i, model in enumerate(route.chain):
            try:
                result = await attempt(model)
            except ProviderError as exc:
                if exc.status not in RETRYABLE:
                    raise  # the request itself was rejected; another model would reject it too
                self.breaker.failed(model.name)
                if i == len(route.chain) - 1:
                    raise
                route.attempts.append({"model": model.name, "reason": str(exc)[:120]})
                continue
            self.breaker.succeeded(model.name)
            return result, model
        raise AssertionError("route has no models")  # pragma: no cover - chain is never empty


def audit_fields(route: Route, requested: str, served: ModelSpec | None) -> dict[str, Any]:
    """What the audit line records about routing: the model that answered, if it was not the one
    requested, and every model skipped or failed on the way."""
    fields: dict[str, Any] = {}
    if served is not None and served.name != requested:
        fields["served_model"] = served.name
    if route.attempts:
        fields["fallback_attempts"] = route.attempts
    return fields


async def first_event(stream: Any) -> tuple[Any, Any]:
    """Start a streamed response and wait for its first event, so a failure to start can still
    fall back. Returns (first event or None if the stream was empty, the stream)."""
    try:
        return await stream.__anext__(), stream
    except StopAsyncIteration:
        return None, stream
