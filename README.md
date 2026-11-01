# AI Gateway Platform

[![CI](https://github.com/csraovaradev/ai-gateway-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/csraovaradev/ai-gateway-platform/actions/workflows/ci.yml)

A self-hosted, Kubernetes-native gateway for AI workloads: one front door for **LLM calls** and **MCP tool calls**, with **no long-lived secrets**, per-team policy, and full audit.

> **Status:** 🚧 Under active development. See the [Roadmap](#roadmap) for what works today.

---

## The problem

When an organization adopts AI tools, every team ends up wiring its own path to models and tools:

- **Static API keys** for model providers and internal systems are copied into IDE configs, `.env` files, and CI variables.
- **No central visibility** into which team called which model or tool, what it cost, or what data moved.
- **MCP servers multiply** (Jira, Jenkins, Datadog, Vault, internal APIs), each with its own auth and no shared guardrails.
- **Regulated environments** need an audit trail that ad-hoc integrations can't produce.

## The approach

A single gateway, run as a platform service on EKS:

| Plane | What it does |
|---|---|
| **LLM gateway** | OpenAI-compatible endpoint that routes to Amazon Bedrock, hosted providers, or self-hosted models (vLLM). Handles streaming, fallback, and model allowlists. |
| **MCP gateway** | Fronts upstream MCP servers. Clients connect once; the gateway enforces per-team tool allowlists and logs every tool call. |
| **Credential broker** | HashiCorp Vault issues **short-lived, scoped credentials** per request. Gateway pods authenticate to Vault with their Kubernetes service account, so no provider keys live in the cluster. |
| **Policy & metering** | Per-team rate limits, token budgets, and cost attribution. |
| **Observability** | OpenTelemetry traces and metrics for every request (latency, tokens, cost, tool usage), plus a structured audit log. |

## Architecture

<p align="center">
  <img src="docs/images/architecture.svg" alt="AI Gateway Platform architecture" width="100%">
</p>

### Request flow (LLM call)

1. A client calls `POST /v1/chat/completions` with its team identity (OIDC token or gateway-issued key).
2. The gateway checks policy: is the team allowed this model, and is it within its rate limit and budget?
3. The router asks Vault for a short-lived credential for the target provider (for example, dynamic AWS credentials for Bedrock).
4. The request is forwarded and the response is streamed back.
5. Tokens, latency, cost, and outcome are emitted as OTel spans and metrics and written to the audit log.

## Repository layout

```
ai-gateway-platform/
├── gateway/          # Gateway service: API, auth, policy, LLM router
├── mcp-gateway/      # MCP proxy: session handling, tool allowlists, audit
├── terraform/        # EKS, networking, IAM, Vault config (IaC)
├── helm/             # Helm charts for gateway, MCP proxy, OTel collector
├── policies/         # Example team policies (models, tools, quotas)
├── docs/             # Architecture decisions, threat model, runbooks
└── examples/         # Client configs (Cursor, Claude Code, curl)
```

## Roadmap

- [ ] **Phase 0: Foundation.** Terraform for VPC and EKS, Vault integration, CI with lint, test, and image build.
- [ ] **Phase 1: LLM gateway.** OpenAI-compatible endpoint with streaming, routing to Bedrock plus one hosted provider, and team auth.
- [ ] **Phase 2: Zero static secrets.** Vault Kubernetes auth and dynamic AWS credentials for Bedrock, with provider keys leased per request.
- [ ] **Phase 3: MCP gateway.** Proxy for Streamable HTTP MCP servers, per-team tool allowlists, and a tool-call audit log.
- [ ] **Phase 4: Policy & metering.** Rate limits, token budgets, and per-team cost attribution.
- [ ] **Phase 5: Observability.** OTel traces and metrics, dashboards, and SLOs for latency and availability.
- [ ] **Phase 6: Hardening.** mTLS between components, NetworkPolicies, a threat model, and failure-mode tests.

## Design decisions

Key choices are recorded as ADRs in [`docs/adr/`](docs/adr/), including:

- Why a gateway instead of SDK-level controls
- Why Vault dynamic secrets instead of Kubernetes Secrets
- How MCP sessions are proxied and scoped per team

## Quick start

Run the gateway locally with the mock model (no cloud credentials needed):

```bash
cd gateway
make install          # creates .venv and installs dependencies
make test             # runs the test suite
make run              # starts the gateway on http://localhost:8080
```

In another terminal:

```bash
curl -s http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer dev-platform-key" \
  -H "Content-Type: application/json" \
  -d '{"model": "mock-echo", "messages": [{"role": "user", "content": "hello"}]}'
```

`dev-platform-key` and `dev-data-key` are public demo keys defined in
[`policies/gateway.example.yaml`](policies/gateway.example.yaml) for local use only.
Generate real keys with `python -m aigw.keys <team>`; the gateway stores only their SHA-256 hashes.

Because the API is OpenAI-compatible, existing SDKs work by changing the base URL:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8080/v1", api_key="dev-platform-key")
print(client.chat.completions.create(
    model="mock-echo", messages=[{"role": "user", "content": "hi"}]
).choices[0].message.content)
```

To use real models, edit the example policy: set `upstream_model` on `bedrock-claude`
(uses your AWS credentials) or point `local-llama` at any OpenAI-compatible server such as Ollama or vLLM.

## License

[Apache 2.0](LICENSE)
