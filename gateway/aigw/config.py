"""Gateway configuration: models (where requests go) and teams (who may send them)."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from .tls import TLSSpec

ProviderKind = Literal["mock", "openai_compat", "bedrock"]


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


class TeamSpec(BaseModel):
    """A team that calls the gateway. Keys are stored only as SHA-256 hashes."""

    name: str
    key_sha256: list[str] = Field(min_length=1)
    allowed_models: list[str] = Field(default_factory=lambda: ["*"])
    # MCP tools as `server/tool` glob patterns, e.g. "jira/get_*". Empty means no MCP access.
    allowed_tools: list[str] = Field(default_factory=list)
    requests_per_minute: int = Field(default=60, gt=0)
    budget: Budget | None = None

    def allows(self, model: str) -> bool:
        return "*" in self.allowed_models or model in self.allowed_models

    def allows_server(self, server: str) -> bool:
        return any(fnmatchcase(server, p.split("/", 1)[0]) for p in self.allowed_tools)

    def allows_tool(self, server: str, tool: str) -> bool:
        return any(fnmatchcase(f"{server}/{tool}", p) for p in self.allowed_tools)


class GatewayConfig(BaseModel):
    vault: VaultSpec | None = None
    store: StoreSpec = Field(default_factory=StoreSpec)
    models: list[ModelSpec]
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
        team_names = [t.name for t in self.teams]
        if len(team_names) != len(set(team_names)):
            raise ValueError("team names must be unique")
        known = set(names)
        for team in self.teams:
            unknown = [m for m in team.allowed_models if m != "*" and m not in known]
            if unknown:
                raise ValueError(f"team {team.name!r} allows unknown models: {unknown}")
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
        return self


def load_config(path: str | Path) -> GatewayConfig:
    with open(path) as fh:
        return GatewayConfig.model_validate(yaml.safe_load(fh))
