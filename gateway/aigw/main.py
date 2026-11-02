"""AI Gateway HTTP service: OpenAI-compatible API with per-team auth, policy, and audit.

Run locally:
    GATEWAY_CONFIG=../policies/gateway.example.yaml uvicorn --factory aigw.main:create_app --reload
"""

from __future__ import annotations

import json
import logging
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
from .config import GatewayConfig, ModelSpec, TeamSpec, load_config
from .errors import GatewayError
from .limits import BodySizeLimit
from .mcp import build_mcp_router
from .messages import build_messages_router
from .metering import Meter, cost_usd
from .oidc import OidcError, OidcVerifier, looks_like_jwt
from .providers import Provider, ProviderError, build_provider
from .ratelimit import RateLimiter
from .schemas import ChatCompletionRequest
from .store import MemoryStore, RedisStore, Store, StoreError
from .vault import VaultClient

DEFAULT_CONFIG = "/etc/aigw/gateway.yaml"
log = logging.getLogger("aigw")
UPSTREAM_ERROR = "upstream provider error; see request_id in the gateway audit log"
# Gateway error kinds in Anthropic's vocabulary, by (status, kind) or kind.
ANTHROPIC_ERROR_TYPES = {
    (404, "invalid_request_error"): "not_found_error",
    "insufficient_quota": "rate_limit_error",
    "upstream_error": "api_error",
    "service_unavailable": "overloaded_error",
}


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


