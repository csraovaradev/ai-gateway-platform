# ADR-0002: MCP gateway inside the aigw service

- **Status:** Accepted
- **Date:** 2026-11-01
- **Phase:** 3 (MCP gateway)

## Context

Agents call tools through MCP servers over Streamable HTTP: JSON-RPC 2.0 messages POSTed to one
endpoint, answered with JSON or an SSE stream. Two protocol generations are in use:

- **Handshake versions (2024-11-05 … 2025-11-25):** `initialize` returns an `Mcp-Session-Id` that the
  client sends on every later request; `GET` opens a server-to-client stream; `DELETE` ends the session.
- **2026-07-28:** stateless. Every POST is self-contained, and the client mirrors the JSON-RPC method
  and the tool/prompt/resource name into `Mcp-Method` / `Mcp-Name` headers so intermediaries can route
  without parsing the body.

Without a gateway, every team holds credentials for every MCP server it uses, there is no central
allowlist of tools, and no record of which team called which tool.

## Decision

1. **Same service, separate module.** The MCP gateway is `aigw/mcp.py`, mounted at `/mcp/{server}` in
   the existing app. It reuses team API keys, the policy file, the rate limiter, the Vault client, and
   the audit log. It can be split into its own deployment later if scaling needs diverge.
2. **Policy.** `mcp_servers` in the policy file lists upstream servers. Teams get `allowed_tools`:
   glob patterns over `server/tool` (`jira/get_*`, `docs/*`). A team with no pattern for a server
   cannot reach it at all (HTTP 403).
3. **Decide on the body; reject disagreeing headers.** Authorisation uses the JSON-RPC body. If
   `Mcp-Method` or `Mcp-Name` is present and differs from the body, the request is rejected with
   `HEADER_MISMATCH` (-32020) before anything is forwarded, so no downstream hop can route on a value
   the gateway did not check.
4. **Enforcement points.**
   - `tools/call` for a tool outside the allowlist → JSON-RPC error -32003 (`permission denied`), never
     forwarded.
   - `tools/list` responses are filtered, so agents only see tools they may call.
   - `tools/call` counts against the team's `requests_per_minute`.
   - JSON-RPC batches are rejected (removed from the protocol in 2025-06-18; would bypass per-call checks).
5. **Session binding (handshake versions).** The gateway records which team's `initialize` produced
   each `Mcp-Session-Id` and answers `404` (the spec's "session not found") when another team presents
   it. Per replica and in memory, like rate limits; moves to the shared store in Phase 4.
6. **Credential isolation.** The client's `Authorization` header is never forwarded. If the server
   needs a credential, it comes from Vault KV (`vault_path`, field `token`).
7. **Audit.** One `event: "mcp"` line per request: team, server, method, tool, status, HTTP status,
   latency, and for tool calls whether the tool reported an error. Never arguments or results.

## Consequences

- One place to grant, revoke, and review tool access per team, with the same keys as LLM access.
- Agents cannot discover or call tools outside their allowlist, even with a direct session ID.
- Prompts and resources are gated per server, not per item, for now.
- Session binding is per replica: behind several replicas without sticky routing, a handshake-version
  client may get `404` and must re-initialise, which the spec already requires clients to handle.

## Alternatives considered

- **Separate `mcp-gateway` service.** Cleaner isolation, but duplicates auth, policy, audit, and Vault
  wiring for no current benefit.
- **Route on `Mcp-Method` / `Mcp-Name` headers only.** Cheaper, but trusts client-supplied headers for
  an authorisation decision.
- **Hide denied tools by answering "unknown tool".** Avoids revealing names, but makes legitimate
  denials hard to debug; teams already know their own policy.
