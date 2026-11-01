"""TLS settings shared by the Vault and Redis clients (threat model: spoofing, tampering)."""

from __future__ import annotations

import ssl

from pydantic import BaseModel, model_validator


class TLSSpec(BaseModel):
    ca_cert: str | None = None  # trust only this CA bundle instead of the system store
    client_cert: str | None = None  # present a client certificate (mutual TLS)
    client_key: str | None = None

    @model_validator(mode="after")
    def _check_pair(self) -> TLSSpec:
        if bool(self.client_cert) != bool(self.client_key):
            raise ValueError("tls: client_cert and client_key go together")
        return self

    @property
    def configured(self) -> bool:
        return bool(self.ca_cert or self.client_cert)

    def context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cafile=self.ca_cert)
        if self.client_cert:
            ctx.load_cert_chain(self.client_cert, self.client_key)
        return ctx
