"""MCP gateway: proxies Streamable HTTP MCP servers with per-team tool allowlists and audit.

Authorisation is decided on the JSON-RPC body. The `Mcp-Method` / `Mcp-Name` headers that the
2026-07-28 protocol mirrors from the body must agree with it, so no downstream hop can route on a
value the gateway did not check (ADR-0002). Arguments and results are never logged.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio
import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry.trace import SpanKind

from . import audit
from .config import GatewayConfig, McpServerSpec, TeamSpec
from .errors import GatewayError
from .ratelimit import RateLimiter
from .store import Store
from .tracing import tracer
from .vault import LeasedSecret, VaultClient, VaultError

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
PERMISSION_DENIED = -32003
HEADER_MISMATCH = -32020

# Body param mirrored into the Mcp-Name header, per method.
NAME_PARAMS = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}
# Request headers forwarded upstream. The client's Authorization header never is.
FORWARD_HEADERS = frozenset({"accept", "content-type", "last-event-id", "mcp-method", "mcp-name",
                             "mcp-protocol-version", "mcp-session-id"})
FORWARD_PREFIX = "mcp-param-"
RESPONSE_HEADERS = frozenset({"content-type", "mcp-session-id"})
SESSION_TTL_SECONDS = 24 * 3600


def _rpc_error(rpc_id: Any, code: int, message: str, status: int = 200,
               headers: dict[str, str] | None = None) -> JSONResponse:
    body = {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}
    return JSONResponse(body, status_code=status, headers=headers)


def _is_sse(resp: httpx.Response) -> bool:
    return resp.headers.get("content-type", "").startswith("text/event-stream")


def _filter_tools(message: Any, allowed: Callable[[str], bool]) -> Any:
    tools = message.get("result", {}).get("tools") if isinstance(message, dict) else None
    if isinstance(tools, list):
        message["result"]["tools"] = [t for t in tools if allowed(str(t.get("name")))]
    return message


def _tool_error(message: Any, rpc_id: Any) -> bool | None:
    """True/False once the response to `rpc_id` is seen (error or result.isError), else None."""
    if not isinstance(message, dict) or message.get("id") != rpc_id:
        return None
    if "error" in message:
        return True
    if "result" in message:
        return bool(message["result"].get("isError"))
    return None


def build_mcp_router(
    config: GatewayConfig,
    current_team: Callable[..., TeamSpec],
    limiter: RateLimiter,
    store: Store,
    vault: VaultClient | None = None,
    http: httpx.AsyncClient | None = None,
) -> APIRouter:
    servers = {s.name: s for s in config.mcp_servers}
    tokens = {s.name: LeasedSecret(vault, s.vault_path)
              for s in config.mcp_servers if vault and s.vault_path}
    client = http or httpx.AsyncClient()
    router = APIRouter()

    def server_for(name: str, team: TeamSpec) -> McpServerSpec:
        if name not in servers:
            raise GatewayError(404, f"mcp server {name!r} not found", "invalid_request_error")
        if not team.allows_server(name):
            audit.record(event="mcp", team=team.name, server=name, status="denied",
                         http_status=403, reason="server_not_allowed")
            raise GatewayError(403, f"team not allowed to use mcp server {name!r}",
                               "permission_error")
        return servers[name]

    async def upstream_headers(request: Request, spec: McpServerSpec,
                               team: TeamSpec) -> dict[str, str]:
        headers = {k: v for k, v in request.headers.items()
                   if k in FORWARD_HEADERS or k.startswith(FORWARD_PREFIX)}
        if secret := tokens.get(spec.name):
            try:
                token = (await anyio.to_thread.run_sync(secret.get)).get("token")
            except VaultError as exc:
                # The Vault error names internal paths: audit it, return a generic message.
                audit.record(event="mcp", team=team.name, server=spec.name, status="error",
                             http_status=502, reason=str(exc)[:200])
                raise GatewayError(502, "mcp server credentials unavailable",
                                   "upstream_error") from exc
            if token:
                headers["authorization"] = f"Bearer {token}"
        return headers

    def owner(team: TeamSpec) -> str:
        # Signed-in users own their sessions; API-key callers share their team's (ADR-0007).
        user = audit.current_user()
        return team.name if user is None else f"{team.name}/{user}"

    async def session_ok(request: Request, team: TeamSpec) -> bool:
        """A handshake-protocol session may only be used by the team that opened it."""
        sid = request.headers.get("mcp-session-id")
        return sid is None or await store.get(f"mcp-session:{sid}") == owner(team)

    @router.post("/mcp/{server}", response_model=None)
    async def post(server: str, request: Request,
                   team: TeamSpec = Depends(current_team)) -> Response:
        spec = server_for(server, team)
        started = time.perf_counter()
        raw = await request.body()
        base: dict[str, Any] = {"event": "mcp", "team": team.name, "server": server}

        try:
            msg = json.loads(raw)
        except ValueError:
            audit.record(**base, status="denied", http_status=400, reason="parse_error")
            return _rpc_error(None, PARSE_ERROR, "invalid JSON", 400)
        if not isinstance(msg, dict):
            audit.record(**base, status="denied", http_status=400, reason="batch_not_supported")
            return _rpc_error(None, INVALID_REQUEST, "JSON-RPC batches are not supported", 400)

        method, rpc_id = msg.get("method"), msg.get("id")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        name = params.get(NAME_PARAMS[method]) if method in NAME_PARAMS else None
        base["method"] = method if isinstance(method, str) else "<response>"
        if method == "tools/call":
            base["tool"] = name

        def deny(reason: str, code: int, message: str, status: int = 200,
                 headers: dict[str, str] | None = None) -> JSONResponse:
            audit.record(**base, status="denied", http_status=status, reason=reason)
            return _rpc_error(rpc_id, code, message, status, headers)

        header_method = request.headers.get("mcp-method")
        header_name = request.headers.get("mcp-name")
        if (header_method is not None and header_method != method) or (
            header_name is not None and header_name != name
        ):
            return deny("header_mismatch", HEADER_MISMATCH,
                        "Mcp-Method/Mcp-Name headers do not match the request body", 400)
        if not await session_ok(request, team):
            return deny("unknown_session", INVALID_REQUEST, "session not found", 404)
        if method == "tools/call":
            if not isinstance(name, str):
                return deny("invalid_params", INVALID_PARAMS,
                            "tools/call requires a tool name", 400)
            if not team.allows_tool(server, name):
                return deny("tool_not_allowed", PERMISSION_DENIED,
                            f"tool {name!r} is not allowed for team {team.name!r}")
            allowed, retry_after = await limiter.check(team.name, team.requests_per_minute)
            if not allowed:
                return deny("rate_limited", PERMISSION_DENIED, "rate limit exceeded", 429,
                            {"Retry-After": str(retry_after)})

        def elapsed_ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        upstream = client.build_request("POST", spec.url, content=raw,
                                        headers=await upstream_headers(request, spec, team),
                                        timeout=spec.timeout_seconds)
        span_attrs = {"mcp.server": server, "mcp.method": base["method"], "aigw.team": team.name}
        if method == "tools/call":
            span_attrs["gen_ai.tool.name"] = name
        try:
            with tracer.start_as_current_span(f"mcp {base['method']}", kind=SpanKind.CLIENT,
                                              attributes=span_attrs):
                resp = await client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            audit.record(**base, status="error", http_status=502, latency_ms=elapsed_ms(),
                         reason=f"upstream unreachable: {exc.__class__.__name__}")
            return _rpc_error(rpc_id, INTERNAL_ERROR, "mcp server unreachable", 502)

        if method == "initialize" and (sid := resp.headers.get("mcp-session-id")):
            await store.set(f"mcp-session:{sid}", owner(team), SESSION_TTL_SECONDS)
        out_headers = {k: v for k, v in resp.headers.items() if k in RESPONSE_HEADERS}

        def finish(status_code: int, tool_error: bool | None = None) -> None:
            extra = {"tool_error": tool_error} if method == "tools/call" else {}
            audit.record(**base, status="ok" if status_code < 400 else "error",
                         http_status=status_code, latency_ms=elapsed_ms(), **extra)

        def allowed_tool(tool: str) -> bool:
            return team.allows_tool(server, tool)

        if not _is_sse(resp):
            body = await resp.aread()
            await resp.aclose()
            tool_error = None
            if body:
                try:
                    reply = json.loads(body)
                except ValueError:
                    reply = None
                if method == "tools/list" and reply is not None:
                    body = json.dumps(_filter_tools(reply, allowed_tool)).encode()
                tool_error = _tool_error(reply, rpc_id)
            finish(resp.status_code, tool_error)
            return Response(body, status_code=resp.status_code, headers=out_headers)

        async def relay() -> AsyncIterator[str]:
            tool_error = None
            try:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        try:
                            event = json.loads(line[5:])
                        except ValueError:
                            event = None
                        if method == "tools/list" and event is not None:
                            line = "data: " + json.dumps(_filter_tools(event, allowed_tool))
                        if (seen := _tool_error(event, rpc_id)) is not None:
                            tool_error = seen
                    yield line + "\n"
            finally:
                await resp.aclose()
                finish(resp.status_code, tool_error)

        return StreamingResponse(relay(), status_code=resp.status_code, headers=out_headers)

    @router.get("/mcp/{server}", response_model=None)
    async def listen(server: str, request: Request,
                     team: TeamSpec = Depends(current_team)) -> Response:
        """Server-to-client stream of a handshake-protocol session."""
        spec = server_for(server, team)
        base = {"event": "mcp", "team": team.name, "server": server, "method": "GET"}
        if not await session_ok(request, team):
            audit.record(**base, status="denied", http_status=404, reason="unknown_session")
            raise GatewayError(404, "session not found", "invalid_request_error")
        upstream = client.build_request("GET", spec.url, timeout=None,
                                        headers=await upstream_headers(request, spec, team))
        try:
            resp = await client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            raise GatewayError(502, "mcp server unreachable", "upstream_error") from exc
        audit.record(**base, status="ok" if resp.status_code < 400 else "error",
                     http_status=resp.status_code)

        async def relay() -> AsyncIterator[bytes]:
            try:
                async for part in resp.aiter_raw():
                    yield part
            finally:
                await resp.aclose()

        out_headers = {k: v for k, v in resp.headers.items() if k in RESPONSE_HEADERS}
        return StreamingResponse(relay(), status_code=resp.status_code, headers=out_headers)

    @router.delete("/mcp/{server}", response_model=None)
    async def close(server: str, request: Request,
                    team: TeamSpec = Depends(current_team)) -> Response:
        spec = server_for(server, team)
        base = {"event": "mcp", "team": team.name, "server": server, "method": "DELETE"}
        if "mcp-session-id" not in request.headers or not await session_ok(request, team):
            audit.record(**base, status="denied", http_status=404, reason="unknown_session")
            raise GatewayError(404, "session not found", "invalid_request_error")
        try:
            resp = await client.request("DELETE", spec.url, timeout=spec.timeout_seconds,
                                        headers=await upstream_headers(request, spec, team))
        except httpx.HTTPError as exc:
            raise GatewayError(502, "mcp server unreachable", "upstream_error") from exc
        await store.delete(f"mcp-session:{request.headers['mcp-session-id']}")
        audit.record(**base, status="ok" if resp.status_code < 400 else "error",
                     http_status=resp.status_code)
        return Response(status_code=resp.status_code)

    return router
