"""Gateway configuration: models (where requests go) and teams (who may send them)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

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

    @model_validator(mode="after")
    def _check_auth_fields(self) -> VaultSpec:
        if self.auth == "kubernetes" and not self.kubernetes_role:
            raise ValueError("vault: kubernetes auth requires kubernetes_role")
        return self


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


class TeamSpec(BaseModel):
    """A team that calls the gateway. Keys are stored only as SHA-256 hashes."""

    name: str
    key_sha256: list[str] = Field(min_length=1)
    allowed_models: list[str] = Field(default_factory=lambda: ["*"])
    requests_per_minute: int = Field(default=60, gt=0)

    def allows(self, model: str) -> bool:
        return "*" in self.allowed_models or model in self.allowed_models


class GatewayConfig(BaseModel):
    vault: VaultSpec | None = None
    models: list[ModelSpec]
    teams: list[TeamSpec]

    @model_validator(mode="after")
    def _check_references(self) -> GatewayConfig:
        needs_vault = [m.name for m in self.models if m.vault_path]
        if needs_vault and self.vault is None:
            raise ValueError(f"models {needs_vault} use vault_path but no vault section is set")
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
        return self


def load_config(path: str | Path) -> GatewayConfig:
    with open(path) as fh:
        return GatewayConfig.model_validate(yaml.safe_load(fh))
