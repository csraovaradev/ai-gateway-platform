"""Gateway configuration: models (where requests go) and teams (who may send them)."""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from .tls import TLSSpec

ProviderKind = Literal["mock", "openai_compat", "bedrock"]
# Asymmetric only: the verification keys come from a public JWKS, so HS* and "none" never apply.
JwtAlgorithm = Literal["RS256", "RS384", "RS512", "PS256", "ES256", "ES384"]
# SHA-256 of the public demo keys `dev-platform-key` and `dev-data-key` (README).
DEMO_KEY_HASHES = frozenset({
    "9831e20a4d38b6fdfa34285772c4b88de26d51134c15107cb941d53b70859e63",
    "33ba9b2d29663d7a3a4043d971cd244b328042768f2dcb1a7ef430be104e8dd1",
})


class VaultSpec(BaseModel):
    """How the gateway reaches Vault for upstream credentials (ADR-0001)."""

    address: str = "http://127.0.0.1:8200"
    auth: Literal["token", "kubernetes"] = "token"
    token_env: str = "VAULT_TOKEN"  # noqa: S105 - name of the env var holding the token
    kubernetes_role: str | None = None  # kubernetes auth: Vault role bound to the service account
    kubernetes_mount: str = "kubernetes"
    jwt_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    timeout_seconds: float = 10.0
    tls: TLSSpec = Field(default_factory=TLSSpec)

    @model_validator(mode="after")
    def _check_auth_fields(self) -> VaultSpec:
        if self.auth == "kubernetes" and not self.kubernetes_role:
            raise ValueError("vault: kubernetes auth requires kubernetes_role")
        return self


class OidcSpec(BaseModel):
    """Users sign in with the company identity provider; the gateway checks its tokens
    (ADR-0007)."""

    issuer: str  # must equal the token's `iss` exactly, e.g. https://login.example.com/realms/aigw
    audience: str  # the token's `aud` must include this
    # Where the signing keys are. Default: `jwks_uri` from the issuer's discovery document.
    # Set it when the gateway reaches the identity provider by another name (containers, clusters).
    jwks_url: str | None = None
    groups_claim: str = "groups"  # list of group names (or IDs, in Entra ID) mapped to teams
    user_claim: str = "sub"  # recorded as `user` in the audit log
    algorithms: list[JwtAlgorithm] = Field(default_factory=lambda: ["RS256"], min_length=1)
    leeway_seconds: int = Field(default=30, ge=0, le=300)  # clock skew allowed on exp/nbf/iat
    jwks_cache_seconds: int = Field(default=300, gt=0)
    timeout_seconds: float = 10.0
    tls: TLSSpec = Field(default_factory=TLSSpec)


class Price(BaseModel):
    """USD per million tokens. Configuration, not code: prices change and differ by contract."""

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    # Prompt caching (Messages API): Anthropic bills cache writes at 1.25x and reads at 0.1x input.
    cache_write_per_mtok: float | None = Field(default=None, ge=0)
    cache_read_per_mtok: float | None = Field(default=None, ge=0)


class Budget(BaseModel):
    usd: float = Field(gt=0)
    period: Literal["day", "month"] = "month"  # UTC calendar day or month


class ModelSpec(BaseModel):
    """A model exposed by the gateway, mapped to an upstream provider."""

    name: str
    provider: ProviderKind
    upstream_model: str | None = None  # model id at the provider; defaults to `name`
    base_url: str | None = None  # openai_compat only
    api_key_env: str | None = None  # openai_compat only: env var holding the upstream key
    region: str | None = None  # bedrock only
    # Vault path for upstream credentials: AWS secrets engine creds for bedrock
    # (e.g. aws/creds/bedrock), or a KV v2 secret with an `api_key` field for openai_compat.
    vault_path: str | None = None
    price: Price | None = None  # without it the model is metered in tokens but costs 0
    timeout_seconds: float = 120.0
    # Other models (by name) to try, in order, when this one is throttled, down, or timing out
    # (ADR-0009). Only one level deep: a fallback's own fallbacks are not followed.
    fallbacks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_provider_fields(self) -> ModelSpec:
        if self.provider == "openai_compat" and not self.base_url:
            raise ValueError(f"model {self.name!r}: openai_compat requires base_url")
        if self.provider == "bedrock" and not self.upstream_model:
            raise ValueError(f"model {self.name!r}: bedrock requires upstream_model")
        if self.provider == "mock" and self.vault_path:
            raise ValueError(f"model {self.name!r}: mock takes no vault_path")
        if self.vault_path and self.api_key_env:
            raise ValueError(f"model {self.name!r}: set vault_path or api_key_env, not both")
        return self


