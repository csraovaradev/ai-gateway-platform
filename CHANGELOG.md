# Changelog

## Unreleased

- **MCP on behalf of people** (ADR-0012): with `on_behalf_of`, a signed-in person's MCP calls carry a
  token the identity provider issued to that server for that person (OAuth 2.0 token exchange,
  RFC 8693), cached until shortly before expiry. The person's own token never leaves the gateway;
  API-key callers keep the server's service token; a refused exchange fails closed.
- **Per-person limits** (ADR-0011): `per_user: {requests_per_minute, budget}` on a team gives each
  signed-in person their own rate limit (model APIs and MCP tool calls) and budget, on top of the
  team's. API-key callers keep team limits only. The cost report adds spend per person and
  attributes cost to the model that answered.
- **Content guardrails** (ADR-0010): prompts are scanned for secrets (cloud keys, API tokens,
  private keys) and personal data (emails, card numbers, US SSNs) before they reach a model provider,
  on every model endpoint including token counting. Per team and category: `off`, `audit`,
  `redact` (default for secrets), or `block`; personal data is audited by default. Policies can add
  their own detectors. Audit lines and `aigw_content_findings_total` record the kind and count, never
  the value. Signed thinking blocks are never modified.
- **Model fallback and circuit breaking** (ADR-0009): a model that is throttled, down, or timing out
  falls back to the policy's `fallbacks`, only to models the team may use and never onto a priced
  model once the team's budget is spent. Responses carry `X-AIGW-Served-Model`, the audit log
  records `served_model` and `fallback_attempts`, and cost and metrics follow the model that
  answered. A per-model circuit breaker skips a failing model for a cooldown. Streamed chat
  responses now wait for the first chunk, so a model that fails to start returns an HTTP error or
  falls back instead of a 200 with an error inside.

## v1.0.0 (2026-11-02)

First release: a self-hosted gateway that puts guardrails around enterprise use of LLMs and MCP tools.
Verified on a laptop (Docker, kind) and on Amazon EKS against Claude models on Amazon Bedrock.

### Guardrails

- **Identity:** single sign-on through any OIDC identity provider (Entra ID, Okta, Keycloak). Groups map
  to teams, and every audit line names the person. `aigw-login` gives Claude Code fresh 15-minute tokens
  through `apiKeyHelper`, so nobody is logged out mid-session. API keys (stored only as SHA-256 hashes)
  remain for services and CI.
- **Model and tool policy:** per-team model allowlists; per-team MCP tool allowlists (`server/tool`
  globs). Tools a team may not use are hidden from `tools/list` and refused without reaching the server.
- **No standing credentials:** Vault issues 15-minute AWS credentials for Bedrock and holds upstream API
  keys; on EKS the gateway logs in with its service account and Vault uses IRSA, so no AWS access key
  exists anywhere. Vault auto-unseals with KMS.
- **Spend control:** per-team rate limits and daily or monthly budgets, shared across replicas through
  Redis; cost per request, including prompt-cache reads and writes; a cost report from the audit log.
- **Audit and observability:** one JSON audit line per request, including denials and failed sign-ins,
  never prompt or response content; Prometheus metrics, OpenTelemetry traces with GenAI attributes, SLO
  burn-rate alerts, and a Grafana dashboard.
- **Production-mode checks:** `mode: production` refuses to start with demo keys, in-memory limits, or
  plain-HTTP Vault, Redis, or identity provider; `python -m aigw.check --production` gates policies in CI.

### Interfaces

- OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`) with streaming.
- Anthropic Messages API (`/v1/messages`, `count_tokens`) for Claude Code and Anthropic SDKs, with tools,
  extended thinking, and prompt caching passed through to Bedrock.
- MCP Streamable HTTP proxy at `/mcp/{server}`, both session-based and stateless protocol versions.
- Providers: Amazon Bedrock, any OpenAI-compatible server (vLLM, Ollama, hosted APIs), and a mock.

### Running it

- `make quickstart`: Docker only, mock model, demo MCP server.
- `make local`: adds Keycloak sign-in, Vault, Redis, Bedrock, Prometheus, Grafana, Jaeger.
- Terraform and `scripts/eks-deploy.sh`: VPC, EKS (Graviton Spot), ElastiCache with TLS, Vault with TLS,
  KMS, and IRSA, NetworkPolicy, Helm chart.

### Known limits

See [docs/production.md](docs/production.md): Vault runs as a single node, the edge (ALB, certificate,
WAF) and organization-level enforcement (SCPs, egress blocking) are not included, MCP calls use a
service token rather than the user's identity, and the gateway does not inspect content (PII, prompt
injection).
