"""Minimal Vault client over HTTP: token or Kubernetes auth, plus cached leased reads (ADR-0001).

Errors never include secret values, only paths and Vault's own error messages.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from .config import VaultSpec

KV_REFRESH_SECONDS = 300  # KV secrets have no lease; re-read periodically to pick up rotation


class VaultError(Exception):
    """Vault was unreachable or refused a request."""


class VaultClient:
    def __init__(
        self,
        spec: VaultSpec,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spec = spec
        self._http = http or httpx.Client(timeout=spec.timeout_seconds)
        self._clock = clock
        self._token: str | None = None
        self._token_expires = 0.0
        self._lock = threading.Lock()

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.spec.address.rstrip('/')}/v1/{path.lstrip('/')}"
        try:
            resp = self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise VaultError(f"vault unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code >= 400:
            try:
                detail = " ".join("; ".join(resp.json().get("errors", [])).split())[:200]
            except ValueError:
                detail = ""
            raise VaultError(f"vault returned {resp.status_code} for {path}: {detail}".rstrip(": "))
        return resp.json()

    def _login(self) -> None:
        if self.spec.auth == "token":
            token = os.environ.get(self.spec.token_env)
            if not token:
                raise VaultError(f"vault token auth: {self.spec.token_env} is not set")
            self._token, self._token_expires = token, float("inf")
            return
        jwt = Path(self.spec.jwt_path).read_text().strip()
        auth = self._call(
            "POST",
            f"auth/{self.spec.kubernetes_mount}/login",
            json={"role": self.spec.kubernetes_role, "jwt": jwt},
        )["auth"]
        ttl = auth.get("lease_duration") or 0
        self._token = auth["client_token"]
        self._token_expires = self._clock() + ttl * 0.9 if ttl else float("inf")

    def _ensure_token(self) -> str:
        with self._lock:
            if self._token is None or self._clock() >= self._token_expires:
                self._login()
            return self._token or ""

    def read(self, path: str) -> dict[str, Any]:
        """GET a Vault path and return the full response (data, lease_duration, ...)."""
        token = self._ensure_token()
        try:
            return self._call("GET", path, headers={"X-Vault-Token": token})
        except VaultError as exc:
            # A Kubernetes-auth token can be revoked early; log in again once.
            if self.spec.auth != "kubernetes" or " 403 " not in str(exc):
                raise
            with self._lock:
                self._token = None
            return self._call("GET", path, headers={"X-Vault-Token": self._ensure_token()})


class LeasedSecret:
    """A Vault secret cached until two thirds of its lease has elapsed, then re-read.

    `get()` returns the same dict object until a refresh, so callers can detect rotation
    with an identity check.
    """

    def __init__(
        self,
        vault: VaultClient,
        path: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.vault, self.path, self._clock = vault, path, clock
        self._data: dict[str, Any] | None = None
        self._refresh_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> dict[str, Any]:
        with self._lock:
            if self._data is None or self._clock() >= self._refresh_at:
                resp = self.vault.read(self.path)
                data = resp.get("data") or {}
                # KV v2 nests the secret under data.data.
                if isinstance(data.get("data"), dict) and "metadata" in data:
                    data = data["data"]
                lease = resp.get("lease_duration") or KV_REFRESH_SECONDS
                self._data = data
                self._refresh_at = self._clock() + lease * 2 / 3
            return self._data
