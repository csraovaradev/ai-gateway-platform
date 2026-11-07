# Feature guides

Step-by-step recipes for each part of the gateway, run from source (`gateway/`, `make run`).
For the one-command setups (Docker only, full local stack, EKS) start with
[getting-started.md](getting-started.md).

## From source with the mock model

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
[`policies/gateway.example.yaml`](../policies/gateway.example.yaml) for local use only.
Generate real keys with `python -m aigw.keys <team>`; the gateway stores only their SHA-256 hashes.

Because the API is OpenAI-compatible, existing SDKs work by changing the base URL:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8080/v1", api_key="dev-platform-key")
print(client.chat.completions.create(
    model="mock-echo", messages=[{"role": "user", "content": "hi"}]
).choices[0].message.content)
```

For real models, `bedrock-claude` calls Amazon Bedrock with your AWS credentials (your account needs
Bedrock access to Claude Haiku 4.5 in `us-west-2`), and `local-llama` points at any OpenAI-compatible
server such as Ollama or vLLM.

## Upstream credentials from Vault

With [`policies/gateway.vault.example.yaml`](../policies/gateway.vault.example.yaml) the gateway holds no
cloud keys at all: it leases 15-minute STS credentials for Bedrock from Vault's AWS secrets engine and
reads upstream API keys from Vault KV ([ADR-0001](adr/0001-vault-dynamic-aws-credentials.md)).

```bash
# 1. IAM role the gateway's credentials are scoped to (Bedrock invoke only), plus Vault's IAM user
terraform -chdir=terraform/iam-bedrock init && terraform -chdir=terraform/iam-bedrock apply

# 2. Dev Vault (in-memory; leave it running in its own terminal)
vault server -dev -dev-root-token-id=root

# 3. Configure Vault and get a gateway token limited to the aigw policy
scripts/vault-dev-setup.sh          # prints: export VAULT_TOKEN=...

# 4. Run the gateway against Vault
cd gateway && make run CONFIG=../policies/gateway.vault.example.yaml
```

## On Kubernetes (kind) with no secrets in the pod

The [Helm chart](../helm/aigw) mounts no Kubernetes API token and no Vault token. The pod presents a
projected service-account token whose audience is only `vault`; Vault verifies it and returns a token
limited to the `aigw` policy.

```bash
kind create cluster --name aigw
docker build -t ai-gateway:dev gateway && kind load docker-image ai-gateway:dev --name aigw
helm install vault hashicorp/vault -n vault --create-namespace --kube-context kind-aigw \
  --set server.dev.enabled=true --set server.dev.devRootToken=root --set injector.enabled=false
scripts/vault-k8s-setup.sh
helm install aigw helm/aigw -n aigw --create-namespace --kube-context kind-aigw
kubectl --context kind-aigw -n aigw port-forward svc/aigw 8090:8080
```

Then call model `mock-via-vault` on `localhost:8090`. That model reads its upstream key from Vault,
so a 200 proves the pod logged in to Vault with only its service-account identity.

## Use it from Claude Code and Cursor

Developers keep working in their own tools; only the endpoint and key change. The gateway serves the
Anthropic Messages API (`/v1/messages`) for Claude Code and Anthropic SDKs, next to the OpenAI-compatible
API ([ADR-0006](adr/0006-anthropic-messages-api.md)). Requests go to Bedrock in Anthropic's own format,
so tools, extended thinking, and prompt caching work, and every turn lands in the audit log and cost report.

```bash
cd gateway && make run CONFIG=../policies/gateway.claude-code.example.yaml   # in another terminal

export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_AUTH_TOKEN=dev-platform-key          # a gateway key: no Anthropic or AWS key on the laptop
export ANTHROPIC_MODEL=claude-sonnet-4-5
export ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-4-5
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1       # Bedrock rejects some pre-release betas
claude
```

A short Claude Code task through the gateway cost $0.0953 by the gateway's own pricing, the same figure
Claude Code reported, with its 18K-token system prompt written to the prompt cache once and read after that.

**Cursor:**

- **Tools (MCP):** add the gateway's MCP endpoints to `~/.cursor/mcp.json` (see
  [`examples/cursor/mcp.json`](../examples/cursor/mcp.json)). Cursor then sees only the tools the team's
  `allowed_tools` permit, and every call is audited. This connects from the laptop, so a local gateway works.
- **Models:** Cursor can use a custom OpenAI base URL (Settings → Models) pointed at `<gateway>/v1` with a
  gateway key. Cursor sends those requests from its own servers, so the gateway must be reachable from the
  internet over HTTPS with real (not demo) keys, and the OpenAI-format path to Bedrock does not yet carry
  tool calls, so it suits chat rather than agent mode.

## Fallback when a model is throttled or down

Give a model `fallbacks` and the gateway tries them in order when it answers 429, 5xx, or times out
([ADR-0009](adr/0009-fallback-and-circuit-breaking.md)). A fallback is only used if the team may use
it, and never onto a priced model once the team's budget is spent. It is never silent:

```yaml
models:
  - name: claude-sonnet-4-5
    # ...
    fallbacks: [claude-haiku-4-5]
