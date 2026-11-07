"""Fallback and circuit breaking (ADR-0009): visible, policy-respecting, priced as served."""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from aigw import metrics
from aigw.auth import hash_key
from aigw.config import CircuitBreakerSpec, GatewayConfig
from aigw.main import create_app
from aigw.providers import ProviderError
from aigw.providers.mock import MockProvider
from aigw.routing import CircuitBreaker, Router

KEY = "test-key"
HEADERS = {"Authorization": f"Bearer {KEY}"}


class Failing:
    """An upstream that fails every call with the given status, and counts the attempts."""

    kind = "failing"

    def __init__(self, status: int = 429):
        self.status, self.calls = status, 0

    def _fail(self):
        self.calls += 1
        raise ProviderError(f"upstream said {self.status}", status=self.status,
                            client_message="rejected" if self.status < 500 else None)

    async def complete(self, req, request_id):
        self._fail()

    async def stream(self, req, request_id):
        self._fail()
        yield  # pragma: no cover

    async def messages(self, body, betas):
        self._fail()

    async def messages_stream(self, body, betas):
        self._fail()
        yield  # pragma: no cover


def config(primary_fallbacks=("backup",), allowed=("*",), budget=None, prices=None, **extra):
    prices = prices or {}
    models = [
        {"name": "primary", "provider": "mock", "fallbacks": list(primary_fallbacks),
         **({"price": prices["primary"]} if "primary" in prices else {})},
        {"name": "backup", "provider": "mock",
         **({"price": prices["backup"]} if "backup" in prices else {})},
        {"name": "other", "provider": "mock"},
    ]
    team = {"name": "t", "key_sha256": [hash_key(KEY)], "allowed_models": list(allowed),
            "requests_per_minute": 1000, **({"budget": budget} if budget else {})}
    return GatewayConfig.model_validate({"models": models, "teams": [team], **extra})


def app(cfg, primary, backup=None):
    providers = {"primary": primary, "backup": backup or MockProvider(cfg.models[1]),
                 "other": MockProvider(cfg.models[2])}
    return TestClient(create_app(cfg, providers=providers))


def chat(model="primary", **extra):
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra}


def message(model="primary", **extra):
    return {"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}],
            **extra}


def audit_lines(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "aigw.audit"]


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_throttled_or_failing_model_falls_back_visibly(status, caplog):
    cfg = config(prices={"primary": {"input_per_mtok": 3, "output_per_mtok": 15},
                         "backup": {"input_per_mtok": 1, "output_per_mtok": 5}})
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = app(cfg, Failing(status)).post("/v1/chat/completions", json=chat(),
                                              headers=HEADERS)
    assert resp.status_code == 200
    assert resp.headers["X-AIGW-Served-Model"] == "backup"
    assert resp.json()["model"] == "backup"
    line = audit_lines(caplog)[-1]
    assert (line["model"], line["served_model"]) == ("primary", "backup")
    assert line["fallback_attempts"][0]["model"] == "primary"
    # Priced as the model that answered: 1 input + 2 output tokens at the backup's prices.
    assert line["cost_usd"] == pytest.approx((1 * 1 + 2 * 5) / 1_000_000)


def test_a_rejected_request_is_not_retried_on_another_model():
    failing = Failing(400)
    resp = app(config(), failing).post("/v1/messages", json=message(), headers=HEADERS)
    assert resp.status_code == 400 and failing.calls == 1
    assert "X-AIGW-Served-Model" not in resp.headers


def test_no_fallback_configured_means_no_fallback():
    resp = app(config(primary_fallbacks=()), Failing(503)).post(
        "/v1/chat/completions", json=chat(), headers=HEADERS)
    assert resp.status_code == 502


def test_fallbacks_the_team_may_not_use_are_skipped(caplog):
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = app(config(allowed=("primary",)), Failing(429)).post(
            "/v1/chat/completions", json=chat(), headers=HEADERS)
    assert resp.status_code == 502
    assert "served_model" not in audit_lines(caplog)[-1]


def test_every_model_failing_is_one_error_with_every_attempt_audited(caplog):
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = app(config(), Failing(503), Failing(429)).post(
            "/v1/chat/completions", json=chat(), headers=HEADERS)
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "upstream_error"
    line = audit_lines(caplog)[-1]
    assert [a["model"] for a in line["fallback_attempts"]] == ["primary"]
    assert "429" in line["reason"]  # the last model's error


def test_streamed_chat_falls_back_before_the_first_chunk():
    resp = app(config(), Failing(503)).post("/v1/chat/completions", json=chat(stream=True),
                                            headers=HEADERS)
    assert resp.status_code == 200 and resp.headers["X-AIGW-Served-Model"] == "backup"
    chunks = [json.loads(line[6:]) for line in resp.text.splitlines()
              if line.startswith("data: {")]
    assert chunks and all(c["model"] == "backup" for c in chunks)


