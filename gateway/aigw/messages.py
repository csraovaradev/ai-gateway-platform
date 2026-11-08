"""Anthropic Messages API (`/v1/messages`), so Claude Code and Anthropic SDKs can use the gateway.

Requests stay in Anthropic's format end to end: Bedrock's InvokeModel takes the Messages body
natively, so tools, thinking, prompt-cache markers, and images reach the model unchanged. The
gateway adds what Claude Code's gateway guide asks for (ADR-0006):

- `anthropic-beta` forwarded verbatim (into the body as `anthropic_beta` for Bedrock),
- events relayed as they arrive, with `ping` events during silences (Bedrock's stream has none),
- request-shape errors passed through with their wording, so the client's capability retries work.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import audit
from .config import ModelSpec, TeamSpec
from .errors import GatewayError
from .metering import Meter, cost_usd
from .providers import Provider, ProviderError
from .routing import Route, Router, audit_fields, first_event

PING_INTERVAL_SECONDS = 10.0
UPSTREAM_ERROR = "upstream provider error; see request_id in the gateway audit log"
# Claude Code's attribution headers, recorded so cost can be grouped by session and subagent.
ATTRIBUTION = {"x-claude-code-session-id": "session_id", "x-claude-code-agent-id": "agent_id",
               "x-claude-code-request-class": "request_class"}

Admit = Callable[[TeamSpec, Any, dict[str, Any]], Awaitable[ModelSpec]]
Plan = Callable[..., Awaitable[Route]]
Screen = Callable[[TeamSpec, dict[str, Any], dict[str, Any], tuple[str, ...]], None]
# The parts of a Messages request that are sent to the model as text (ADR-0010).
SCREENED_FIELDS = ("system", "messages")


def _sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"


async def _with_pings(events: AsyncIterator[dict[str, Any]],
                      interval: float) -> AsyncIterator[dict[str, Any]]:
    """Relay events as they arrive, inserting a ping whenever `interval` passes with none."""
    iterator = events.__aiter__()
    pending = asyncio.ensure_future(iterator.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield {"type": "ping"}
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            yield event
            pending = asyncio.ensure_future(iterator.__anext__())
    finally:
        pending.cancel()


class Usage:
    """Token usage of one response, from a full message or accumulated from stream events."""

    def __init__(self) -> None:
        self.input = self.output = self.cache_read = self.cache_write = 0

    def add(self, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        # Streaming reports input on message_start and cumulative output on message_delta.
        self.input = usage.get("input_tokens") or self.input
        self.output = usage.get("output_tokens") or self.output
        self.cache_read = usage.get("cache_read_input_tokens") or self.cache_read
        self.cache_write = usage.get("cache_creation_input_tokens") or self.cache_write

    def fields(self, model: ModelSpec) -> dict[str, Any]:
        return {"prompt_tokens": self.input, "completion_tokens": self.output,
                "cache_read_tokens": self.cache_read, "cache_write_tokens": self.cache_write,
                "cost_usd": cost_usd(model, self.input, self.output, self.cache_read,
                                     self.cache_write)}


def build_messages_router(
    models: dict[str, ModelSpec],
    providers: dict[str, Provider],
    current_team: Callable[..., TeamSpec],
    admit: Admit,
    meter: Meter,
    plan: Plan,
    routing: Router,
    screen: Screen,
) -> APIRouter:
    router = APIRouter()

    async def read_body(request: Request) -> dict[str, Any]:
        try:
            body = json.loads(await request.body())
        except ValueError as exc:
            raise GatewayError(400, "request body is not valid JSON",
                               "invalid_request_error") from exc
        if not isinstance(body, dict) or not isinstance(body.get("model"), str):
            raise GatewayError(400, "request needs a model", "invalid_request_error")
        return body

    def betas(request: Request) -> list[str]:
        values = request.headers.get("anthropic-beta", "").split(",")
        return [b.strip() for b in values if b.strip()]

    def messages_provider(name: str, base: dict[str, Any]) -> Any:
        provider = providers[name]
        if not hasattr(provider, "messages"):
            audit.record(**base, status="denied", http_status=400, reason="unsupported_api")
            raise GatewayError(400, f"model {name!r} does not support the Messages API",
                               "invalid_request_error")
        return provider

    def upstream_failed(exc: ProviderError, base: dict[str, Any], latency_ms: int,
                        route: Route) -> GatewayError:
        # Upstream 4xx wording reaches the client (scrubbed of ARNs and account IDs) because
        # Claude Code retries by matching it; anything else stays generic (ADR-0005, ADR-0006).
        status = "error" if exc.status >= 500 else "rejected"
        audit.record(**base, status=status, http_status=exc.status, latency_ms=latency_ms,
                     reason=str(exc)[:200], **audit_fields(route, base["model"], None))
        headers = {"X-Request-Id": base["request_id"]}
        if exc.retry_after:
            headers["Retry-After"] = str(exc.retry_after)
        kind = {400: "invalid_request_error", 429: "rate_limit_error"}.get(exc.status, "api_error")
        return GatewayError(exc.status, exc.client_message or UPSTREAM_ERROR, kind, headers)

    # Claude Code's connection-warming probe.
    @router.head("/api/hello", include_in_schema=False)
    async def hello() -> Response:
        return Response(status_code=200)

    @router.post("/v1/messages", response_model=None)
    async def messages(request: Request,
                       team: TeamSpec = Depends(current_team)) -> Response:
        body = await read_body(request)
        name, stream = body["model"], bool(body.get("stream"))
        base: dict[str, Any] = {
            "request_id": f"msgreq-{uuid.uuid4().hex}", "api": "messages", "team": team.name,
            "model": name, "stream": stream,
            **{field: request.headers[h] for h, field in ATTRIBUTION.items()
               if h in request.headers},
        }
        model = await admit(team, name, base)
        user = audit.current_user()
        messages_provider(name, base)
        screen(team, base, body, SCREENED_FIELDS)
        # Fallbacks must speak the Messages API too (ADR-0009).
        route = await plan(team, model, lambda m: hasattr(providers[m.name], "messages"))
        started = time.perf_counter()

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        def headers(served: ModelSpec) -> dict[str, str]:
            return {"X-Request-Id": base["request_id"], "X-AIGW-Served-Model": served.name}

        if not stream:
            try:
                result, served = await routing.call(
                    route, lambda m: providers[m.name].messages(body, betas(request)))
            except ProviderError as exc:
                raise upstream_failed(exc, base, elapsed_ms(), route) from exc
            base["provider"] = providers[served.name].kind
            usage = Usage()
            usage.add(result.get("usage"))
            fields = usage.fields(served)  # priced as the model that answered
            audit.record(**base, status="ok", http_status=200, latency_ms=elapsed_ms(), **fields,
                         **audit_fields(route, name, served))
            await meter.charge(team, fields["cost_usd"], user)
            result["model"] = served.name
            return JSONResponse(result, headers=headers(served))

        # Wait for the first event before answering, so an upfront rejection (such as a beta the
        # upstream refuses) arrives as an HTTP error the client can retry, not inside a 200 stream,
        # and a model that fails to start can fall back.
        try:
            (first, upstream), served = await routing.call(
                route, lambda m: first_event(providers[m.name].messages_stream(body,
                                                                              betas(request))))
        except ProviderError as exc:
            raise upstream_failed(exc, base, elapsed_ms(), route) from exc
        base["provider"] = providers[served.name].kind

        async def relay() -> AsyncIterator[str]:
            async def events() -> AsyncIterator[dict[str, Any]]:
                if first is not None:
                    yield first
                async for event in upstream:
                    yield event

            usage, status, reason, chunks = Usage(), "ok", None, 0
            try:
                async for event in _with_pings(events(), PING_INTERVAL_SECONDS):
                    if event["type"] == "message_start":
                        event["message"]["model"] = served.name
                        usage.add(event["message"].get("usage"))
                    elif event["type"] == "message_delta":
                        usage.add(event.get("usage"))
                    chunks += event["type"] != "ping"
                    yield _sse(event)
            except ProviderError as exc:
                status, reason = "error", str(exc)[:200]
                kind = "overloaded_error" if exc.status in (429, 503) else "api_error"
                yield _sse({"type": "error", "error": {
                    "type": kind, "message": exc.client_message or UPSTREAM_ERROR}})
            finally:
                fields = usage.fields(served)
                extra = {"reason": reason} if reason else {}
                audit.record(**base, status=status, http_status=200, latency_ms=elapsed_ms(),
                             chunks=chunks, **fields, **audit_fields(route, name, served),
                             **extra)
                await meter.charge(team, fields["cost_usd"], user)

        return StreamingResponse(relay(), media_type="text/event-stream",
                                 headers={**headers(served), "Cache-Control": "no-cache"})

    @router.post("/v1/messages/count_tokens", response_model=None)
    async def count_tokens(request: Request,
                           team: TeamSpec = Depends(current_team)) -> Response:
        body = await read_body(request)
        name = body["model"]
        base = {"event": "count_tokens", "team": team.name, "model": name}
        if name not in models or not team.allows(name):
            audit.record(**base, status="denied", http_status=404, reason="unknown_model")
            raise GatewayError(404, f"model {name!r} not found", "invalid_request_error")
        screen(team, base, body, SCREENED_FIELDS)  # counting also sends the prompt upstream
        provider = providers[name]
        if not hasattr(provider, "count_tokens"):
            # Claude Code falls back to a local estimate when this endpoint is missing.
            raise GatewayError(404, "token counting is not available for this model",
                               "invalid_request_error")
        try:
            tokens = await provider.count_tokens(body, betas(request))
        except ProviderError as exc:
            status = "error" if exc.status >= 500 else "rejected"
            audit.record(**base, status=status, http_status=exc.status, reason=str(exc)[:200])
            kind = "invalid_request_error" if exc.status == 400 else "api_error"
            raise GatewayError(exc.status, exc.client_message or UPSTREAM_ERROR, kind) from exc
        audit.record(**base, status="ok", http_status=200, prompt_tokens=tokens)
        return JSONResponse({"input_tokens": tokens})

    return router