circuit_breaker: {failure_threshold: 3, cooldown_seconds: 30}   # the defaults
```

```bash
curl -si http://localhost:8080/v1/messages -H "x-api-key: dev-platform-key" ... | grep -i served
# X-AIGW-Served-Model: claude-haiku-4-5
# audit: {"model": "claude-sonnet-4-5", "served_model": "claude-haiku-4-5",
#         "fallback_attempts": [{"model": "claude-sonnet-4-5", "reason": "..."}], ...}
```

After `failure_threshold` consecutive failures a model is skipped for `cooldown_seconds`
(`"reason": "circuit_open"`), then one trial request goes through. `aigw_fallbacks_total` counts
fallbacks by team, requested, and served model.

## MCP tool calls

MCP servers listed under `mcp_servers` are reachable at `/mcp/{name}` with the same team keys
([ADR-0002](adr/0002-mcp-gateway.md)). Each team's `allowed_tools` (`server/tool` globs) decides
which tools it can call; `tools/list` only shows those tools. Every request writes an `event: "mcp"`
audit line with team, server, method, and tool, never arguments or results. Both the session-based
protocol versions and the stateless 2026-07-28 version work.

```bash
cd gateway && .venv/bin/pip install -e ".[mcp-demo]"
.venv/bin/python ../examples/mcp-demo/server.py &        # demo "jira" MCP server on :9000
make run &                                                # gateway on :8080

.venv/bin/python ../examples/mcp-demo/client.py dev-data-key
# visible tools: ['search', 'get_issue']
# get_issue: ok: Gateway returns 502 when Vault is sealed
# delete_issue: refused: tool 'delete_issue' is not allowed for team 'data'
```

## Budgets and cost attribution

Give a model a `price` (USD per million tokens) and every audit line carries `cost_usd`. Give a team a
`budget` and it gets `429 insufficient_quota` once its spend for the day or month reaches it; free
models stay usable. Streamed requests are metered too. With `store: {backend: redis}` rate limits,
budgets, and MCP sessions are shared by all replicas ([ADR-0003](adr/0003-shared-state-and-metering.md)).

```bash
python -m aigw.report audit.log               # or: kubectl logs deploy/aigw | python -m aigw.report
# data  $0.000364
#   model                  requests  denied  errors   in tokens  out tokens     cost USD
#   bedrock-claude                4       2       0          34          66     0.000364
```

## Metrics, traces, and SLOs

`/metrics` serves Prometheus metrics built from the same events as the audit log (requests, denials,
latency, tokens, cost), labelled only with values from the policy file. With
`OTEL_EXPORTER_OTLP_ENDPOINT` set, requests are traced over OTLP using the GenAI semantic
conventions, and each audit line carries its `trace_id`. SLOs (99.5% availability, 95% of
non-streaming requests under 10 s) and burn-rate alerts are in
[`observability/prometheus/rules.yaml`](../observability/prometheus/rules.yaml)
([ADR-0004](adr/0004-observability.md)).

```bash
docker compose -f observability/docker-compose.yaml up -d     # AIGW_PORT=8081 if 8080 is taken
cd gateway && OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 make run
# Grafana http://127.0.0.1:3000   Prometheus http://127.0.0.1:9090   Jaeger http://127.0.0.1:16686
```

## On Amazon EKS

[`terraform/eks`](../terraform/eks) builds a VPC, an EKS cluster with Graviton Spot nodes and NetworkPolicy
enforcement, ElastiCache (TLS), ECR, and a KMS key for Vault auto-unseal. Vault runs in the cluster
with TLS and gets its AWS credentials through IRSA, so no AWS access key exists anywhere: the gateway
proves its identity to Vault with its service account, and Vault proves its identity to AWS with its own.
Running cost is roughly $0.18/hour; `terraform destroy` removes it all.

```bash
terraform -chdir=terraform/iam-bedrock apply
echo 'admin_cidrs = ["'"$(curl -s https://checkip.amazonaws.com)"'/32"]' > terraform/eks/terraform.tfvars
terraform -chdir=terraform/eks init && terraform -chdir=terraform/eks apply        # ~15 minutes
terraform -chdir=terraform/iam-bedrock apply \
  -var "vault_role_arns=[\"$(terraform -chdir=terraform/eks output -raw vault_role_arn)\"]"
scripts/eks-deploy.sh     # image to ECR, Vault (init, config), gateway, smoke test
```

## Hardening

The [threat model](threat-model.md) walks every trust boundary with STRIDE and links each threat to
its mitigation. Phase 6 closed the gaps it found ([ADR-0005](adr/0005-transport-security-and-isolation.md)):

- Upstream errors no longer reach clients (they can contain ARNs, account IDs, or Vault paths); clients
  get a generic message and the `request_id`, the audit log keeps the detail.
- Request bodies over 1 MiB get 413, including chunked uploads.
- Bedrock calls have a 5 s connect timeout, a per-model read timeout, and at most 2 attempts.
- Vault and Redis connections can pin a CA and present a client certificate (`vault.tls`, `store.tls`).
- The Helm chart ships a NetworkPolicy (API clients and Prometheus in, DNS/Vault/Redis/HTTPS out) and an
  Ingress that routes only `/v1` and `/mcp`, so `/metrics` stays internal.

[`tests/test_failure_modes.py`](../gateway/tests/test_failure_modes.py) covers Vault down, hung upstreams
and MCP servers, leaky upstream errors, and oversized bodies.
