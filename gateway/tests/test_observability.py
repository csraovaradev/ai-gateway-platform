import json

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from aigw.auth import hash_key
from aigw.config import GatewayConfig
from aigw.main import create_app
from aigw.metrics import REGISTRY

KEY = "obs-key"
SPANS = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(SPANS))
trace.set_tracer_provider(_provider)  # once per process; the aigw tracer proxies to it


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(GatewayConfig.model_validate({
        "models": [{"name": "mock-echo", "provider": "mock",
                    "price": {"input_per_mtok": 1_000_000, "output_per_mtok": 1_000_000}},
                   {"name": "secret-model", "provider": "mock"}],
        "teams": [{"name": "obs", "key_sha256": [hash_key(KEY)],
                   "allowed_models": ["mock-echo"]}],
    })))


def chat(client: TestClient, model: str = "mock-echo"):
    return client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {KEY}"},
                       json={"model": model, "messages": [{"role": "user", "content": "hi there"}]})


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_metrics_follow_audit_events(client):
    ok = {"event": "chat_completion", "team": "obs", "target": "mock-echo", "status": "ok"}
    before = (sample("aigw_requests_total", **ok),
              sample("aigw_tokens_total", team="obs", model="mock-echo", direction="output"),
              sample("aigw_cost_usd_total", team="obs", model="mock-echo"),
              sample("aigw_denials_total", event="chat_completion", team="obs",
                     reason="model_not_allowed"))
    chat(client)
    chat(client, "secret-model")
    after = (sample("aigw_requests_total", **ok),
             sample("aigw_tokens_total", team="obs", model="mock-echo", direction="output"),
             sample("aigw_cost_usd_total", team="obs", model="mock-echo"),
             sample("aigw_denials_total", event="chat_completion", team="obs",
                    reason="model_not_allowed"))
    # "echo: hi there" is 3 output tokens; at $1 per token the request costs 2 + 3 = $5.
    assert [a - b for a, b in zip(after, before, strict=True)] == [1, 3, 5, 1]


def test_client_strings_never_become_labels(client):
    client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {KEY}"},
                json={"model": "made-up-model-123", "messages": [{"role": "user", "content": "x"}]})
    assert "made-up-model-123" not in client.get("/metrics").text
    assert sample("aigw_denials_total", event="chat_completion", team="obs",
                  reason="unknown_model") >= 1


def test_metrics_endpoint_is_prometheus_text(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "# TYPE aigw_request_duration_seconds histogram" in resp.text


def test_upstream_span_uses_gen_ai_conventions_and_matches_audit(client, caplog):
    SPANS.clear()
    with caplog.at_level("INFO", logger="aigw.audit"):
        chat(client)
    span = next(s for s in SPANS.get_finished_spans() if s.name == "chat mock-echo")
    assert span.kind == trace.SpanKind.CLIENT
    assert {k: span.attributes[k] for k in (
        "gen_ai.operation.name", "gen_ai.system", "gen_ai.request.model",
        "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "aigw.team")} == {
        "gen_ai.operation.name": "chat", "gen_ai.system": "mock",
        "gen_ai.request.model": "mock-echo", "gen_ai.usage.input_tokens": 2,
        "gen_ai.usage.output_tokens": 3, "aigw.team": "obs"}
    record = json.loads(caplog.records[-1].getMessage())
    assert record["trace_id"] == format(span.context.trace_id, "032x")
    assert "hi there" not in json.dumps(dict(span.attributes))
