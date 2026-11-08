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
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import anyio
import httpx
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry.trace import Span, SpanKind, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import audit, metrics, tracing
from .auth import Authenticator
from .config import GatewayConfig, ModelSpec, OidcSpec, TeamSpec, load_config
from .errors import GatewayError
from .guardrails import ContentGuard
from .limits import BodySizeLimit
from .mcp import build_mcp_router
from .messages import build_messages_router
from .metering import Meter, cost_usd
from .oidc import (
    OidcError,
    OidcVerifier,
    TokenExchanger,
    looks_like_jwt,
    set_subject_token,
)
from .providers import Provider, ProviderError, build_provider
from .ratelimit import RateLimiter
from .routing import CircuitBreaker, Route, Router, audit_fields, first_event
from .schemas import ChatCompletionRequest
from .store import MemoryStore, RedisStore, Store, StoreError
from .vault import LeasedSecret, VaultClient

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


async def _chain(first: list[Any], rest: AsyncIterator[Any]) -> AsyncIterator[Any]:
    for item in first:
        yield item
    async for item in rest:
        yield item


def client_secret(spec: OidcSpec, vault: VaultClient | None) -> Callable[[], Awaitable[str]]:
    """The gateway's OIDC client secret: from Vault KV in production, an env var on a laptop."""
    if spec.client_secret_vault_path and vault is not None:
        secret = LeasedSecret(vault, spec.client_secret_vault_path)

        async def from_vault() -> str:
            return str((await anyio.to_thread.run_sync(secret.get)).get("client_secret", ""))
        return from_vault

    async def from_env() -> str:
        return os.environ.get(spec.client_secret_env or "", "")
    return from_env


