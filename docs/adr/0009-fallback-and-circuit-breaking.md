# ADR-0009: Model fallback and circuit breaking

- **Status:** Accepted
- **Date:** 2026-11-07
- **Follows:** ADR-0003, ADR-0005, ADR-0006

## Context

A single upstream model is a single point of failure. Bedrock throttles under load, regions have
incidents, and a model can be unavailable to an account. Until now the gateway returned the error,
and every developer's agent stalled. Fallback is the feature most expected of an AI gateway.

Fallback also has a trap. If the gateway silently answers a request for Sonnet with Haiku, the
developer gets a weaker answer without knowing why, the cost report prices the wrong model, and a
team could end up on a model its policy does not allow.

## Decision

1. **`fallbacks` per model:** an ordered list of other model names in the policy. A request tries
   the requested model, then each fallback, and stops at the first success.
   - Only failures another model might not share trigger a fallback: 429, 500, 502, 503, 504
     (throttling, outages, timeouts, unreachable endpoints). A 400 means the request itself was
     rejected, so it is returned as is.
   - One level deep: a fallback's own fallbacks are not followed, so there are no chains or loops.
2. **Policy still applies.** A fallback is used only if the team may use that model. A team over
   budget can keep using a free model, but cannot spill onto a priced fallback when that model
   fails. On `/v1/messages`, fallbacks must speak the Messages API.
3. **Never silent.**
   - Responses carry `X-AIGW-Served-Model`, and the body's `model` names the model that answered.
   - The audit line keeps `model` (requested) and adds `served_model` and `fallback_attempts`
     (each skipped or failed model, with the reason).
   - Cost, tokens, and metrics are attributed to the model that answered; `aigw_fallbacks_total`
     counts fallbacks by team, requested, and served model.
4. **Streams fall back only before the first event.** Chat streams now wait for the first chunk
   before answering, as `/v1/messages` already did (ADR-0006), so a model that fails to start can
   fall back, and a request nobody can serve gets an HTTP error instead of a 200 with an error
   inside. Once text has reached the client, a failure ends the stream as before.
5. **Circuit breaker per model:** after `failure_threshold` (default 3) consecutive retryable
   failures, the model is skipped for `cooldown_seconds` (default 30) and requests go straight to
   fallbacks, without paying a timeout each time. After the cooldown one trial request goes through;
   success closes the breaker, failure reopens it. If every model in a route is open, the gateway
   tries them anyway rather than failing without trying. State is per replica and in memory: it is a
   latency optimisation, not policy, so replicas do not need to agree.

## Alternatives considered

- **Weighted load balancing across providers.** Not built. For Bedrock, cross-region inference
  profiles (`us.anthropic...`) already spread requests across regions on AWS's side; for
  self-hosted models, a Kubernetes Service balances across replicas. A gateway-level weighting
  would duplicate both. Fallback covers the case they do not: a model or account being unavailable.
- **Retrying the same model.** Bedrock calls already make up to two attempts (ADR-0005). More
  retries on a throttled model add load where it hurts most.
- **Shared breaker state in Redis.** More moving parts for a hint; per-replica state converges
  within a few requests.

## Consequences

- Availability no longer depends on one model or region, and the audit log shows how often it
  mattered.
- A fallback can be a different quality, price, or region. Choosing fallbacks is a policy decision
  reviewed like any other: in regulated environments, keep fallbacks within approved regions.
- Verified live: with Sonnet pointed at an unreachable region, requests were answered by Claude
  Haiku on Bedrock, priced as Haiku, and after three failures the breaker skipped Sonnet entirely.
  Claude Code worked unchanged.
