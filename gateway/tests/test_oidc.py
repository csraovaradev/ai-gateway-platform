"""Sign-in with an identity provider (ADR-0007): JWKS-verified tokens, groups map to teams."""

import asyncio
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from aigw.auth import hash_key
from aigw.config import GatewayConfig, OidcSpec
from aigw.main import create_app
from aigw.oidc import OidcError, OidcVerifier

ISSUER = "https://idp.example.com/realms/aigw"
JWKS_URL = "http://idp.internal/certs"
PLATFORM_KEY = "test-platform-key"


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNING_KEY = new_key()


def jwk(key: rsa.RSAPrivateKey, kid: str) -> dict:
    return {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
            "kid": kid, "use": "sig", "alg": "RS256"}


class FakeIdp:
    """Serves discovery and JWKS like Keycloak or Entra ID, and counts fetches."""

    def __init__(self, keys: list[dict] | None = None):
        self.keys = keys if keys is not None else [jwk(SIGNING_KEY, "k1")]
        self.fetches, self.down = 0, False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("identity provider down")
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URL})
        self.fetches += 1
        return httpx.Response(200, json={"keys": self.keys})


def token(key: rsa.RSAPrivateKey = SIGNING_KEY, kid: str = "k1", alg: str = "RS256",
          **claims) -> str:
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": "aigw", "sub": "u-123", "preferred_username": "alice",
               "groups": ["eng-platform"], "iat": now, "exp": now + 900, **claims}
    return jwt.encode(payload, key, algorithm=alg, headers={"kid": kid})


def make_config(**oidc) -> GatewayConfig:
    return GatewayConfig.model_validate({
        "oidc": {"issuer": ISSUER, "audience": "aigw", "jwks_url": JWKS_URL,
                 "user_claim": "preferred_username", **oidc},
        "models": [{"name": "mock-echo", "provider": "mock"},
                   {"name": "mock-premium", "provider": "mock"}],
        "mcp_servers": [{"name": "jira", "url": "http://jira-mcp/mcp"}],
        "teams": [
            {"name": "platform", "key_sha256": [hash_key(PLATFORM_KEY)],
             "oidc_groups": ["eng-platform"], "allowed_tools": ["*"]},
            {"name": "data", "oidc_groups": ["eng-data"], "allowed_models": ["mock-echo"]},
        ],
    })


def verifier(idp: FakeIdp, clock=time.monotonic, **oidc) -> OidcVerifier:
    spec = make_config(**oidc).oidc
    return OidcVerifier(spec, http=httpx.AsyncClient(transport=httpx.MockTransport(idp)),
                        clock=clock)


def client(idp: FakeIdp | None = None, **oidc) -> TestClient:
    return TestClient(create_app(make_config(**oidc), oidc=verifier(idp or FakeIdp(), **oidc)))


def chat(bearer: str, model: str = "mock-echo") -> httpx.Response:
    return client().post("/v1/chat/completions", headers={"Authorization": f"Bearer {bearer}"},
                         json={"model": model, "messages": [{"role": "user", "content": "hi"}]})


def audit_lines(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "aigw.audit"]


def test_signed_in_user_acts_as_their_team_and_is_audited(caplog):
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = chat(token())
    assert resp.status_code == 200
    line = audit_lines(caplog)[-1]
    assert (line["team"], line["user"], line["status"]) == ("platform", "alice", "ok")


