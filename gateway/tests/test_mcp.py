import json

import httpx
import pytest
from fastapi.testclient import TestClient

import aigw.main
from aigw.auth import hash_key
from aigw.config import GatewayConfig
from aigw.main import create_app
from aigw.vault import VaultClient

PLATFORM_KEY = "test-platform-key"
DATA_KEY = "test-data-key"
TOOLS = [{"name": "search", "inputSchema": {"type": "object"}},
         {"name": "get_issue", "inputSchema": {"type": "object"}},
         {"name": "delete_issue", "inputSchema": {"type": "object"}}]


def make_config(**server_extra) -> GatewayConfig:
    return GatewayConfig.model_validate({
        **({"vault": {}} if "vault_path" in server_extra else {}),
        "models": [{"name": "mock-echo", "provider": "mock"}],
        "mcp_servers": [{"name": "jira", "url": "http://jira-mcp/mcp", **server_extra},
                        {"name": "admin", "url": "http://admin-mcp/mcp"}],
        "teams": [
            {"name": "platform", "key_sha256": [hash_key(PLATFORM_KEY)],
             "allowed_tools": ["*"], "requests_per_minute": 100},
            {"name": "data", "key_sha256": [hash_key(DATA_KEY)],
             "allowed_tools": ["jira/search", "jira/get_*"], "requests_per_minute": 2},
        ],
    })


