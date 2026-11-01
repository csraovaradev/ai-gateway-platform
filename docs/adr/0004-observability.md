# ADR-0004: Metrics from audit events, OpenTelemetry traces, and SLOs

- **Status:** Accepted
- **Date:** 2026-11-01
- **Phase:** 5 (Observability)

## Context

Operators need to answer three questions quickly: is the gateway healthy, who is using it and at
what cost, and why was this one request slow or refused. The audit log answers the last two per
request but is not built for dashboards or alerting.

## Decision

1. **Metrics are derived from audit events.** `audit.record()` also updates Prometheus counters and
   histograms, so every number on a dashboard has a matching audit line and the two cannot drift.
   Metrics: `aigw_requests_total`, `aigw_denials_total`, `aigw_request_duration_seconds`,
   `aigw_tokens_total`, `aigw_cost_usd_total`.
2. **Bounded labels only.** Label values come from the policy file (team, model, MCP server) or a fixed
   set (status, denial reason). Client-controlled strings (tool names, unknown model names) stay in the
   audit log, since as labels they would let any caller create unbounded time series.
3. **`/metrics` on the main port, internal only.** It exposes team names and spend, so it must not be
   routed through the public ingress; Phase 6 enforces that with a NetworkPolicy.
4. **Traces via OpenTelemetry, opt-in.** Spans use the OTel API everywhere (no-ops by default). With
   `OTEL_EXPORTER_OTLP_ENDPOINT` set and the `otel` extra installed they are exported over OTLP/HTTP, so
   any backend works (Jaeger, Tempo, Datadog, X-Ray via the ADOT collector). Upstream LLM calls follow
   the GenAI semantic conventions (`gen_ai.request.model`, `gen_ai.usage.input_tokens`, ...); Vault
   reads and MCP calls get their own spans. No prompt or tool content goes into spans.
5. **Audit lines carry `trace_id`,** so an audit entry links straight to its trace.
6. **SLOs as Prometheus rules** (`observability/prometheus/rules.yaml`):
   - Availability: 99.5% of chat requests over 30 days do not fail on the gateway side (`status="error"`;
     policy denials are correct behaviour, not failures).
   - Latency: 95% of non-streaming chat requests complete within 10 s.
   - Multi-window burn-rate alerts (1 h and 6 h) page before the monthly error budget is gone.

## Consequences

- Dashboards, alerts, and the audit log tell one consistent story.
- Latency for streamed requests measures the whole stream, so the latency SLO covers non-streaming
  requests only; time-to-first-token is a candidate for a later metric.
- Upstream latency (Bedrock, MCP servers) counts against the gateway's latency SLO; traces separate
  gateway time from upstream time when a breach needs explaining.

## Alternatives considered

- **Separate metric calls at each decision point.** More flexible, but easy to forget on new code paths,
  and the numbers drift from the audit log.
- **A separate metrics port.** Stronger isolation, but another port to configure in every chart and
  probe; network policy achieves the same isolation.
