"""Gateway configuration: models (where requests go) and teams (who may send them)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

ProviderKind = Literal["mock", "openai_compat", "bedrock"]


class ModelSpec(BaseModel):
    """A model exposed by the gateway, mapped to an upstream provider."""

    name: str
    provider: ProviderKind
    upstream_model: str | None = None  # model id at the provider; defaults to `name`
    base_url: str | None = None  # openai_compat only
    api_key_env: str | None = None  # openai_compat only: env var holding the upstream key
    region: str | None = None  # bedrock only
    timeout_seconds: float = 120.0

    @model_validator(mode="after")
    def _check_provider_fields(self) -> ModelSpec:
        if self.provider == "openai_compat" and not self.base_url:
            raise ValueError(f"model {self.name!r}: openai_compat requires base_url")
        if self.provider == "bedrock" and not self.upstream_model:
            raise ValueError(f"model {self.name!r}: bedrock requires upstream_model")
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
    models: list[ModelSpec]
    teams: list[TeamSpec]

    @model_validator(mode="after")
    def _check_references(self) -> GatewayConfig:
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
