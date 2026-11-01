import asyncio
import json

import httpx
import pytest

from aigw.config import GatewayConfig, ModelSpec, VaultSpec
from aigw.providers.bedrock import BedrockProvider
from aigw.providers.openai_compat import OpenAICompatProvider
from aigw.schemas import ChatCompletionRequest
from aigw.vault import LeasedSecret, VaultClient, VaultError

REQ = ChatCompletionRequest.model_validate(
    {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
)
STS = {"access_key": "ASIAEXAMPLE", "secret_key": "s3cr3t", "security_token": "tok"}


class FakeVault:
    """Records requests and answers like Vault's HTTP API."""

    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/v1/")
        handler = self.routes.get(path)
        if handler is None:
            return httpx.Response(404, json={"errors": [f"no handler for {path}"]})
        return handler(request) if callable(handler) else httpx.Response(200, json=handler)

    def client(self, spec: VaultSpec, clock=None) -> VaultClient:
        http = httpx.Client(transport=httpx.MockTransport(self))
        return VaultClient(spec, http=http, **({"clock": clock} if clock else {}))


def test_token_auth_reads_with_header(monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", "dev-token")
    fake = FakeVault({"aws/creds/bedrock": {"data": STS, "lease_duration": 900}})
    resp = fake.client(VaultSpec()).read("aws/creds/bedrock")
    assert resp["data"] == STS
    assert fake.requests[0].headers["X-Vault-Token"] == "dev-token"


def test_token_auth_without_token_fails(monkeypatch):
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    with pytest.raises(VaultError, match="VAULT_TOKEN is not set"):
        FakeVault({}).client(VaultSpec()).read("aws/creds/bedrock")


def test_errors_carry_vault_message_not_secrets(monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", "dev-token")
    fake = FakeVault({"aws/creds/bedrock": lambda r: httpx.Response(
        403, json={"errors": ["permission denied"]})})
    with pytest.raises(VaultError) as exc:
        fake.client(VaultSpec()).read("aws/creds/bedrock")
    assert "403" in str(exc.value) and "permission denied" in str(exc.value)
    assert "dev-token" not in str(exc.value)


def test_kubernetes_auth_logs_in_and_relogs_after_expiry(tmp_path):
    jwt = tmp_path / "token"
    jwt.write_text("sa-jwt\n")
    logins = []

    def login(request):
        logins.append(json.loads(request.content))
        return httpx.Response(200, json={"auth": {"client_token": f"t{len(logins)}",
                                                  "lease_duration": 100}})

    now = [0.0]
    fake = FakeVault({"auth/kubernetes/login": login,
                      "secret/data/x": {"data": {"data": {"api_key": "k"}, "metadata": {}}}})
    spec = VaultSpec(auth="kubernetes", kubernetes_role="aigw", jwt_path=str(jwt))
    client = fake.client(spec, clock=lambda: now[0])

    client.read("secret/data/x")
    assert logins == [{"role": "aigw", "jwt": "sa-jwt"}]
    assert fake.requests[-1].headers["X-Vault-Token"] == "t1"

    now[0] = 95  # past 90% of the 100s token lease
    client.read("secret/data/x")
    assert len(logins) == 2
    assert fake.requests[-1].headers["X-Vault-Token"] == "t2"


def test_kubernetes_auth_relogs_once_on_403(tmp_path):
    jwt = tmp_path / "token"
    jwt.write_text("sa-jwt")
    tokens = iter(["revoked", "fresh"])
    fake = FakeVault({
        "auth/kubernetes/login": lambda r: httpx.Response(
            200, json={"auth": {"client_token": next(tokens), "lease_duration": 3600}}),
        "aws/creds/bedrock": lambda r: (
            httpx.Response(200, json={"data": STS, "lease_duration": 900})
            if r.headers["X-Vault-Token"] == "fresh"
            else httpx.Response(403, json={"errors": ["permission denied"]})),
    })
    spec = VaultSpec(auth="kubernetes", kubernetes_role="aigw", jwt_path=str(jwt))
    assert fake.client(spec).read("aws/creds/bedrock")["data"] == STS


def test_leased_secret_caches_until_two_thirds_of_lease(monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", "dev-token")
    issued = []

    def creds(request):
        issued.append(1)
        return httpx.Response(200, json={"data": {**STS, "access_key": f"ASIA{len(issued)}"},
                                         "lease_duration": 900})

    now = [0.0]
    fake = FakeVault({"aws/creds/bedrock": creds})
    secret = LeasedSecret(fake.client(VaultSpec()), "aws/creds/bedrock", clock=lambda: now[0])

    first = secret.get()
    now[0] = 599
    assert secret.get() is first  # still inside 2/3 of 900s
    now[0] = 600
    second = secret.get()
    assert second is not first and second["access_key"] == "ASIA2"


def test_leased_secret_unwraps_kv_v2(monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", "dev-token")
    fake = FakeVault({"secret/data/aigw/upstream": {
        "data": {"data": {"api_key": "sk-test"}, "metadata": {"version": 3}},
        "lease_duration": 0}})
    secret = LeasedSecret(fake.client(VaultSpec()), "secret/data/aigw/upstream")
    assert secret.get() == {"api_key": "sk-test"}


class StaticSecret:
    def __init__(self, data):
        self.data = data

    def get(self):
        return self.data


def test_bedrock_uses_vault_creds_and_rebuilds_client_on_rotation(monkeypatch):
    import boto3

    built = []
    monkeypatch.setattr(boto3, "client", lambda service, **kw: built.append(kw) or object())
    secret = StaticSecret(STS)
    provider = BedrockProvider(
        ModelSpec(name="b", provider="bedrock", upstream_model="id", region="us-west-2"),
        credentials=secret,
    )

    client = provider._bedrock()
    assert provider._bedrock() is client  # same creds, same client
    assert built[0]["aws_access_key_id"] == "ASIAEXAMPLE"
    assert built[0]["aws_session_token"] == "tok"

    secret.data = {**STS, "access_key": "ASIAROTATED"}
    assert provider._bedrock() is not client
    assert built[1]["aws_access_key_id"] == "ASIAROTATED"


def test_openai_compat_sends_vault_api_key():
    seen = {}

    def upstream(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [], "usage": {}})

    provider = OpenAICompatProvider(
        ModelSpec(name="m", provider="openai_compat", base_url="http://up/v1",
                  vault_path="secret/data/aigw/upstream"),
        httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        api_key=StaticSecret({"api_key": "sk-from-vault"}),
    )
    asyncio.run(provider.complete(REQ, "chatcmpl-v"))
    assert seen["auth"] == "Bearer sk-from-vault"


def test_config_requires_vault_section_for_vault_path():
    with pytest.raises(ValueError, match="no vault section"):
        GatewayConfig.model_validate({
            "models": [{"name": "b", "provider": "bedrock", "upstream_model": "id",
                        "vault_path": "aws/creds/bedrock"}],
            "teams": [{"name": "t", "key_sha256": ["x"]}],
        })


def test_config_kubernetes_auth_requires_role():
    with pytest.raises(ValueError, match="kubernetes_role"):
        VaultSpec(auth="kubernetes")