def test_messages_api_falls_back_streamed_and_not():
    client = app(config(), Failing(429))
    plain = client.post("/v1/messages", json=message(), headers=HEADERS)
    assert plain.status_code == 200 and plain.json()["model"] == "backup"
    streamed = client.post("/v1/messages", json=message(stream=True), headers=HEADERS)
    assert streamed.status_code == 200
    assert '"model":"backup"' in streamed.text
    assert streamed.headers["X-AIGW-Served-Model"] == "backup"


def test_messages_api_skips_fallbacks_that_cannot_speak_it():
    class ChatOnly:
        kind = "openai_compat"

        async def complete(self, req, request_id):  # pragma: no cover - never chosen
            raise AssertionError

    resp = app(config(), Failing(429), ChatOnly()).post("/v1/messages", json=message(),
                                                        headers=HEADERS)
    assert resp.status_code == 429 and resp.headers.get("X-AIGW-Served-Model") is None


def test_team_over_budget_does_not_spill_onto_a_priced_fallback():
    cfg = config(budget={"usd": 0.000001, "period": "day"},
                 prices={"backup": {"input_per_mtok": 1000, "output_per_mtok": 1000}})
    client = app(cfg, Failing(503))
    # Spend the budget on the priced model directly, then fail the free primary.
    assert client.post("/v1/chat/completions", json=chat("backup"),
                       headers=HEADERS).status_code == 200
    assert client.post("/v1/chat/completions", json=chat(), headers=HEADERS).status_code == 502


def test_circuit_breaker_skips_a_failing_model_then_retries_after_cooldown():
    now = [0.0]
    spec = CircuitBreakerSpec(failure_threshold=3, cooldown_seconds=30)
    breaker = CircuitBreaker(spec, clock=lambda: now[0])
    cfg = config()
    router = Router({m.name: m for m in cfg.models}, breaker)
    team = cfg.teams[0]
    for _ in range(3):
        breaker.failed("primary")
    route = router.route(team, "primary")
    assert [m.name for m in route.chain] == ["backup"]
    assert route.attempts == [{"model": "primary", "reason": "circuit_open"}]
    now[0] = 31  # half-open: one more try is allowed
    assert [m.name for m in router.route(team, "primary").chain] == ["primary", "backup"]
    breaker.failed("primary")  # the trial failed: open again straight away
    assert [m.name for m in router.route(team, "primary").chain] == ["backup"]
    now[0] = 62
    breaker.succeeded("primary")
    assert [m.name for m in router.route(team, "primary").chain] == ["primary", "backup"]


def test_open_breaker_saves_the_failing_call(caplog):
    failing = Failing(503)
    client = app(config(circuit_breaker={"failure_threshold": 2, "cooldown_seconds": 60}),
                 failing)
    for _ in range(4):
        assert client.post("/v1/chat/completions", json=chat(),
                           headers=HEADERS).status_code == 200
    assert failing.calls == 2  # after two failures the primary is skipped, not retried


def test_all_breakers_open_still_tries_rather_than_failing_untried():
    breaker = CircuitBreaker(CircuitBreakerSpec(failure_threshold=1))
    cfg = config()
    router = Router({m.name: m for m in cfg.models}, breaker)
    breaker.failed("primary")
    breaker.failed("backup")
    assert [m.name for m in router.route(cfg.teams[0], "primary").chain] == ["primary", "backup"]


def test_fallbacks_are_counted_in_metrics():
    def count():
        return metrics.REGISTRY.get_sample_value(
            "aigw_fallbacks_total", {"team": "t", "requested": "primary", "served": "backup"}) or 0

    before = count()
    app(config(), Failing(429)).post("/v1/chat/completions", json=chat(), headers=HEADERS)
    assert count() == before + 1


@pytest.mark.parametrize("fallbacks", [["missing"], ["primary"], ["backup", "backup"]])
def test_fallbacks_must_be_other_distinct_known_models(fallbacks):
    with pytest.raises(ValidationError, match="fallbacks"):
        config(primary_fallbacks=fallbacks)


def test_fallback_is_one_level_deep():
    cfg = GatewayConfig.model_validate({
        "models": [{"name": "a", "provider": "mock", "fallbacks": ["b"]},
                   {"name": "b", "provider": "mock", "fallbacks": ["c"]},
                   {"name": "c", "provider": "mock"}],
        "teams": [{"name": "t", "key_sha256": ["x"]}]})
    router = Router({m.name: m for m in cfg.models},
                    CircuitBreaker(CircuitBreakerSpec()))
    assert [m.name for m in router.route(cfg.teams[0], "a").chain] == ["a", "b"]
