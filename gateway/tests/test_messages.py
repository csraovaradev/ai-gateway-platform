import asyncio
import io
import json

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

import aigw.messages
from aigw.auth import hash_key
from aigw.config import GatewayConfig, ModelSpec
from aigw.main import create_app
from aigw.metering import cost_usd
from aigw.providers import ProviderError
from aigw.providers.bedrock import BedrockProvider, _invoke_body

KEY = "msg-key"
PRICE = {"input_per_mtok": 1_000_000, "output_per_mtok": 1_000_000}


def config(**team) -> GatewayConfig:
    return GatewayConfig.model_validate({
        "models": [{"name": "claude-mock", "provider": "mock", "price": PRICE},
                   {"name": "llama", "provider": "openai_compat", "base_url": "http://x/v1"}],
        "teams": [{"name": "dev", "key_sha256": [hash_key(KEY)], **team}],
        "max_request_bytes": 1000,
    })


def body(stream: bool = False, model: str = "claude-mock", **extra) -> dict:
    return {"model": model, "max_tokens": 50, "stream": stream,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hello gateway"}]}],
            **extra}


def sse(text: str) -> list[tuple[str, dict]]:
    events, name = [], None
    for line in text.splitlines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            events.append((name, json.loads(line[6:])))
    return events


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(config()))


def test_message_with_x_api_key_and_attribution(client, caplog):
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = client.post("/v1/messages?beta=true", json=body(), headers={
            "x-api-key": KEY, "anthropic-version": "2023-06-01",
            "x-claude-code-session-id": "sess-1", "x-claude-code-agent-id": "agent-7"})
    assert resp.status_code == 200
    msg = resp.json()
    assert (msg["type"], msg["model"], msg["content"][0]["text"]) == (
        "message", "claude-mock", "echo: hello gateway")
    record = json.loads(caplog.records[-1].getMessage())
    assert record["api"] == "messages" and record["event"] == "chat_completion"
    assert (record["session_id"], record["agent_id"]) == ("sess-1", "agent-7")
    assert (record["prompt_tokens"], record["completion_tokens"], record["cost_usd"]) == (2, 3, 5.0)
    assert "hello gateway" not in caplog.text.split("chat_completion", 1)[1]


def test_bearer_token_also_works(client):
    resp = client.post("/v1/messages", json=body(), headers={"Authorization": f"Bearer {KEY}"})
    assert resp.status_code == 200


def test_stream_events_arrive_in_order_with_model_rewritten(client, caplog):
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = client.post("/v1/messages", json=body(stream=True), headers={"x-api-key": KEY})
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = sse(resp.text)
    names = [n for n, _ in events]
    assert names[0] == "message_start" and names[-2:] == ["message_delta", "message_stop"]
    assert all(n == e["type"] for n, e in events)
    assert events[0][1]["message"]["model"] == "claude-mock"
    text = "".join(e["delta"]["text"] for n, e in events if n == "content_block_delta")
    assert text == "echo: hello gateway"
    record = json.loads(caplog.records[-1].getMessage())
    assert (record["stream"], record["prompt_tokens"], record["completion_tokens"]) == (True, 2, 3)


@pytest.mark.parametrize(("headers", "payload", "status", "kind"), [
    ({"x-api-key": "wrong"}, body(), 401, "authentication_error"),
    ({}, body(), 401, "authentication_error"),
    ({"x-api-key": KEY}, body(model="nope"), 404, "not_found_error"),
    ({"x-api-key": KEY}, body(model="llama"), 400, "invalid_request_error"),
], ids=["bad-key", "no-key", "unknown-model", "openai-only-model"])
def test_errors_use_anthropic_envelope(client, headers, payload, status, kind):
    resp = client.post("/v1/messages", json=payload, headers=headers)
    assert resp.status_code == status
    assert resp.json()["type"] == "error" and resp.json()["error"]["type"] == kind


def test_budget_stop_tells_claude_code_not_to_retry():
    client = TestClient(create_app(config(budget={"usd": 5})))
    assert client.post("/v1/messages", json=body(), headers={"x-api-key": KEY}).status_code == 200
    resp = client.post("/v1/messages", json=body(), headers={"x-api-key": KEY})
    assert resp.status_code == 429
    assert resp.json()["error"]["type"] == "rate_limit_error"
    assert int(resp.headers["Retry-After"]) > 60  # above 60 s Claude Code stops retrying