class CircuitBreakerSpec(BaseModel):
    """Skip a model for a while after repeated upstream failures, going straight to fallbacks."""

    failure_threshold: int = Field(default=3, gt=0)  # consecutive retryable failures
    cooldown_seconds: float = Field(default=30.0, gt=0)  # then one trial request is let through


class StoreSpec(BaseModel):
    """Where shared state lives: rate limits, MCP sessions, spend (ADR-0003)."""

    backend: Literal["memory", "redis"] = "memory"
    url: str = "redis://127.0.0.1:6379/0"  # redis backend only; rediss:// for TLS
    key_prefix: str = "aigw:"
    tls: TLSSpec = Field(default_factory=TLSSpec)

    @model_validator(mode="after")
    def _check_tls(self) -> StoreSpec:
        if self.tls.configured and not self.url.startswith("rediss://"):
            raise ValueError("store: tls settings need a rediss:// url")
        return self


class McpServerSpec(BaseModel):
    """An upstream MCP server reached at /mcp/{name} (ADR-0002)."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    url: str  # Streamable HTTP endpoint, e.g. http://jira-mcp:8000/mcp
    vault_path: str | None = None  # KV v2 secret whose `token` is sent upstream as a bearer token
    timeout_seconds: float = 60.0


ContentAction = Literal["off", "audit", "redact", "block"]


class ContentPolicy(BaseModel):
    """What to do when a prompt contains a secret or personal data (ADR-0010)."""

    secrets: ContentAction = "redact"  # cloud keys, API tokens, private keys
    pii: ContentAction = "audit"  # email addresses, card numbers, US social security numbers


class ContentRuleSpec(BaseModel):
    """An extra detector, e.g. for internal hostnames or project code names."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    category: Literal["secrets", "pii"]
    pattern: str  # Python regular expression; a capture group limits redaction to that group

    @model_validator(mode="after")
    def _compiles(self) -> ContentRuleSpec:
        try:
            re.compile(self.pattern)
        except re.error as exc:
            raise ValueError(f"content rule {self.name!r}: invalid pattern: {exc}") from exc
        return self


class TeamSpec(BaseModel):
    """A team that calls the gateway, with API keys (stored only as SHA-256 hashes), identity
    provider groups, or both."""

    name: str
    key_sha256: list[str] = Field(default_factory=list)
    # Signed-in users whose token lists one of these groups act as this team. A user in groups of
    # several teams gets the first such team in this file.
    oidc_groups: list[str] = Field(default_factory=list)
    allowed_models: list[str] = Field(default_factory=lambda: ["*"])
    # MCP tools as `server/tool` glob patterns, e.g. "jira/get_*". Empty means no MCP access.
    allowed_tools: list[str] = Field(default_factory=list)
    requests_per_minute: int = Field(default=60, gt=0)
    budget: Budget | None = None
    content: ContentPolicy = Field(default_factory=ContentPolicy)

    @model_validator(mode="after")
    def _check_identity(self) -> TeamSpec:
        if not self.key_sha256 and not self.oidc_groups:
            raise ValueError(f"team {self.name!r} needs key_sha256 or oidc_groups")
        return self

    def allows(self, model: str) -> bool:
        return "*" in self.allowed_models or model in self.allowed_models

    def allows_server(self, server: str) -> bool:
        return any(fnmatchcase(server, p.split("/", 1)[0]) for p in self.allowed_tools)

    def allows_tool(self, server: str, tool: str) -> bool:
        return any(fnmatchcase(f"{server}/{tool}", p) for p in self.allowed_tools)