def test_anthropic_clients_can_send_the_token_as_x_api_key():
    resp = client().post("/v1/messages", headers={"x-api-key": token()}, json={
        "model": "mock-echo", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200


def test_group_decides_the_team_and_its_policy():
    assert chat(token(groups=["eng-data"]), model="mock-premium").status_code == 403
    assert chat(token(groups=["eng-data"])).status_code == 200


def test_user_without_a_mapped_group_is_refused(caplog):
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = chat(token(groups=["sales"]))
    assert resp.status_code == 403
    assert resp.json()["error"]["type"] == "permission_error"
    line = audit_lines(caplog)[-1]
    assert (line["reason"], line["user"]) == ("no_team_for_groups", "alice")


def test_api_keys_keep_working_next_to_sign_in(caplog):
    with caplog.at_level("INFO", logger="aigw.audit"):
        assert chat(PLATFORM_KEY).status_code == 200
    assert "user" not in audit_lines(caplog)[-1]


@pytest.mark.parametrize(("bad_token", "reason"), [
    (token(exp=int(time.time()) - 120), "token_expired"),
    (token(aud="some-other-app"), "wrong_audience"),
    (token(iss="https://evil.example.com"), "wrong_issuer"),
    (token(key=new_key()), "bad_signature"),  # right kid, wrong key
    (token(kid="k-unknown"), "unknown_signing_key"),
    (jwt.encode({"sub": "x", "iss": ISSUER, "aud": "aigw"}, "shared-secret-key-32-bytes-long!!",
                algorithm="HS256", headers={"kid": "k1"}), "unsupported_algorithm"),
    ("eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0.", "unsupported_algorithm"),  # alg: none
    ("eyJnot.a.jwt", "malformed_token"),
])
def test_rejected_tokens_get_401_and_an_audited_reason(bad_token, reason, caplog):
    with caplog.at_level("INFO", logger="aigw.audit"):
        resp = chat(bad_token)
    assert resp.status_code == 401
    assert resp.json()["error"]["message"] == f"sign-in token rejected: {reason}"
    line = audit_lines(caplog)[-1]
    assert (line["event"], line["reason"]) == ("auth", reason)
    assert bad_token not in caplog.text


def test_keys_are_cached_and_unknown_kids_refetch_at_most_every_30s():
    async def scenario():
        idp, now = FakeIdp(), [1000.0]
        v = verifier(idp, clock=lambda: now[0])
        for _ in range(5):
            await v.verify(token())
        assert idp.fetches == 1
        # Key rotation: the provider publishes k2; the first k2 token triggers a refetch.
        k2 = new_key()
        idp.keys.append(jwk(k2, "k2"))
        now[0] += 31
        await v.verify(token(key=k2, kid="k2"))
        assert idp.fetches == 2
        # Made-up kids do not each cause a fetch.
        for i in range(20):
            with pytest.raises(OidcError, match="unknown_signing_key"):
                await v.verify(token(kid=f"made-up-{i}"))
        assert idp.fetches == 2

    asyncio.run(scenario())




def test_cached_keys_survive_an_identity_provider_outage():
    async def scenario():
        idp, now = FakeIdp(), [1000.0]
        v = verifier(idp, clock=lambda: now[0])
        await v.verify(token())
        idp.down, now[0] = True, now[0] + 3600  # cache expired and the refresh fails
        assert (await v.verify(token()))["sub"] == "u-123"

    asyncio.run(scenario())




def test_identity_provider_down_before_any_keys_is_503():
    idp = FakeIdp()
    idp.down = True
    resp = client(idp).post("/v1/chat/completions", headers={"Authorization": f"Bearer {token()}"},
                            json={"model": "mock-echo",
                                  "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 503


def test_discovery_document_finds_the_jwks_when_no_url_is_set():
    async def scenario():
        idp = FakeIdp()
        spec = OidcSpec(issuer=ISSUER, audience="aigw")
        v = OidcVerifier(spec, http=httpx.AsyncClient(transport=httpx.MockTransport(idp)))
        assert (await v.verify(token()))["sub"] == "u-123"
        assert idp.fetches == 1

    asyncio.run(scenario())




def test_symmetric_algorithms_cannot_be_configured():
    with pytest.raises(ValueError, match="algorithms"):
        OidcSpec(issuer=ISSUER, audience="aigw", algorithms=["HS256"])


def test_mcp_sessions_belong_to_the_signed_in_user():
    from test_mcp import FakeMcpServer, headers, rpc

    mcp_http = httpx.AsyncClient(transport=httpx.MockTransport(FakeMcpServer()))
    gw = TestClient(create_app(make_config(), mcp_http=mcp_http, oidc=verifier(FakeIdp())))
    alice, bob = token(preferred_username="alice"), token(preferred_username="bob")

    opened = gw.post("/mcp/jira", json=rpc("initialize"), headers=headers(alice))
    sid = opened.headers["mcp-session-id"]
    # Same team, different person: the session is not theirs.
    stolen = gw.post("/mcp/jira", json=rpc("tools/list"),
                     headers=headers(bob, **{"mcp-session-id": sid}))
    assert stolen.status_code == 404
    own = gw.post("/mcp/jira", json=rpc("tools/list"),
                  headers=headers(alice, **{"mcp-session-id": sid}))
    assert own.status_code == 200