class Recorder:
    """A Messages-capable provider that records what it was given."""

    kind = "recorder"

    def __init__(self, error: ProviderError | None = None, delay: float = 0.0):
        self.error, self.delay, self.calls = error, delay, []

    async def messages(self, body, betas):
        self.calls.append((body, betas))
        if self.error:
            raise self.error
        return {"type": "message", "content": [], "model": "x",
                "usage": {"input_tokens": 10, "output_tokens": 2,
                          "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 100}}

    async def messages_stream(self, body, betas):
        self.calls.append((body, betas))
        if self.error:
            raise self.error
        yield {"type": "message_start", "message": {"model": "x", "usage": {"input_tokens": 1}}}
        await asyncio.sleep(self.delay)
        yield {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 1}}
        yield {"type": "message_stop"}


def app_with(provider) -> TestClient:
    cfg = config()
    return TestClient(create_app(cfg, providers={"claude-mock": provider, "llama": provider}))


def test_beta_header_and_body_reach_the_provider_unchanged():
    provider = Recorder()
    payload = body(system=[{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
                   context_management={"edits": []})
    app_with(provider).post("/v1/messages", json=payload, headers={
        "x-api-key": KEY, "anthropic-beta": "context-management-2025-06-27, interleaved-thinking"})
    sent, betas = provider.calls[0]
    assert betas == ["context-management-2025-06-27", "interleaved-thinking"]
    assert sent["system"] == payload["system"] and sent["context_management"] == {"edits": []}


def test_upfront_stream_rejection_is_an_http_error_with_upstream_wording():
    wording = "context_management: Extra inputs are not permitted"
    provider = Recorder(ProviderError(f"bedrock ValidationException: {wording}", status=400,
                                      client_message=wording))
    resp = app_with(provider).post("/v1/messages", json=body(stream=True),
                                   headers={"x-api-key": KEY})
    assert resp.status_code == 400
    assert resp.json()["error"] == {"type": "invalid_request_error", "message": wording}


def test_pings_fill_long_silences(monkeypatch):
    monkeypatch.setattr(aigw.messages, "PING_INTERVAL_SECONDS", 0.05)
    resp = app_with(Recorder(delay=0.3)).post("/v1/messages", json=body(stream=True),
                                              headers={"x-api-key": KEY})
    names = [n for n, _ in sse(resp.text)]
    assert names[0] == "message_start" and names[-1] == "message_stop"
    assert names.count("ping") >= 2


def test_cache_tokens_are_metered_and_priced(caplog):
    with caplog.at_level("INFO", logger="aigw.audit"):
        app_with(Recorder()).post("/v1/messages", json=body(), headers={"x-api-key": KEY})
    record = json.loads(caplog.records[-1].getMessage())
    assert (record["cache_read_tokens"], record["cache_write_tokens"]) == (1000, 100)
    # 10 in + 2 out + 100 writes at 1.25x + 1000 reads at 0.1x, at $1 per token
    assert record["cost_usd"] == pytest.approx(10 + 2 + 125 + 100)


def test_cache_prices_can_be_set_explicitly():
    model = ModelSpec(name="m", provider="mock", price={
        "input_per_mtok": 2, "output_per_mtok": 10, "cache_read_per_mtok": 0.2,
        "cache_write_per_mtok": 2.5})
    assert cost_usd(model, 1_000_000, 0, cache_read_tokens=1_000_000,
                    cache_write_tokens=1_000_000) == pytest.approx(2 + 0.2 + 2.5)


def test_count_tokens(client):
    resp = client.post("/v1/messages/count_tokens", json=body(), headers={"x-api-key": KEY})
    assert resp.json() == {"input_tokens": 2}
    missing = client.post("/v1/messages/count_tokens", json=body(model="nope"),
                          headers={"x-api-key": KEY})
    assert missing.status_code == 404 and missing.json()["type"] == "error"


def test_messages_bodies_get_a_larger_size_limit(client):
    big = body(messages=[{"role": "user", "content": "x" * 5000}])
    assert client.post("/v1/messages", json=big, headers={"x-api-key": KEY}).status_code == 200
    chat = {"model": "claude-mock", "messages": [{"role": "user", "content": "x" * 5000}]}
    assert client.post("/v1/chat/completions", json=chat,
                       headers={"Authorization": f"Bearer {KEY}"}).status_code == 413


def test_connection_warming_probe(client):
    assert client.head("/api/hello").status_code == 200


# Bedrock InvokeModel translation


def test_invoke_body_moves_model_to_url_and_betas_into_body():
    sent = json.loads(_invoke_body(body(stream=True, thinking={"type": "adaptive"}), ["b1"]))
    assert "model" not in sent and "stream" not in sent
    assert sent["anthropic_version"] == "bedrock-2023-05-31" and sent["anthropic_beta"] == ["b1"]
    assert sent["thinking"] == {"type": "adaptive"}


class FakeBedrockRuntime:
    def __init__(self, error: Exception | None = None):
        self.error, self.calls = error, []

    def invoke_model(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"body": io.BytesIO(json.dumps({"type": "message", "usage": {}}).encode())}

    def invoke_model_with_response_stream(self, **kwargs):
        self.calls.append(kwargs)
        events = [{"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
                  {"type": "message_stop", "amazon-bedrock-invocationMetrics": {"x": 1}}]
        return {"body": [{"chunk": {"bytes": json.dumps(e).encode()}} for e in events]}

    def count_tokens(self, **kwargs):
        self.calls.append(kwargs)
        return {"inputTokens": 42}


def bedrock(fake) -> BedrockProvider:
    provider = BedrockProvider(ModelSpec(name="claude", provider="bedrock", region="us-west-2",
                                         upstream_model="us.anthropic.claude-x-v1:0"))
    provider._client = fake
    return provider


def client_error(code: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "InvokeModel")


def test_bedrock_messages_calls_invoke_model_with_upstream_id():
    fake = FakeBedrockRuntime()
    asyncio.run(bedrock(fake).messages(body(), []))
    assert fake.calls[0]["modelId"] == "us.anthropic.claude-x-v1:0"
    assert json.loads(fake.calls[0]["body"])["anthropic_version"] == "bedrock-2023-05-31"


def test_bedrock_stream_strips_invocation_metrics():
    async def collect():
        return [e async for e in bedrock(FakeBedrockRuntime()).messages_stream(body(), [])]

    events = asyncio.run(collect())
    assert [e["type"] for e in events] == ["message_start", "message_stop"]
    assert "amazon-bedrock-invocationMetrics" not in events[-1]


def test_bedrock_count_tokens_adds_required_max_tokens():
    fake = FakeBedrockRuntime()
    payload = {k: v for k, v in body().items() if k != "max_tokens"}
    assert asyncio.run(bedrock(fake).count_tokens(payload, [])) == 42
    assert fake.calls[0]["modelId"] == "anthropic.claude-x-v1:0"  # base model, not the profile
    assert json.loads(fake.calls[0]["input"]["invokeModel"]["body"])["max_tokens"] == 1


def test_bedrock_validation_errors_pass_through_scrubbed():
    fake = FakeBedrockRuntime(client_error(
        "ValidationException", "context_management: Extra inputs are not permitted "
        "for arn:aws:bedrock:us-west-2:123456789012:inference-profile/x"))
    with pytest.raises(ProviderError) as exc:
        asyncio.run(bedrock(fake).messages(body(), []))
    assert exc.value.status == 400
    assert "Extra inputs are not permitted" in exc.value.client_message
    assert "123456789012" not in exc.value.client_message


@pytest.mark.parametrize(("code", "status"), [("AccessDeniedException", 502),
                                              ("ThrottlingException", 429)])
def test_bedrock_other_errors_stay_generic(code, status):
    fake = FakeBedrockRuntime(client_error(code, "User arn:aws:sts::123456789012:assumed-role/r"))
    with pytest.raises(ProviderError) as exc:
        asyncio.run(bedrock(fake).messages(body(), []))
    assert exc.value.status == status
    assert "123456789012" not in (exc.value.client_message or "")


class FakeLease:
    """Stands in for a Vault-leased STS credential: a new dict after every invalidate()."""

    def __init__(self):
        self.issued = 0
        self.current = None

    def get(self):
        if self.current is None:
            self.issued += 1
            self.current = {"access_key": f"ASIA{self.issued}", "secret_key": "s",
                            "security_token": "t"}
        return self.current

    def invalidate(self):
        self.current = None


def test_expired_vault_credentials_are_replaced_and_the_call_retried(monkeypatch):
    import boto3
    # The cache still held credentials that AWS considers expired (laptop slept, VM paused).
    clients = [FakeBedrockRuntime(error=client_error("ExpiredTokenException", "token expired")),
               FakeBedrockRuntime()]
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: clients.pop(0))
    lease = FakeLease()
    provider = BedrockProvider(ModelSpec(name="claude", provider="bedrock", region="us-west-2",
                                         upstream_model="us.anthropic.claude-x-v1:0",
                                         vault_path="aws/creds/bedrock"), lease)
    assert asyncio.run(provider.messages(body(), []))["type"] == "message"
    assert lease.issued == 2 and clients == []


def test_expired_credentials_without_vault_are_not_retried():
    fake = FakeBedrockRuntime(error=client_error("ExpiredTokenException", "token expired"))
    with pytest.raises(ProviderError):
        asyncio.run(bedrock(fake).messages(body(), []))
    assert len(fake.calls) == 1
