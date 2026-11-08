"""Verify sign-in tokens from the company identity provider (ADR-0007).

The gateway never sees passwords. The identity provider signs a short-lived JWT; the gateway checks
the signature against the provider's published keys (JWKS) and then trusts the claims in it:
who the user is (`sub`) and which groups they belong to.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

import httpx
import jwt

from .config import OidcSpec

REFETCH_MIN_SECONDS = 30  # an unknown key ID refetches the JWKS at most this often
EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105 - a URN
EXCHANGE_CACHE_SIZE = 1024
EXCHANGE_EARLY_SECONDS = 30  # re-exchange this long before a cached token expires

# The signed-in person's own token for this request, kept in memory only, so the MCP proxy can
# exchange it (ADR-0012). Never logged, never forwarded as is.
_subject_token: ContextVar[str | None] = ContextVar("aigw_subject_token", default=None)


def set_subject_token(token: str | None) -> None:
    _subject_token.set(token)


def current_subject_token() -> str | None:
    return _subject_token.get()


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


class TokenExchanger:
    """OAuth 2.0 Token Exchange (RFC 8693): trade a person's token for one the identity provider
    issues to a single downstream audience, still naming that person (ADR-0012).

    In AWS terms it is AssumeRole with session tags: a narrower credential that keeps the caller's
    identity. Exchanged tokens are cached per person and audience until shortly before expiry.
    """

    def __init__(
        self,
        spec: OidcSpec,
        client_secret: Callable[[], Awaitable[str]],
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # Token expiry is wall-clock time set by the identity provider, so the cache uses the
        # wall clock too (the lesson of ADR-0009's credential bug).
        self.spec, self._client_secret, self._clock = spec, client_secret, clock
        verify = spec.tls.context() if spec.tls.configured else True
        self._http = http or httpx.AsyncClient(timeout=spec.timeout_seconds, verify=verify)
        self._token_url = spec.token_url
        self._cache: OrderedDict[tuple[str, str], tuple[str, float]] = OrderedDict()

    async def _url(self) -> str:
        if self._token_url is None:
            discovery = f"{self.spec.issuer.rstrip('/')}/.well-known/openid-configuration"
            try:
                resp = await self._http.get(discovery)
                resp.raise_for_status()
                self._token_url = resp.json()["token_endpoint"]
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                raise OidcError("token_exchange_unavailable") from exc
        return self._token_url

    async def exchange(self, subject_token: str, audience: str) -> str:
        key = (hashlib.sha256(subject_token.encode()).hexdigest(), audience)
        now = self._clock()
        if (hit := self._cache.get(key)) and hit[1] - now > EXCHANGE_EARLY_SECONDS:
            self._cache.move_to_end(key)
            return hit[0]
        try:
            resp = await self._http.post(
                await self._url(), auth=(self.spec.client_id or "", await self._client_secret()),
                data={"grant_type": EXCHANGE_GRANT, "subject_token": subject_token,
                      "subject_token_type": ACCESS_TOKEN_TYPE,
                      "requested_token_type": ACCESS_TOKEN_TYPE, "audience": audience})
        except httpx.HTTPError as exc:
            raise OidcError("token_exchange_unavailable") from exc
        if resp.status_code != 200:
            raise OidcError("token_exchange_refused")
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise OidcError("token_exchange_refused")
        self._cache[key] = (token, now + float(body.get("expires_in") or 60))
        while len(self._cache) > EXCHANGE_CACHE_SIZE:
            self._cache.popitem(last=False)
        return token
