"""Prometheus metrics derived from audit events, so dashboards and the audit log agree (ADR-0004).

Label values come only from the policy file (team, model, MCP server) or a fixed set (status,
reason). Client-controlled strings such as tool names or unknown model names stay in the audit log:
as labels they would let any caller create unbounded time series.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram

REGISTRY = CollectorRegistry()

REQUESTS = Counter("aigw_requests", "Requests by kind and outcome.",
                   ["event", "team", "target", "status"], registry=REGISTRY)
DENIALS = Counter("aigw_denials", "Requests refused by policy, by reason.",
                  ["event", "team", "reason"], registry=REGISTRY)
LATENCY = Histogram("aigw_request_duration_seconds",
                    "Time from accepting a request to its last byte (whole stream when streaming).",
                    ["event", "target", "stream"], registry=REGISTRY,
                    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120))
TOKENS = Counter("aigw_tokens", "Tokens processed.", ["team", "model", "direction"],
                 registry=REGISTRY)
COST = Counter("aigw_cost_usd", "Spend in USD at policy prices.", ["team", "model"],
               registry=REGISTRY)
CONTENT = Counter("aigw_content_findings", "Secrets and personal data found in prompts (ADR-0010).",
                  ["team", "detector", "action"], registry=REGISTRY)
FALLBACKS = Counter("aigw_fallbacks", "Requests answered by a fallback model (ADR-0009).",
                    ["team", "requested", "served"], registry=REGISTRY)


def _target(event: dict[str, Any]) -> str:
    if event.get("event") == "mcp":
        return str(event.get("server", "-"))
    if event.get("event") == "chat_completion" and event.get("reason") != "unknown_model":
        return str(event.get("model", "-"))
    return "-"


def observe(event: dict[str, Any]) -> None:
    kind = str(event.get("event"))
    team = str(event.get("team", "-"))
    target = _target(event)
    status = str(event.get("status", "-"))
    REQUESTS.labels(kind, team, target, status).inc()
    if status == "denied":
        DENIALS.labels(kind, team, str(event.get("reason", "-"))).inc()
    if event.get("latency_ms") is not None:
        stream = "true" if event.get("stream") else "false"
        LATENCY.labels(kind, target, stream).observe(event["latency_ms"] / 1000)
    # Detector names come from the code and the policy file, so the label set stays bounded.
    for detector, count in (event.get("content_findings") or {}).items():
        CONTENT.labels(team, detector, str(event.get("content_action"))).inc(count)
    if kind == "chat_completion" and status != "denied":
        # Tokens and spend belong to the model that answered, which a fallback may have changed.
        served = str(event.get("served_model") or target)
        if event.get("served_model"):
            FALLBACKS.labels(team, target, served).inc()
        TOKENS.labels(team, served, "input").inc(event.get("prompt_tokens") or 0)
        TOKENS.labels(team, served, "output").inc(event.get("completion_tokens") or 0)
        for direction in ("cache_read", "cache_write"):
            if event.get(f"{direction}_tokens"):
                TOKENS.labels(team, served, direction).inc(event[f"{direction}_tokens"])
        COST.labels(team, served).inc(event.get("cost_usd") or 0)
