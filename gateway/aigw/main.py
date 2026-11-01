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
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry.trace import Span, SpanKind, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import audit, metrics, tracing
from .auth import Authenticator
from .config import GatewayConfig, TeamSpec, load_config
from .errors import GatewayError
from .mcp import build_mcp_router
from .metering import Meter, cost_usd
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
    meter = Meter(store)

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

    # Scraped from inside the cluster; do not route it through the public ingress (ADR-0004).
    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        return Response(generate_latest(metrics.REGISTRY), media_type=CONTENT_TYPE_LATEST)

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
        model = models[req.model]
        # Budgets limit spend, so they only gate models that cost something.
        if model.price is not None and await meter.over_budget(team):
            audit.record(**base, status="denied", http_status=429, reason="budget_exceeded")
            budget = team.budget
            raise GatewayError(429, f"team {team.name!r} has used its budget of "
                               f"${budget.usd:g} per {budget.period}", "insufficient_quota")

        provider = providers[req.model]
        base["provider"] = provider.kind
        started = time.perf_counter()

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        def upstream_span():  # OTel GenAI semantic conventions
            return tracing.tracer.start_as_current_span(
                f"chat {model.name}", kind=SpanKind.CLIENT, attributes={
                    "gen_ai.operation.name": "chat", "gen_ai.system": provider.kind,
                    "gen_ai.request.model": model.upstream_model or model.name,
                    "aigw.team": team.name, "aigw.request_id": request_id})

        def end_span(span: Span, status: str, prompt: int | None, completion: int | None,
                     cost: float) -> None:
            span.set_attributes({"gen_ai.usage.input_tokens": prompt or 0,
                                 "gen_ai.usage.output_tokens": completion or 0,
                                 "aigw.cost_usd": cost})
            if status == "error":
                span.set_status(StatusCode.ERROR)

        if req.stream:
            async def events() -> AsyncIterator[str]:
                status, chunks, tokens = "ok", 0, {}
                with upstream_span() as span:
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
                        prompt = tokens.get("prompt_tokens")
                        completion = tokens.get("completion_tokens")
                        cost = cost_usd(model, prompt, completion)
                        end_span(span, status, prompt, completion, cost)
                        audit.record(**base, status=status, http_status=200,
                                     latency_ms=elapsed_ms(), chunks=chunks, prompt_tokens=prompt,
                                     completion_tokens=completion, cost_usd=cost)
                        await meter.charge(team, cost)

            return StreamingResponse(events(), media_type="text/event-stream",
                                     headers={"X-Request-Id": request_id})

        with upstream_span() as span:
            try:
                result = await provider.complete(req, request_id)
            except ProviderError as exc:
                end_span(span, "error", None, None, 0.0)
                audit.record(**base, status="error", http_status=502, latency_ms=elapsed_ms(),
                             reason=str(exc)[:200])
                raise GatewayError(502, str(exc), "upstream_error") from exc

            usage = result.get("usage") or {}
            prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
            cost = cost_usd(model, prompt, completion)
            end_span(span, "ok", prompt, completion, cost)
            audit.record(**base, status="ok", http_status=200, latency_ms=elapsed_ms(),
                         prompt_tokens=prompt, completion_tokens=completion, cost_usd=cost)
        await meter.charge(team, cost)
        return result

    app.include_router(build_mcp_router(config, current_team, limiter, store, vault, mcp_http))
    tracing.setup(app)
    return app
