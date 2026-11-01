from ..config import ModelSpec
from ..vault import LeasedSecret, VaultClient
from .base import Provider, ProviderError
from .bedrock import BedrockProvider
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider


def build_provider(spec: ModelSpec, vault: VaultClient | None = None) -> Provider:
    secret = LeasedSecret(vault, spec.vault_path) if vault and spec.vault_path else None
    if spec.provider == "mock":
        return MockProvider(spec)
    if spec.provider == "openai_compat":
        return OpenAICompatProvider(spec, api_key=secret)
    if spec.provider == "bedrock":
        return BedrockProvider(spec, credentials=secret)
    raise ValueError(f"unknown provider {spec.provider!r}")


__all__ = ["Provider", "ProviderError", "build_provider"]
