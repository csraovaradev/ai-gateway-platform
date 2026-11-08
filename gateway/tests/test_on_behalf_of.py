"""MCP calls on behalf of the signed-in person (ADR-0012): token exchange, never the raw token."""

import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_mcp import FakeMcpServer, headers, rpc
from test_oidc import ISSUER, JWKS_URL, FakeIdp, token, verifier

from aigw.auth import hash_key
from aigw.config import GatewayConfig, OidcSpec
from aigw.main import create_app
from aigw.oidc import OidcError, TokenExchanger

KEY = "service-key"
TOKEN_URL = "http://idp.internal/token"


class FakeTokenEndpoint:
    """Answers RFC 8693 token exchange like Keycloak, and records the requests."""

    def __init__(self, status: int = 200):
        self.status, self.requests = status, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"token_endpoint": TOKEN_URL})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append((request.headers.get("authorization"), form))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "access_denied"})
        who = form["subject_token"][-6:]
        return httpx.Response(200, json={"access_token": f"for-{form['audience']}-{who}",
                                         "expires_in": 900})


def config(**oidc):
    return GatewayConfig.model_validate({
        "oidc": {"issuer": ISSUER, "audience": "aigw", "jwks_url": JWKS_URL,
                 "user_claim": "preferred_username", "client_id": "aigw",
                 "client_secret_env": "AIGW_TEST_CLIENT_SECRET", "token_url": TOKEN_URL, **oidc},
        "models": [{"name": "m", "provider": "mock"}],
        "mcp_servers": [{"name": "jira", "url": "http://jira-mcp/mcp", "on_behalf_of": "jira-mcp"}],
        "teams": [{"name": "platform", "oidc_groups": ["eng-platform"],
                   "key_sha256": [hash_key(KEY)], "allowed_tools": ["*"]}],
    })


def gateway(endpoint, monkeypatch, **oidc):
    monkeypatch.setenv("AIGW_TEST_CLIENT_SECRET", "s3cret")
    cfg = config(**oidc)
    mcp = FakeMcpServer()
    exchanger = TokenExchanger(cfg.oidc, lambda: _secret(),
                               http=httpx.AsyncClient(transport=httpx.MockTransport(endpoint)))
    gw = TestClient(create_app(cfg, oidc=verifier(FakeIdp()), exchanger=exchanger,
                               mcp_http=httpx.AsyncClient(transport=httpx.MockTransport(mcp))))
    return gw, mcp


async def _secret():
    return "s3cret"


CALL = rpc("tools/call", name="get_issue", arguments={"key": "OPS-1"})


def test_signed_in_person_reaches_the_server_with_an_exchanged_token(monkeypatch):
    endpoint = FakeTokenEndpoint()
    gw, mcp = gateway(endpoint, monkeypatch)
    alice = token(preferred_username="alice")
    assert gw.post("/mcp/jira", json=CALL, headers=headers(alice)).status_code == 200
    forwarded = mcp.requests[-1].headers["authorization"]
    assert forwarded == f"Bearer for-jira-mcp-{alice[-6:]}"
    assert alice not in forwarded  # the person's own token never leaves the gateway
    client_auth, form = endpoint.requests[-1]
    assert client_auth.startswith("Basic ")  # the gateway authenticates as its own client
    assert form["subject_token"] == alice and form["audience"] == "jira-mcp"
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"


def test_exchanged_tokens_are_cached_per_person(monkeypatch):
    endpoint = FakeTokenEndpoint()
    gw, mcp = gateway(endpoint, monkeypatch)
    alice, bob = token(preferred_username="alice"), token(preferred_username="bob", sub="u-2")
    for _ in range(3):
        gw.post("/mcp/jira", json=CALL, headers=headers(alice))
    gw.post("/mcp/jira", json=CALL, headers=headers(bob))
    assert len(endpoint.requests) == 2  # one exchange each for alice and bob


def test_api_key_callers_do_not_exchange(monkeypatch):
    endpoint = FakeTokenEndpoint()
    gw, mcp = gateway(endpoint, monkeypatch)
    assert gw.post("/mcp/jira", json=CALL, headers=headers(KEY)).status_code == 200
    assert endpoint.requests == []
    assert "authorization" not in mcp.requests[-1].headers  # and their key is never forwarded


def test_refused_exchange_fails_closed_and_is_audited(monkeypatch, caplog):
    gw, mcp = gateway(FakeTokenEndpoint(status=403), monkeypatch)
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = gw.post("/mcp/jira", json=CALL, headers=headers(token()))
    assert resp.status_code == 502 and mcp.requests == []
    line = [json.loads(r.getMessage()) for r in caplog.records if r.name == "aigw.audit"][-1]
    assert line["reason"] == "token_exchange_refused"


def test_token_endpoint_found_through_discovery_and_cache_expires():
    endpoint, now = FakeTokenEndpoint(), [1000.0]
    spec = OidcSpec(issuer=ISSUER, audience="aigw", client_id="aigw", client_secret_env="X")
    exchanger = TokenExchanger(spec, _secret, clock=lambda: now[0],
                               http=httpx.AsyncClient(transport=httpx.MockTransport(endpoint)))

    async def scenario():
        await exchanger.exchange("subject-token-abcdef", "jira-mcp")
        await exchanger.exchange("subject-token-abcdef", "jira-mcp")
        assert len(endpoint.requests) == 1
        now[0] += 900 - 29  # inside the last 30 s of the token's life: exchange again
        await exchanger.exchange("subject-token-abcdef", "jira-mcp")
        assert len(endpoint.requests) == 2
        endpoint.status = 401
        with pytest.raises(OidcError, match="token_exchange_refused"):
            await exchanger.exchange("another-subject-token", "jira-mcp")

    asyncio.run(scenario())


def test_on_behalf_of_needs_a_gateway_client():
    with pytest.raises(ValidationError, match="on_behalf_of"):
        GatewayConfig.model_validate({
            "oidc": {"issuer": ISSUER, "audience": "aigw"},
            "models": [{"name": "m", "provider": "mock"}],
            "mcp_servers": [{"name": "jira", "url": "http://j/mcp", "on_behalf_of": "jira-mcp"}],
            "teams": [{"name": "t", "key_sha256": ["x"]}]})


def test_client_secret_from_env_is_not_production_ready():
    issues = config().production_issues()
    assert any("client secret" in i for i in issues)
