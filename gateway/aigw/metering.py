"""Cost per request and per-team budgets (ADR-0003).

Spend is kept in the shared store under one key per team and budget period (UTC), so every
replica sees the same total. Budgets are checked before a request and charged after it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from .config import ModelSpec, TeamSpec
from .store import Store

_GRACE = timedelta(hours=1)  # keep a period's key briefly after it ends, for late charges


def cost_usd(model: ModelSpec, prompt_tokens: int | None, completion_tokens: int | None) -> float:
    if model.price is None:
        return 0.0
    return round((prompt_tokens or 0) * model.price.input_per_mtok / 1e6
                 + (completion_tokens or 0) * model.price.output_per_mtok / 1e6, 6)


class Meter:
    def __init__(self, store: Store, clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._store = store
        self._clock = clock

    def _period(self, team: TeamSpec) -> tuple[str, int]:
        """Store key for the team's current budget period, and seconds until it can expire."""
        now = self._clock()
        period = team.budget.period if team.budget else "month"
        if period == "day":
            label = now.strftime("%Y-%m-%d")
            end = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
        else:
            label = now.strftime("%Y-%m")
            end = datetime(now.year + now.month // 12, now.month % 12 + 1, 1, tzinfo=UTC)
        return f"spend:{team.name}:{label}", int((end + _GRACE - now).total_seconds())

    async def spent(self, team: TeamSpec) -> float:
        key, _ = self._period(team)
        return float(await self._store.get(key) or 0)

    async def over_budget(self, team: TeamSpec) -> bool:
        return team.budget is not None and await self.spent(team) >= team.budget.usd

    async def charge(self, team: TeamSpec, amount: float) -> None:
        if amount > 0:
            key, ttl = self._period(team)
            await self._store.incr(key, amount, ttl)