class FakeMcpServer:
    """Answers like an MCP server and records what the gateway forwarded."""

    def __init__(self, sse: bool = False, is_error: bool = False):
        self.sse, self.is_error, self.requests = sse, is_error, []

    def reply(self, payload: dict, headers: dict | None = None) -> httpx.Response:
        if self.sse:
            text = (f'data: {json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"})}'
                    f"\n\ndata: {json.dumps(payload)}\n\n")
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream",
                                                           **(headers or {})})
        return httpx.Response(200, json=payload, headers=headers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(200)
        msg = json.loads(request.content)
        if "id" not in msg:
            return httpx.Response(202)
        result: dict = {}
        headers = None
        if msg["method"] == "initialize":
            headers = {"mcp-session-id": "sess-123"}
        elif msg["method"] == "tools/list":
            result = {"tools": [dict(t) for t in TOOLS]}
        elif msg["method"] == "tools/call":
            result = {"content": [{"type": "text", "text": "secret result"}],
                      "isError": self.is_error}
        return self.reply({"jsonrpc": "2.0", "id": msg["id"], "result": result}, headers)


def gateway(fake: FakeMcpServer, config: GatewayConfig | None = None) -> TestClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return TestClient(create_app(config or make_config(), mcp_http=http))


def rpc(method: str, rpc_id: int | None = 1, **params) -> dict:
    msg = {"jsonrpc": "2.0", "method": method, "params": params}
    return msg if rpc_id is None else {**msg, "id": rpc_id}


def headers(key: str, **extra) -> dict:
    return {"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream",
            **extra}


def sse_messages(text: str) -> list[dict]:
    return [json.loads(ln[5:]) for ln in text.splitlines() if ln.startswith("data:")]


@pytest.mark.parametrize("sse", [False, True], ids=["json", "sse"])
def test_tools_list_is_filtered_per_team(sse):
    client = gateway(FakeMcpServer(sse=sse))
    resp = client.post("/mcp/jira", json=rpc("tools/list"), headers=headers(DATA_KEY))
    assert resp.status_code == 200
    reply = sse_messages(resp.text)[-1] if sse else resp.json()
    assert [t["name"] for t in reply["result"]["tools"]] == ["search", "get_issue"]

    resp = client.post("/mcp/jira", json=rpc("tools/list"), headers=headers(PLATFORM_KEY))
    reply = sse_messages(resp.text)[-1] if sse else resp.json()
    assert len(reply["result"]["tools"]) == 3


def test_allowed_tool_call_is_forwarded_without_client_key(caplog):
    fake = FakeMcpServer()
    client = gateway(fake)
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = client.post("/mcp/jira", headers=headers(DATA_KEY),
                           json=rpc("tools/call", name="get_issue", arguments={"key": "OPS-1"}))
    assert resp.json()["result"]["content"][0]["text"] == "secret result"
    forwarded = fake.requests[0]
    assert "authorization" not in forwarded.headers
    assert json.loads(forwarded.content)["params"]["arguments"] == {"key": "OPS-1"}
    record = json.loads(caplog.records[-1].getMessage())
    assert record | {"ts": None, "latency_ms": None} == {
        "ts": None, "event": "mcp", "team": "data", "server": "jira", "method": "tools/call",
        "tool": "get_issue", "status": "ok", "http_status": 200, "latency_ms": None,
        "tool_error": False}
    assert "OPS-1" not in caplog.text and "secret result" not in caplog.text


def test_denied_tool_call_never_reaches_server(caplog):
    fake = FakeMcpServer()
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = gateway(fake).post("/mcp/jira", headers=headers(DATA_KEY),
                                  json=rpc("tools/call", rpc_id=7, name="delete_issue"))
    assert resp.status_code == 200
    assert resp.json() == {"jsonrpc": "2.0", "id": 7, "error": {
        "code": -32003, "message": "tool 'delete_issue' is not allowed for team 'data'"}}
    assert fake.requests == []
    assert json.loads(caplog.records[-1].getMessage())["reason"] == "tool_not_allowed"


@pytest.mark.parametrize("extra", [{"Mcp-Name": "get_issue"}, {"Mcp-Method": "tools/list"}],
                         ids=["name", "method"])
def test_header_body_mismatch_is_rejected(extra):
    fake = FakeMcpServer()
    resp = gateway(fake).post("/mcp/jira", headers=headers(DATA_KEY, **extra),
                              json=rpc("tools/call", name="delete_issue"))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32020
    assert fake.requests == []


def test_matching_mirror_headers_are_forwarded():
    fake = FakeMcpServer()
    gateway(fake).post("/mcp/jira", json=rpc("tools/call", name="search"),
                       headers=headers(DATA_KEY, **{"Mcp-Method": "tools/call",
                                                    "Mcp-Name": "search",
                                                    "Mcp-Param-Project": "OPS"}))
    forwarded = fake.requests[0].headers
    assert forwarded["mcp-method"] == "tools/call" and forwarded["mcp-name"] == "search"
    assert forwarded["mcp-param-project"] == "OPS"


def test_server_outside_allowlist_is_403():
    fake = FakeMcpServer()
    resp = gateway(fake).post("/mcp/admin", json=rpc("tools/list"), headers=headers(DATA_KEY))
    assert resp.status_code == 403
    assert fake.requests == []


def test_unknown_server_is_404():
    assert gateway(FakeMcpServer()).post(
        "/mcp/nope", json=rpc("tools/list"), headers=headers(DATA_KEY)).status_code == 404


def test_sessions_are_bound_to_the_team_that_opened_them():
    fake = FakeMcpServer()
    client = gateway(fake)
    resp = client.post("/mcp/jira", json=rpc("initialize"), headers=headers(DATA_KEY))
    assert resp.headers["mcp-session-id"] == "sess-123"

    same_team = client.post("/mcp/jira", json=rpc("tools/list"),
                            headers=headers(DATA_KEY, **{"Mcp-Session-Id": "sess-123"}))
    assert same_team.status_code == 200

    hijack = client.post("/mcp/jira", json=rpc("tools/list"),
                         headers=headers(PLATFORM_KEY, **{"Mcp-Session-Id": "sess-123"}))
    assert hijack.status_code == 404
    assert client.delete("/mcp/jira", headers=headers(
        PLATFORM_KEY, **{"Mcp-Session-Id": "sess-123"})).status_code == 404

    assert client.delete("/mcp/jira", headers=headers(
        DATA_KEY, **{"Mcp-Session-Id": "sess-123"})).status_code == 200
    after_close = client.post("/mcp/jira", json=rpc("tools/list"),
                              headers=headers(DATA_KEY, **{"Mcp-Session-Id": "sess-123"}))
    assert after_close.status_code == 404


def test_notifications_pass_through_as_202():
    resp = gateway(FakeMcpServer()).post(
        "/mcp/jira", json=rpc("notifications/initialized", rpc_id=None), headers=headers(DATA_KEY))
    assert resp.status_code == 202


def test_batches_are_rejected():
    fake = FakeMcpServer()
    resp = gateway(fake).post("/mcp/jira", json=[rpc("tools/list"), rpc("tools/list", 2)],
                              headers=headers(DATA_KEY))
    assert resp.status_code == 400
    assert fake.requests == []


def test_tool_calls_are_rate_limited():
    client = gateway(FakeMcpServer())
    codes = [client.post("/mcp/jira", json=rpc("tools/call", name="search"),
                         headers=headers(DATA_KEY)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_tool_error_in_sse_stream_is_audited(caplog):
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = gateway(FakeMcpServer(sse=True, is_error=True)).post(
            "/mcp/jira", json=rpc("tools/call", name="search"), headers=headers(DATA_KEY))
    assert sse_messages(resp.text)[-1]["result"]["isError"] is True
    record = json.loads(caplog.records[-1].getMessage())
    assert record["tool_error"] is True and record["status"] == "ok"


def test_upstream_token_comes_from_vault(monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", "dev-token")
    vault_http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        "data": {"data": {"token": "jira-svc-token"}, "metadata": {}}})))
    monkeypatch.setattr(aigw.main, "VaultClient",
                        lambda spec: VaultClient(spec, http=vault_http))
    fake = FakeMcpServer()
    gateway(fake, make_config(vault_path="secret/data/aigw/mcp/jira")).post(
        "/mcp/jira", json=rpc("tools/list"), headers=headers(DATA_KEY))
    assert fake.requests[0].headers["authorization"] == "Bearer jira-svc-token"


def test_config_rejects_tool_patterns_for_unknown_servers():
    with pytest.raises(ValueError, match="matches no mcp server"):
        GatewayConfig.model_validate({
            "models": [], "mcp_servers": [{"name": "jira", "url": "http://x/mcp"}],
            "teams": [{"name": "t", "key_sha256": ["x"], "allowed_tools": ["github/*"]}]})