def create_app(
    config: GatewayConfig | None = None,
    providers: dict[str, Provider] | None = None,
    limiter: RateLimiter | None = None,
    mcp_http: httpx.AsyncClient | None = None,
    oidc: OidcVerifier | None = None,
) -> FastAPI:
    config = config or load_config(os.environ.get("GATEWAY_CONFIG", DEFAULT_CONFIG))
    models = {m.name: m for m in config.models}
    vault = VaultClient(config.vault) if config.vault else None
    providers = providers or {m.name: build_provider(m, vault) for m in config.models}
    authenticator = Authenticator(config.teams)
    if oidc is None and config.oidc:
        oidc = OidcVerifier(config.oidc)
    store: Store = (RedisStore(config.store.url, config.store.key_prefix, tls=config.store.tls)
                    if config.store.backend == "redis" else MemoryStore())
    limiter = limiter or RateLimiter(store)
    meter = Meter(store)
    if config.mode == "development" and (issues := config.production_issues()):
        # Development mode starts anyway, but says what production mode would refuse (ADR-0008).
        log.warning("aigw: development mode; not for shared environments:\n  - %s",
                    "\n  - ".join(issues))

    app = FastAPI(title="AI Gateway", version="0.1.0")
    app.add_middleware(BodySizeLimit, max_bytes=config.max_request_bytes,
                       overrides={"/v1/messages": config.max_messages_request_bytes})

    def error_response(request: Request, status: int, message: str, kind: str,
                       headers: dict[str, str] | None = None) -> JSONResponse:
        # Anthropic-format clients (Claude Code) expect Anthropic's error envelope.
        if request.url.path.startswith("/v1/messages"):
            kind = ANTHROPIC_ERROR_TYPES.get((status, kind), ANTHROPIC_ERROR_TYPES.get(kind, kind))
            body: dict[str, Any] = {"type": "error", "error": {"type": kind, "message": message}}
        else:
            body = {"error": {"message": message, "type": kind}}
        return JSONResponse(body, status_code=status, headers=headers)

    @app.exception_handler(GatewayError)
    async def _gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        return error_response(request, exc.status, exc.message, exc.kind, exc.headers)

    @app.exception_handler(StoreError)
    async def _store_error(request: Request, exc: StoreError) -> JSONResponse:
        # Fail closed: without the store there are no rate limits or budgets.
        return error_response(request, 503, str(exc), "service_unavailable")

    async def current_team(request: Request) -> TeamSpec:
        audit.set_user(None)
        # Failed auth is audited too, but never with the presented token.
        denied = {"event": "auth", "path": request.url.path,
                  "client": request.client.host if request.client else None,
                  "status": "denied", "http_status": 401}
        # OpenAI clients send `Authorization: Bearer`; Anthropic clients may send `x-api-key`.
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            token = request.headers.get("x-api-key", "")
        token = token.strip()
        if not token:
            audit.record(**denied, reason="missing_token")
            raise GatewayError(401, "missing api key", "authentication_error")
        if oidc is not None and looks_like_jwt(token):
            return await signed_in_team(oidc, token, denied)
        team = authenticator.authenticate(token)
        if team is None:
            audit.record(**denied, reason="invalid_api_key")
            raise GatewayError(401, "invalid api key", "authentication_error")
        return team

    async def signed_in_team(verifier: OidcVerifier, token: str,
                             denied: dict[str, Any]) -> TeamSpec:
        """A user signed in with the identity provider acts as the team their groups map to."""
        try:
            claims = await verifier.verify(token)
        except OidcError as exc:
            if exc.reason == "jwks_unavailable":
                audit.record(**{**denied, "http_status": 503}, reason=exc.reason)
                raise GatewayError(503, "identity provider unavailable",
                                   "service_unavailable") from exc
            audit.record(**denied, reason=exc.reason)
            # Claude Code reruns its apiKeyHelper on 401, which refreshes an expired token.
            raise GatewayError(401, f"sign-in token rejected: {exc.reason}",
                               "authentication_error") from exc
        user, groups = verifier.user(claims), verifier.groups(claims)
        audit.set_user(user)
        team = next((t for t in config.teams if groups & set(t.oidc_groups)), None)
        if team is None:
            audit.record(**{**denied, "http_status": 403}, reason="no_team_for_groups")
            raise GatewayError(403, "signed in, but none of your groups grants gateway access",
                               "permission_error")
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

    async def admit(team: TeamSpec, name: Any, base: dict[str, Any]) -> ModelSpec:
        """Policy checks shared by both APIs; each refusal is audited with its reason."""
        if name not in models:
            audit.record(**base, status="denied", http_status=404, reason="unknown_model")
            raise GatewayError(404, f"model {name!r} not found", "invalid_request_error")
        if not team.allows(name):
            audit.record(**base, status="denied", http_status=403, reason="model_not_allowed")
            raise GatewayError(403, f"team not allowed to use {name!r}", "permission_error")
        allowed, retry_after = await limiter.check(team.name, team.requests_per_minute)
        if not allowed:
            audit.record(**base, status="denied", http_status=429, reason="rate_limited")
            raise GatewayError(429, "rate limit exceeded", "rate_limit_error",
                               headers={"Retry-After": str(retry_after)})
        model = models[name]
        # Budgets limit spend, so they only gate models that cost something.
        if model.price is not None and await meter.over_budget(team):
            audit.record(**base, status="denied", http_status=429, reason="budget_exceeded")
            budget = team.budget
            # Retry-After over 60 s tells Claude Code to stop retrying and show the error.
            raise GatewayError(429, f"team {team.name!r} has used its budget of "
                               f"${budget.usd:g} per {budget.period}", "insufficient_quota",
                               headers={"Retry-After": "3600"})
        return model

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(
        req: ChatCompletionRequest, team: TeamSpec = Depends(current_team)
    ) -> dict[str, Any] | StreamingResponse:
        request_id = f"chatcmpl-{uuid.uuid4().hex}"
        base = {"request_id": request_id, "team": team.name, "model": req.model,
                "stream": req.stream}
        model = await admit(team, req.model, base)

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
                status, chunks, tokens, reason = "ok", 0, {}, None
                with upstream_span() as span:
                    try:
                        async for event in provider.stream(req, request_id):
                            chunks += 1
                            tokens = event.get("usage") or tokens
                            yield _sse(event)
                        yield "data: [DONE]\n\n"
                    except ProviderError as exc:
                        # Upstream errors can carry ARNs, account IDs, or internal paths: clients
                        # get a generic message, the audit line keeps the detail.
                        status, reason = "error", str(exc)[:200]
                        yield _sse({"error": {"message": UPSTREAM_ERROR,
                                              "type": "upstream_error"}})
                    finally:
                        prompt = tokens.get("prompt_tokens")
                        completion = tokens.get("completion_tokens")
                        cost = cost_usd(model, prompt, completion)
                        end_span(span, status, prompt, completion, cost)
                        extra = {"reason": reason} if reason else {}
                        audit.record(**base, status=status, http_status=200,
                                     latency_ms=elapsed_ms(), chunks=chunks, prompt_tokens=prompt,
                                     completion_tokens=completion, cost_usd=cost, **extra)
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
                raise GatewayError(502, UPSTREAM_ERROR, "upstream_error",
                                   headers={"X-Request-Id": request_id}) from exc

            usage = result.get("usage") or {}
            prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
            cost = cost_usd(model, prompt, completion)
            end_span(span, "ok", prompt, completion, cost)
            audit.record(**base, status="ok", http_status=200, latency_ms=elapsed_ms(),
                         prompt_tokens=prompt, completion_tokens=completion, cost_usd=cost)
        await meter.charge(team, cost)
        return result

    app.include_router(build_messages_router(models, providers, current_team, admit, meter))
    app.include_router(build_mcp_router(config, current_team, limiter, store, vault, mcp_http))
    tracing.setup(app)
    return app
