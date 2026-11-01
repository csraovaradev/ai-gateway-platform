"""What the gateway does when its dependencies fail (threat model: DoS, info disclosure)."""

import json
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from aigw.auth import hash_key
from aigw.config import GatewayConfig, ModelSpec, StoreSpec, VaultSpec
from aigw.main import create_app
from aigw.providers import ProviderError
from aigw.providers.bedrock import BedrockProvider

KEY = "fail-key"
LEAKY = ("AccessDenied: arn:aws:sts::123456789012:assumed-role/aigw-bedrock/vault-token "
         "is not authorized")
HEADERS = {"Authorization": f"Bearer {KEY}"}


def config(models: list[dict], **extra) -> GatewayConfig:
    return GatewayConfig.model_validate({
        "models": models,
        "teams": [{"name": "t", "key_sha256": [hash_key(KEY)], "allowed_tools": extra.pop(
            "allowed_tools", [])}],
        **extra,
    })


def chat(model: str, stream: bool = False) -> dict:
    return {"model": model, "stream": stream, "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def black_hole():
    """A TCP server that accepts connections and never answers: a hung upstream."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    held = []
    stop = threading.Event()

    def accept():
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                held.append(server.accept()[0])
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}"
    stop.set()
    thread.join()
    for conn in held:
        conn.close()
    server.close()


class Leaky:
    kind = "leaky"

    async def complete(self, req, request_id):
        raise ProviderError(LEAKY)

    async def stream(self, req, request_id):
        raise ProviderError(LEAKY)
        yield  # pragma: no cover


def test_upstream_errors_do_not_leak_to_clients(caplog):
    app = create_app(config([{"name": "m", "provider": "mock"}]), providers={"m": Leaky()})
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = TestClient(app).post("/v1/chat/completions", json=chat("m"), headers=HEADERS)
    assert resp.status_code == 502
    assert "123456789012" not in resp.text and "arn:" not in resp.text
    assert resp.headers["X-Request-Id"].startswith("chatcmpl-")
    record = json.loads(caplog.records[-1].getMessage())
    assert "123456789012" in record["reason"]  # operators still see the detail


def test_streamed_upstream_errors_do_not_leak_to_clients(caplog):
    app = create_app(config([{"name": "m", "provider": "mock"}]), providers={"m": Leaky()})
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = TestClient(app).post("/v1/chat/completions", json=chat("m", stream=True),
                                    headers=HEADERS)
    assert "upstream_error" in resp.text and "123456789012" not in resp.text
    assert "123456789012" in json.loads(caplog.records[-1].getMessage())["reason"]


def test_vault_outage_only_affects_vault_backed_models(monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", "t")
    cfg = config([{"name": "mock-echo", "provider": "mock"},
                  {"name": "bedrock", "provider": "bedrock", "upstream_model": "id",
                   "region": "us-west-2", "vault_path": "aws/creds/bedrock"}],
                 vault={"address": "http://127.0.0.1:1"})
    client = TestClient(create_app(cfg))
    down = client.post("/v1/chat/completions", json=chat("bedrock"), headers=HEADERS)
    assert down.status_code == 502
    assert "aws/creds" not in down.text and "127.0.0.1" not in down.text
    assert client.post("/v1/chat/completions", json=chat("mock-echo"),
                       headers=HEADERS).status_code == 200


def test_vault_outage_for_mcp_credentials_is_generic(monkeypatch, caplog):
    monkeypatch.setenv("VAULT_TOKEN", "t")
    cfg = config([], vault={"address": "http://127.0.0.1:1"}, allowed_tools=["jira/*"],
                 mcp_servers=[{"name": "jira", "url": "http://127.0.0.1:1/mcp",
                               "vault_path": "secret/data/aigw/mcp/jira"}])
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = TestClient(create_app(cfg)).post("/mcp/jira", headers=HEADERS, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 502
    assert resp.json()["error"]["message"] == "mcp server credentials unavailable"
    record = json.loads(caplog.records[-1].getMessage())
    assert (record["server"], record["reason"]) == ("jira", "vault unreachable: ConnectError")


def test_hung_openai_compat_upstream_times_out(black_hole):
    cfg = config([{"name": "slow", "provider": "openai_compat", "base_url": black_hole + "/v1",
                   "timeout_seconds": 0.3}])
    started = time.monotonic()
    resp = TestClient(create_app(cfg)).post("/v1/chat/completions", json=chat("slow"),
                                            headers=HEADERS)
    assert resp.status_code == 502
    assert time.monotonic() - started < 3


def test_hung_mcp_server_times_out(black_hole, caplog):
    cfg = config([], allowed_tools=["slow/*"], mcp_servers=[
        {"name": "slow", "url": black_hole + "/mcp", "timeout_seconds": 0.3}])
    started = time.monotonic()
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = TestClient(create_app(cfg)).post("/mcp/slow", headers=HEADERS, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}})
    assert resp.status_code == 502
    assert time.monotonic() - started < 3
    record = json.loads(caplog.records[-1].getMessage())
    assert (record["status"], record["tool"]) == ("error", "x")


def test_bedrock_client_has_bounded_timeouts_and_retries():
    provider = BedrockProvider(ModelSpec(name="b", provider="bedrock", upstream_model="id",
                                         region="us-west-2", timeout_seconds=30))
    cfg = provider._bedrock().meta.config
    assert (cfg.connect_timeout, cfg.read_timeout) == (5, 30)
    assert cfg.retries == {"total_max_attempts": 2, "mode": "standard"}  # 1 try + 1 retry


def test_oversized_bodies_are_rejected():
    client = TestClient(create_app(config([{"name": "m", "provider": "mock"}],
                                          max_request_bytes=1000)))
    big = chat("m") | {"messages": [{"role": "user", "content": "x" * 2000}]}
    resp = client.post("/v1/chat/completions", json=big, headers=HEADERS)
    assert resp.status_code == 413
    assert resp.json()["error"]["type"] == "invalid_request_error"

    def chunked():  # no Content-Length: the limit applies while reading
        yield json.dumps(big).encode()

    resp = client.post("/v1/chat/completions", content=chunked(),
                       headers=HEADERS | {"Content-Type": "application/json"})
    assert resp.status_code == 413
    assert client.post("/v1/chat/completions", json=chat("m"), headers=HEADERS).status_code == 200


def test_tls_settings_are_validated():
    with pytest.raises(ValueError, match="go together"):
        VaultSpec(tls={"client_cert": "c.pem"})
    with pytest.raises(ValueError, match="rediss://"):
        StoreSpec(backend="redis", url="redis://r:6379/0", tls={"ca_cert": "ca.pem"})