class GatewayConfig(BaseModel):
    # production refuses to start with settings that are only safe on a laptop (ADR-0008).
    mode: Literal["development", "production"] = "development"
    oidc: OidcSpec | None = None
    vault: VaultSpec | None = None
    store: StoreSpec = Field(default_factory=StoreSpec)
    circuit_breaker: CircuitBreakerSpec = Field(default_factory=CircuitBreakerSpec)
    models: list[ModelSpec]
    content_rules: list[ContentRuleSpec] = Field(default_factory=list)
    mcp_servers: list[McpServerSpec] = Field(default_factory=list)
    teams: list[TeamSpec]
    max_request_bytes: int = Field(default=1_048_576, gt=0)  # larger bodies get 413
    # Coding agents resend the whole conversation (tool results, images) on every turn.
    max_messages_request_bytes: int = Field(default=32 * 1_048_576, gt=0)

    @model_validator(mode="after")
    def _check_references(self) -> GatewayConfig:
        needs_vault = [m.name for m in self.models if m.vault_path]
        needs_vault += [s.name for s in self.mcp_servers if s.vault_path]
        if needs_vault and self.vault is None:
            raise ValueError(f"{needs_vault} use vault_path but no vault section is set")
        names = [m.name for m in self.models]
        if len(names) != len(set(names)):
            raise ValueError("model names must be unique")
        for model in self.models:
            bad = [f for f in model.fallbacks if f not in names or f == model.name]
            if bad or len(model.fallbacks) != len(set(model.fallbacks)):
                raise ValueError(f"model {model.name!r}: fallbacks must be other, distinct, "
                                 f"known models (got {model.fallbacks})")
        team_names = [t.name for t in self.teams]
        if len(team_names) != len(set(team_names)):
            raise ValueError("team names must be unique")
        known = set(names)
        for team in self.teams:
            unknown = [m for m in team.allowed_models if m != "*" and m not in known]
            if unknown:
                raise ValueError(f"team {team.name!r} allows unknown models: {unknown}")
        rule_names = [r.name for r in self.content_rules]
        if len(rule_names) != len(set(rule_names)):
            raise ValueError("content rule names must be unique")
        sso_teams = [t.name for t in self.teams if t.oidc_groups]
        if sso_teams and self.oidc is None:
            raise ValueError(f"teams {sso_teams} use oidc_groups but no oidc section is set")
        servers = [s.name for s in self.mcp_servers]
        if len(servers) != len(set(servers)):
            raise ValueError("mcp server names must be unique")
        for team in self.teams:
            for pattern in team.allowed_tools:
                prefix = pattern.split("/", 1)[0]
                if "/" not in pattern and pattern != "*":
                    raise ValueError(
                        f"team {team.name!r}: tool pattern {pattern!r} needs server/tool")
                if not any(fnmatchcase(s, prefix) for s in servers):
                    raise ValueError(f"team {team.name!r}: {pattern!r} matches no mcp server")
        if self.mode == "production" and (issues := self.production_issues()):
            raise ValueError("not safe for mode: production:\n  - " + "\n  - ".join(issues))
        return self

    def production_issues(self) -> list[str]:
        """Settings that are fine on a laptop but must not reach a shared environment."""
        issues = []
        demo = [t.name for t in self.teams if DEMO_KEY_HASHES & {h.lower() for h in t.key_sha256}]
        if demo:
            issues.append(f"teams {demo} use the public demo keys; generate keys with "
                          "`python -m aigw.keys <team>`")
        if self.store.backend == "memory":
            issues.append("store: memory keeps rate limits and budgets per process; use redis")
        elif not self.store.url.startswith("rediss://"):
            issues.append("store: use rediss:// so limits and spend travel over TLS")
        if self.vault is not None:
            if not self.vault.address.startswith("https://"):
                issues.append("vault: address must be https://")
            if self.vault.auth == "token":
                issues.append("vault: token auth needs a long-lived token; use kubernetes auth")
        if self.oidc is not None and not self.oidc.issuer.startswith("https://"):
            issues.append("oidc: issuer must be https://")
        static = [m.name for m in self.models if m.api_key_env]
        if static:
            issues.append(f"models {static} read upstream keys from env vars; use vault_path")
        return issues


def load_config(path: str | Path) -> GatewayConfig:
    with open(path) as fh:
        return GatewayConfig.model_validate(yaml.safe_load(fh))
