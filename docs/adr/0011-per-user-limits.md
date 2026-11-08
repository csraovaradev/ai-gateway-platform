# ADR-0011: Per-person rate limits and budgets

- **Status:** Accepted
- **Date:** 2026-11-08
- **Follows:** ADR-0003, ADR-0007

## Context

Rate limits and budgets are per team (ADR-0003). With single sign-on (ADR-0007) every request from a
person carries their identity, which exposes the gap: one developer's agent stuck in a loop can use
the whole team's rate limit and budget, and everyone else on the team is refused until the period
ends.

## Decision

1. **`per_user` on a team:** `requests_per_minute` and `budget` for each signed-in person in the team.
   Both are optional and apply on top of the team's own limits; a request must pass both.
2. **Only for signed-in people.** Callers using an API key (services, CI) have no person attached, so
   only team limits apply to them.
3. **Where:** the per-person rate limit covers the model APIs and MCP tool calls, like the team limit.
   The per-person budget gates priced models, like the team budget, and also stops a person over
   budget from spilling onto a priced fallback (ADR-0009).
4. **State:** the same shared store as team limits, with keys like `ratelimit:<team>/user:<name>` and
   `spend:<team>/user:<name>:<period>`, so every replica agrees. Per-person spend is only kept for
   teams that set a per-person budget.
5. **Clear refusals:** `your personal rate limit is exceeded` or `you have used your personal budget
   of $X per day`, audited as `user_rate_limited` or `user_budget_exceeded`. Budget refusals carry
   `Retry-After: 3600`, so Claude Code stops retrying and shows the message (ADR-0006).
6. **Reporting:** `python -m aigw.report` adds spend per signed-in person, and attributes cost to the
   model that answered rather than the one requested.

## Consequences

- One person's runaway agent no longer locks out their team.
- Per-person budgets have the same small overshoot as team budgets: at most the requests in flight
  when the limit is crossed (ADR-0003).
- User names become part of store keys. They come from verified tokens, not from request content, and
  each key expires with its window or period.
- Verified live with Keycloak sign-ins and Redis: with a limit of three requests a minute, alice's
  fourth and fifth requests were refused while bob's went through, and Redis held separate counters
  for the team, alice, and bob.
