import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aigw.auth import hash_key
from aigw.config import GatewayConfig, load_config
from aigw.main import create_app
from aigw.providers import ProviderError
from aigw.ratelimit import RateLimiter
from aigw.store import MemoryStore

PLATFORM_KEY = "test-platform-key"
DATA_KEY = "test-data-key"
POLICIES = Path(__file__).resolve().parents[2] / "policies"


def make_config() -> GatewayConfig:
    return GatewayConfig.model_validate(
        {
            "models": [
                {"name": "mock-echo", "provider": "mock"},
                {"name": "mock-premium", "provider": "mock"},
            ],
            "teams": [
                {"name": "platform", "key_sha256": [hash_key(PLATFORM_KEY)],
                 "allowed_models": ["*"], "requests_per_minute": 100},
                {"name": "data", "key_sha256": [hash_key(DATA_KEY)],
                 "allowed_models": ["mock-echo"], "requests_per_minute": 2},
            ],
        }
    )


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(make_config()))


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def chat(model: str = "mock-echo", **extra):
    return {"model": model, "messages": [{"role": "user", "content": "hello gateway"}], **extra}


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_missing_and_invalid_keys_rejected(client):
    assert client.post("/v1/chat/completions", json=chat()).status_code == 401
    resp = client.post("/v1/chat/completions", json=chat(), headers=auth("wrong"))
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "authentication_error"


def test_models_filtered_by_team(client):
    ids = {m["id"] for m in client.get("/v1/models", headers=auth(DATA_KEY)).json()["data"]}
    assert ids == {"mock-echo"}
    ids = {m["id"] for m in client.get("/v1/models", headers=auth(PLATFORM_KEY)).json()["data"]}
    assert ids == {"mock-echo", "mock-premium"}


def test_completion(client):
    resp = client.post("/v1/chat/completions", json=chat(), headers=auth(PLATFORM_KEY))
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "echo: hello gateway"
    assert body["model"] == "mock-echo"
    assert body["usage"]["total_tokens"] > 0


def test_content_parts_supported(client):
    payload = {"model": "mock-echo", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "from parts"}]}]}
    resp = client.post("/v1/chat/completions", json=payload, headers=auth(PLATFORM_KEY))
    assert resp.json()["choices"][0]["message"]["content"] == "echo: from parts"


def test_unknown_model_404(client):
    resp = client.post("/v1/chat/completions", json=chat("nope"), headers=auth(PLATFORM_KEY))
    assert resp.status_code == 404


def test_model_not_allowed_403(client):
    resp = client.post("/v1/chat/completions", json=chat("mock-premium"), headers=auth(DATA_KEY))
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "permission_error"


def test_rate_limit_429(client):
    for _ in range(2):
        assert client.post("/v1/chat/completions", json=chat(),
                           headers=auth(DATA_KEY)).status_code == 200
    resp = client.post("/v1/chat/completions", json=chat(), headers=auth(DATA_KEY))
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) >= 1


def test_streaming(client):
    with client.stream("POST", "/v1/chat/completions", json=chat(stream=True),
                       headers=auth(PLATFORM_KEY)) as resp:
        assert resp.status_code == 200
        lines = [ln for ln in resp.iter_lines() if ln.startswith("data:")]
    assert lines[-1] == "data: [DONE]"
    events = [json.loads(ln[5:]) for ln in lines[:-1]]
    text = "".join(e["choices"][0]["delta"].get("content", "") for e in events)
    assert text == "echo: hello gateway"
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


def test_streamed_requests_are_metered(client, caplog):
    with caplog.at_level("INFO", logger="aigw.audit"), client.stream(
        "POST", "/v1/chat/completions", json=chat(stream=True), headers=auth(PLATFORM_KEY)
    ) as resp:
        events = [json.loads(ln[5:]) for ln in resp.iter_lines()
                  if ln.startswith("data:") and ln != "data: [DONE]"]
    assert events[-1]["usage"]["completion_tokens"] == 3
    record = json.loads(caplog.records[-1].getMessage())
    assert record["stream"] is True
    assert (record["prompt_tokens"], record["completion_tokens"]) == (2, 3)


def test_audit_log_has_no_prompt_content(client, caplog):
    with caplog.at_level("INFO", logger="aigw.audit"):
        client.post("/v1/chat/completions", json=chat(), headers=auth(PLATFORM_KEY))
    line = caplog.records[-1].getMessage()
    record = json.loads(line)
    assert record["team"] == "platform" and record["status"] == "ok"
    assert "hello gateway" not in line


def test_auth_failures_are_audited_without_token(client, caplog):
    with caplog.at_level("INFO", logger="aigw.audit"):
        client.post("/v1/chat/completions", json=chat())
        client.post("/v1/chat/completions", json=chat(), headers=auth("guessed-key"))
    records = [json.loads(r.getMessage()) for r in caplog.records]
    assert [r["reason"] for r in records] == ["missing_token", "invalid_api_key"]
    assert all(r["event"] == "auth" and r["http_status"] == 401 for r in records)
    assert records[1]["path"] == "/v1/chat/completions"
    assert "guessed-key" not in caplog.text


def test_upstream_failure_is_502():
    class Broken:
        kind = "broken"

        async def complete(self, req, request_id):
            raise ProviderError("boom")

    cfg = make_config()
    app = create_app(cfg, providers={"mock-echo": Broken(), "mock-premium": Broken()})
    resp = TestClient(app).post("/v1/chat/completions", json=chat(), headers=auth(PLATFORM_KEY))
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "upstream_error"


def test_rate_limiter_window_resets():
    now = [0.0]
    limiter = RateLimiter(MemoryStore(clock=lambda: now[0]), window_seconds=60)
    assert asyncio.run(limiter.check("t", 1)) == (True, 0)
    assert asyncio.run(limiter.check("t", 1))[0] is False
    now[0] = 61
    assert asyncio.run(limiter.check("t", 1)) == (True, 0)


def test_config_rejects_unknown_model_reference():
    with pytest.raises(ValueError):
        GatewayConfig.model_validate({
            "models": [{"name": "a", "provider": "mock"}],
            "teams": [{"name": "t", "key_sha256": ["x"], "allowed_models": ["b"]}],
        })


@pytest.mark.parametrize("policy", sorted(POLICIES.glob("*.yaml")), ids=lambda p: p.name)
def test_example_policies_are_valid(policy):
    cfg = load_config(policy)
    assert cfg.models and cfg.teams
    # Every laptop policy offers the credential-free mock; production serves real models only.
    assert cfg.mode == "production" or "mock-echo" in {m.name for m in cfg.models}
