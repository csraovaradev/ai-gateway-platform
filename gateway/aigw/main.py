"""AI Gateway HTTP service: OpenAI-compatible API with per-team auth, policy, and audit.

Run locally:
    GATEWAY_CONFIG=../policies/gateway.example.yaml uvicorn --factory aigw.main:create_app --reload
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import audit
from .auth import Authenticator
from .config import GatewayConfig, TeamSpec, load_config
from .errors import GatewayError
from .mcp import build_mcp_router
from .providers import Provider, ProviderError, build_provider
from .ratelimit import RateLimiter
from .schemas import ChatCompletionRequest
from .store import MemoryStore, RedisStore, Store, StoreError
from .vault import VaultClient

DEFAULT_CONFIG = "/etc/aigw/gateway.yaml"


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


def create_app(
    config: GatewayConfig | None = None,
    providers: dict[str, Provider] | None = None,
    limiter: RateLimiter | None = None,
    mcp_http: httpx.AsyncClient | None = None,
) -> FastAPI:
    config = config or load_config(os.environ.get("GATEWAY_CONFIG", DEFAULT_CONFIG))
    models = {m.name: m for m in config.models}
    vault = VaultClient(config.vault) if config.vault else None
    providers = providers or {m.name: build_provider(m, vault) for m in config.models}
    authenticator = Authenticator(config.teams)
    store: Store = (RedisStore(config.store.url, config.store.key_prefix)
                    if config.store.backend == "redis" else MemoryStore())
    limiter = limiter or RateLimiter(store)

    app = FastAPI(title="AI Gateway", version="0.1.0")

    @app.exception_handler(GatewayError)
    async def _gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        body = {"error": {"message": exc.message, "type": exc.kind}}
        return JSONResponse(body, status_code=exc.status, headers=exc.headers)

    @app.exception_handler(StoreError)
    async def _store_error(_: Request, exc: StoreError) -> JSONResponse:
        # Fail closed: without the store there are no rate limits or budgets.
        body = {"error": {"message": str(exc), "type": "service_unavailable"}}
        return JSONResponse(body, status_code=503)

    def current_team(request: Request) -> TeamSpec:
        # Failed auth is audited too, but never with the presented token.
        denied = {"event": "auth", "path": request.url.path,
                  "client": request.client.host if request.client else None,
                  "status": "denied", "http_status": 401}
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            audit.record(**denied, reason="missing_token")
            raise GatewayError(401, "missing bearer token", "authentication_error")
        team = authenticator.authenticate(token.strip())
        if team is None:
            audit.record(**denied, reason="invalid_api_key")
            raise GatewayError(401, "invalid api key", "authentication_error")
        return team

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_models(team: TeamSpec = Depends(current_team)) -> dict[str, Any]:
        data = [
            {"id": name, "object": "model", "owned_by": spec.provider}
            for name, spec in models.items()
            if team.allows(name)
        ]
        return {"object": "list", "data": data}

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(
        req: ChatCompletionRequest, team: TeamSpec = Depends(current_team)
    ) -> dict[str, Any] | StreamingResponse:
        request_id = f"chatcmpl-{uuid.uuid4().hex}"
        base = {"request_id": request_id, "team": team.name, "model": req.model,
                "stream": req.stream}

        if req.model not in models:
            audit.record(**base, status="denied", http_status=404, reason="unknown_model")
            raise GatewayError(404, f"model {req.model!r} not found", "invalid_request_error")
        if not team.allows(req.model):
            audit.record(**base, status="denied", http_status=403, reason="model_not_allowed")
            raise GatewayError(403, f"team not allowed to use {req.model!r}", "permission_error")
        allowed, retry_after = await limiter.check(team.name, team.requests_per_minute)
        if not allowed:
            audit.record(**base, status="denied", http_status=429, reason="rate_limited")
            raise GatewayError(429, "rate limit exceeded", "rate_limit_error",
                               headers={"Retry-After": str(retry_after)})

        provider = providers[req.model]
        base["provider"] = provider.kind
        started = time.perf_counter()

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        if req.stream:
            async def events() -> AsyncIterator[str]:
                status, chunks, tokens = "ok", 0, {}
                try:
                    async for event in provider.stream(req, request_id):
                        chunks += 1
                        tokens = event.get("usage") or tokens
                        yield _sse(event)
                    yield "data: [DONE]\n\n"
                except ProviderError as exc:
                    status = "error"
                    yield _sse({"error": {"message": str(exc), "type": "upstream_error"}})
                finally:
                    audit.record(**base, status=status, http_status=200,
                                 latency_ms=elapsed_ms(), chunks=chunks,
                                 prompt_tokens=tokens.get("prompt_tokens"),
                                 completion_tokens=tokens.get("completion_tokens"))

            return StreamingResponse(events(), media_type="text/event-stream",
                                     headers={"X-Request-Id": request_id})

        try:
            result = await provider.complete(req, request_id)
        except ProviderError as exc:
            audit.record(**base, status="error", http_status=502, latency_ms=elapsed_ms(),
                         reason=str(exc)[:200])
            raise GatewayError(502, str(exc), "upstream_error") from exc

        usage = result.get("usage") or {}
        audit.record(**base, status="ok", http_status=200, latency_ms=elapsed_ms(),
                     prompt_tokens=usage.get("prompt_tokens"),
                     completion_tokens=usage.get("completion_tokens"))
        return result

    app.include_router(build_mcp_router(config, current_team, limiter, store, vault, mcp_http))
    return app
