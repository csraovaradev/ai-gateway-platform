# ADR-0003: Shared state in Redis, token metering, and budgets

- **Status:** Accepted
- **Date:** 2026-11-01
- **Phase:** 4 (Policy & metering)

## Context

Rate limits and MCP session owners live in each replica's memory. With N replicas behind a load
balancer a team effectively gets N times its rate limit, and an MCP session opened on one replica is
unknown on the others. There are no spending limits, and streamed responses record no token usage,
so any budget built on the current audit data could be bypassed by setting `stream: true`.

## Decision

1. **A small `Store` interface** (`aigw/store.py`) with two backends:
   - `memory`: the default, same behaviour as before, no dependencies for local development.
   - `redis`: shared by all replicas (ElastiCache on AWS). Selected with a top-level `store:` section.
2. **Rate limits** are a sliding window kept in a Redis sorted set and updated by one Lua script, so
   "count, compare, record" is atomic across replicas. The script reads Redis's own clock (`TIME`),
   so replicas with skewed clocks still agree on the window.
3. **MCP session owners** are stored with a 24-hour TTL, so any replica can check them.
4. **Every provider reports usage when streaming** on its final chunk (`usage`, as OpenAI does with
   `stream_options.include_usage`), so streamed and non-streamed requests are metered the same way.
5. **Prices are policy.** Models may set `price` (USD per million input and output tokens). Audit
   lines then carry `cost_usd`. Prices are configuration, not code, because they change and differ by
   contract.
6. **Budgets** are per team: `budget: {usd: 25, period: day | month}`. Spend is accumulated in the
   store under a key per team and period (UTC). A team at or over budget gets
   `429 insufficient_quota` before the request is sent upstream. The check happens before the call and
   the spend is added after it, so a team can overshoot by at most the requests already in flight.
7. **Cost attribution** comes from the audit log (`python -m aigw.report`): spend and tokens per team
   and model. Metrics and dashboards are Phase 5.

## Consequences

- Rate limits and budgets hold across replicas and restarts once Redis is configured.
- Redis becomes a dependency on the request path. If it is unreachable the gateway fails closed
  (503) rather than serving traffic without limits.
- Budgets are soft by at most the cost of concurrent in-flight requests; reserving an estimate before
  each call would tighten that at the cost of more complexity.
- Models without a `price` are metered in tokens but cost `0`, so budgets do not limit them.

## Alternatives considered

- **Fixed-window counters (`INCR` + `EXPIRE`).** Simpler, but allow up to 2x the limit at a window
  boundary.
- **DynamoDB.** Managed and IAM-native, but higher latency per check and no server-side scripting for
  an atomic sliding window.
- **Sticky sessions at the load balancer instead of shared MCP session state.** Breaks on scale-in and
  pod restarts.
