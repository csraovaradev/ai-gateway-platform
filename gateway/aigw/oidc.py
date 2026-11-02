"""Verify sign-in tokens from the company identity provider (ADR-0007).

The gateway never sees passwords. The identity provider signs a short-lived JWT; the gateway checks
the signature against the provider's published keys (JWKS) and then trusts the claims in it:
who the user is (`sub`) and which groups they belong to.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt

from .config import OidcSpec

REFETCH_MIN_SECONDS = 30  # an unknown key ID refetches the JWKS at most this often


class OidcError(Exception):
    """The token was rejected; `reason` is a short code for the audit log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def looks_like_jwt(token: str) -> bool:
    # Team API keys never contain dots; a JWT is three base64url parts starting with `{"`.
    return token.startswith("eyJ") and token.count(".") == 2


class OidcVerifier:
    def __init__(
        self,
        spec: OidcSpec,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spec = spec
        verify = spec.tls.context() if spec.tls.configured else True
        self._http = http or httpx.AsyncClient(timeout=spec.timeout_seconds, verify=verify)
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at = self._attempted_at = float("-inf")
        self._lock = asyncio.Lock()

    async def _get_json(self, url: str) -> dict[str, Any]:
        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError("jwks_unavailable") from exc

    async def _refresh_keys(self) -> None:
        url = self.spec.jwks_url
        if url is None:
            discovery = f"{self.spec.issuer.rstrip('/')}/.well-known/openid-configuration"
            url = (await self._get_json(discovery)).get("jwks_uri")
            if not url:
                raise OidcError("jwks_unavailable")
        keys = {}
        for entry in (await self._get_json(url)).get("keys", []):
            if entry.get("use", "sig") != "sig" or "kid" not in entry:
                continue
            try:
                keys[entry["kid"]] = jwt.PyJWK(entry)
            except jwt.PyJWKError:
                continue  # key types or algorithms we do not support, e.g. encryption keys
        self._keys, self._fetched_at = keys, self._clock()

    async def _key(self, kid: str) -> jwt.PyJWK:
        now = self._clock()
        # Providers rotate keys by publishing a new one first, so an unknown kid means refetch,
        # but at most every REFETCH_MIN_SECONDS: a flood of made-up kids, or an identity provider
        # outage, must not turn into a flood of fetches.
        due = now - self._fetched_at > self.spec.jwks_cache_seconds or kid not in self._keys
        if due and now - self._attempted_at > REFETCH_MIN_SECONDS:
            async with self._lock:
                if self._attempted_at < now:  # another request may have refreshed while we waited
                    self._attempted_at = self._clock()
                    try:
                        await self._refresh_keys()
                    except OidcError:
                        if not self._keys:
                            raise
                        # Identity provider unreachable: keep verifying with the keys we have.
        if not self._keys:
            raise OidcError("jwks_unavailable")
        if kid not in self._keys:
            raise OidcError("unknown_signing_key")
        return self._keys[kid]

    async def verify(self, token: str) -> dict[str, Any]:
        """Return the token's claims, or raise OidcError."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise OidcError("malformed_token") from exc
        if header.get("alg") not in self.spec.algorithms:
            raise OidcError("unsupported_algorithm")
        key = await self._key(str(header.get("kid", "")))
        try:
            return jwt.decode(
                token, key, algorithms=list(self.spec.algorithms),
                audience=self.spec.audience, issuer=self.spec.issuer,
                leeway=self.spec.leeway_seconds,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise OidcError("token_expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise OidcError("wrong_audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise OidcError("wrong_issuer") from exc
        except jwt.InvalidSignatureError as exc:
            raise OidcError("bad_signature") from exc
        except jwt.InvalidTokenError as exc:
            raise OidcError("invalid_token") from exc

    def groups(self, claims: dict[str, Any]) -> set[str]:
        value = claims.get(self.spec.groups_claim) or []
        if isinstance(value, str):
            value = [value]
        return {g for g in value if isinstance(g, str)}

    def user(self, claims: dict[str, Any]) -> str:
        return str(claims.get(self.spec.user_claim) or claims["sub"])