def create_app(
    config: GatewayConfig | None = None,
    providers: dict[str, Provider] | None = None,
    limiter: RateLimiter | None = None,
    mcp_http: httpx.AsyncClient | None = None,
    oidc: OidcVerifier | None = None,
    exchanger: TokenExchanger | None = None,
) -> FastAPI:
    config = config or load_config(os.environ.get("GATEWAY_CONFIG", DEFAULT_CONFIG))
    models = {m.name: m for m in config.models}
    vault = VaultClient(config.vault) if config.vault else None
    providers = providers or {m.name: build_provider(m, vault) for m in config.models}
    authenticator = Authenticator(config.teams)
    if oidc is None and config.oidc:
        oidc = OidcVerifier(config.oidc)
    if exchanger is None and config.oidc and config.oidc.can_exchange:
        exchanger = TokenExchanger(config.oidc, client_secret(config.oidc, vault))
    store: Store = (RedisStore(config.store.url, config.store.key_prefix, tls=config.store.tls)
                    if config.store.backend == "redis" else MemoryStore())
    limiter = limiter or RateLimiter(store)
    meter = Meter(store)
    router = Router(models, CircuitBreaker(config.circuit_breaker))
    guard = ContentGuard(config.content_rules)
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
        set_subject_token(None)
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
        set_subject_token(token)  # for exchanging into MCP-server tokens (ADR-0012)
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

    async def user_rate_ok(team: TeamSpec, user: str | None) -> tuple[bool, int]:
        """Per-person rate limit for signed-in users (ADR-0011); API-key callers have none."""
        if user is None or team.per_user.requests_per_minute is None:
            return True, 0
        return await limiter.check(f"{team.name}/user:{user}", team.per_user.requests_per_minute)

    async def admit(team: TeamSpec, name: Any, base: dict[str, Any]) -> ModelSpec:
        """Policy checks shared by both APIs; each refusal is audited with its reason."""
        if name not in models:
            audit.record(**base, status="denied", http_status=404, reason="unknown_model")
            raise GatewayError(404, f"model {name!r} not found", "invalid_request_error")
        if not team.allows(name):
            audit.record(**base, status="denied", http_status=403, reason="model_not_allowed")
            raise GatewayError(403, f"team not allowed to use {name!r}", "permission_error")
        user = audit.current_user()
        allowed, retry_after = await user_rate_ok(team, user)
        if not allowed:
            audit.record(**base, status="denied", http_status=429, reason="user_rate_limited")
            raise GatewayError(429, "your personal rate limit is exceeded", "rate_limit_error",
                               headers={"Retry-After": str(retry_after)})
        allowed, retry_after = await limiter.check(team.name, team.requests_per_minute)
        if not allowed:
            audit.record(**base, status="denied", http_status=429, reason="rate_limited")
            raise GatewayError(429, "rate limit exceeded", "rate_limit_error",
                               headers={"Retry-After": str(retry_after)})
        model = models[name]
        if model.price is not None and await meter.user_over_budget(team, user):
            audit.record(**base, status="denied", http_status=429, reason="user_budget_exceeded")
            personal = team.per_user.budget
            raise GatewayError(429, f"you have used your personal budget of ${personal.usd:g} "
                               f"per {personal.period}", "insufficient_quota",
                               headers={"Retry-After": "3600"})
        # Budgets limit spend, so they only gate models that cost something.
        if model.price is not None and await meter.over_budget(team):
            audit.record(**base, status="denied", http_status=429, reason="budget_exceeded")
            budget = team.budget
            # Retry-After over 60 s tells Claude Code to stop retrying and show the error.
            raise GatewayError(429, f"team {team.name!r} has used its budget of "
                               f"${budget.usd:g} per {budget.period}", "insufficient_quota",
                               headers={"Retry-After": "3600"})
        return model

    def screen(team: TeamSpec, base: dict[str, Any], body: dict[str, Any],
               fields: tuple[str, ...]) -> None:
        """Apply the team's content policy to a request body in place (ADR-0010). Findings go on
        every audit line of the request; a blocked request is refused here."""
        verdict = guard.screen(team.content, body, fields)
        base.update(verdict.audit_fields())
        if verdict.blocked_by:
            audit.record(**base, status="denied", http_status=400, reason="content_blocked")
            raise GatewayError(400, "request blocked by content policy: it contains "
                               f"{', '.join(verdict.blocked_by)}; remove it and retry",
                               "invalid_request_error")

    async def plan(team: TeamSpec, model: ModelSpec,
                   usable: Callable[[ModelSpec], bool] = lambda _: True) -> Route:
        """The models to try for a request (ADR-0009). A team over budget may still use a free
        model, but must not spill onto a priced fallback when that free model fails."""
        over = (model.price is None and any(models[f].price for f in model.fallbacks)
                and (await meter.over_budget(team)
                     or await meter.user_over_budget(team, audit.current_user())))
        return router.route(team, model.name, lambda m: usable(m) and not (over and m.price))

    @app.post("/v1/chat/completions", response_model=None)
    async def chat_completions(
        req: ChatCompletionRequest, team: TeamSpec = Depends(current_team)
    ) -> dict[str, Any] | StreamingResponse:
        request_id = f"chatcmpl-{uuid.uuid4().hex}"
        base = {"request_id": request_id, "team": team.name, "model": req.model,
                "stream": req.stream}
        model = await admit(team, req.model, base)
        user = audit.current_user()
        contents = {"messages": [{"content": m.content} for m in req.messages]}
        screen(team, base, contents, ("messages",))
        for message, screened in zip(req.messages, contents["messages"], strict=True):
            message.content = screened["content"]
        route = await plan(team, model)
        started = time.perf_counter()

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        def upstream_span():  # OTel GenAI semantic conventions
            return tracing.tracer.start_as_current_span(
                f"chat {model.name}", kind=SpanKind.CLIENT, attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": model.upstream_model or model.name,
                    "aigw.team": team.name, "aigw.request_id": request_id})

        def end_span(span: Span, served: ModelSpec | None, status: str, prompt: int | None,
                     completion: int | None, cost: float) -> None:
            if served is not None:
                span.set_attributes({"gen_ai.system": providers[served.name].kind,
                                     "gen_ai.response.model": served.upstream_model or served.name})
            span.set_attributes({"gen_ai.usage.input_tokens": prompt or 0,
                                 "gen_ai.usage.output_tokens": completion or 0,
                                 "aigw.cost_usd": cost})
            if status == "error":
                span.set_status(StatusCode.ERROR)

        def failed(span: Span, exc: ProviderError) -> GatewayError:
            # Upstream errors can carry ARNs, account IDs, or internal paths: clients get a
            # generic message, the audit line keeps the detail.
            end_span(span, None, "error", None, None, 0.0)
            audit.record(**base, status="error", http_status=502, latency_ms=elapsed_ms(),
                         reason=str(exc)[:200], **audit_fields(route, model.name, None))
            return GatewayError(502, UPSTREAM_ERROR, "upstream_error",
                                headers={"X-Request-Id": request_id})

        def response_headers(served: ModelSpec) -> dict[str, str]:
            # A fallback is never silent: the client can see which model answered.
            return {"X-Request-Id": request_id, "X-AIGW-Served-Model": served.name}

        if req.stream:
            # Wait for the first chunk before answering, so a model that fails to start can fall
            # back, and a request nobody can serve gets an HTTP error instead of a broken stream.
            try:
                (first, upstream), served = await router.call(
                    route, lambda m: first_event(providers[m.name].stream(req, request_id)))
            except ProviderError as exc:
                with upstream_span() as span:
                    raise failed(span, exc) from exc
            base["provider"] = providers[served.name].kind

            async def events() -> AsyncIterator[str]:
                status, chunks, tokens, reason = "ok", 0, {}, None
                with upstream_span() as span:
                    try:
                        pending = [first] if first is not None else []
                        async for event in _chain(pending, upstream):
                            chunks += 1
                            event["model"] = served.name
                            tokens = event.get("usage") or tokens
                            yield _sse(event)
                        yield "data: [DONE]\n\n"
                    except ProviderError as exc:
                        status, reason = "error", str(exc)[:200]
                        yield _sse({"error": {"message": UPSTREAM_ERROR,
                                              "type": "upstream_error"}})
                    finally:
                        prompt = tokens.get("prompt_tokens")
                        completion = tokens.get("completion_tokens")
                        cost = cost_usd(served, prompt, completion)
                        end_span(span, served, status, prompt, completion, cost)
                        extra = {"reason": reason} if reason else {}
                        audit.record(**base, status=status, http_status=200,
                                     latency_ms=elapsed_ms(), chunks=chunks,
                                     prompt_tokens=prompt, completion_tokens=completion,
                                     cost_usd=cost, **audit_fields(route, model.name, served),
                                     **extra)
                        await meter.charge(team, cost, user)

            return StreamingResponse(events(), media_type="text/event-stream",
                                     headers=response_headers(served))

        with upstream_span() as span:
            try:
                result, served = await router.call(
                    route, lambda m: providers[m.name].complete(req, request_id))
            except ProviderError as exc:
                raise failed(span, exc) from exc
            base["provider"] = providers[served.name].kind
            result["model"] = served.name
            usage = result.get("usage") or {}
            prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
            cost = cost_usd(served, prompt, completion)  # priced as the model that answered
            end_span(span, served, "ok", prompt, completion, cost)
            audit.record(**base, status="ok", http_status=200, latency_ms=elapsed_ms(),
                         prompt_tokens=prompt, completion_tokens=completion, cost_usd=cost,
                         **audit_fields(route, model.name, served))
        await meter.charge(team, cost, user)
        return JSONResponse(result, headers=response_headers(served))

    app.include_router(build_messages_router(models, providers, current_team, admit, meter,
                                             plan, router, screen))
    app.include_router(build_mcp_router(config, current_team, limiter, store, vault, mcp_http,
                                        user_rate_ok, exchanger))
    tracing.setup(app)
    return app
