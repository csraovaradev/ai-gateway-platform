# Threat model

- **Scope:** the aigw service (LLM and MCP gateway), its policy file, Vault, Redis, and the upstreams it
  calls (Amazon Bedrock, OpenAI-compatible servers, MCP servers).
- **Method:** STRIDE per trust boundary. Each threat lists its mitigation and where it is enforced, or
  marks it as a gap with the work that closes it.
- **Last reviewed:** 2026-11-01 (Phase 6).

## Assets

| Asset | Why it matters |
|---|---|
| Upstream credentials (AWS STS, provider API keys, MCP server tokens) | Spend money and reach data as the platform |
| Team API keys | Act as a team: spend its budget, call its tools |
| Prompts, completions, tool arguments and results | Customer and company data |
| Policy file | Decides who may use which model and tool |
| Audit log | Evidence of who did what; basis for cost attribution |
| Budget and rate-limit state (Redis) | Spend control |

## Trust boundaries

```
 clients ──(1)──▶ aigw ──(3)──▶ Bedrock / OpenAI-compatible / MCP servers
                   │  ╲
                  (2)  (4)
                   ▼     ▼
                 Vault  Redis            (5) operators: policy file, Helm values, Vault config
```

1. **Client → gateway:** untrusted callers on the network, holding (or guessing) team keys.
2. **Gateway → Vault:** credential issuance.
3. **Gateway → upstreams:** the gateway's credentials leave the cluster; upstream responses are untrusted.
4. **Gateway → Redis:** shared limits and spend.
5. **Operators:** trusted, but changes must be reviewable.

## Threats

### 1. Client → gateway

| STRIDE | Threat | Mitigation | Where |
|---|---|---|---|
| Spoofing | Guess or brute-force a team key | 256-bit random keys (`aigw.keys`); only SHA-256 hashes stored; constant-time compare over every entry | `auth.py`, `keys.py` |
| Spoofing | Reuse another team's MCP session ID | Sessions bound to the opening team; mismatch → 404 | `mcp.py`, ADR-0002 |
| Tampering | Header says one MCP tool, body calls another | Body is authoritative; `Mcp-Method`/`Mcp-Name` mismatch → `-32020` | `mcp.py` |
| Repudiation | Team denies having made a call | One audit line per request incl. denials and failed auth, with `trace_id` | `audit.py` |
| Info disclosure | Upstream error text (AWS ARNs and account ID, Vault paths) returned to clients | **Gap → fixed in Phase 6:** clients get a generic message and `request_id`; detail stays in the audit log. Exception (ADR-0006): on `/v1/messages`, Bedrock `ValidationException` wording passes through with ARNs and account IDs scrubbed, because Claude Code's retries depend on it | `main.py`, `mcp.py`, `messages.py` |
| Info disclosure | `/metrics` exposes team names and spend | **Gap → fixed in Phase 6:** NetworkPolicy admits only ingress controller and Prometheus; ingress routes only `/v1` and `/mcp` | Helm chart |
| DoS | Oversized request bodies exhaust memory | **Gap → fixed in Phase 6:** body size limit (default 1 MiB) → 413 | `main.py` |
| DoS | Request floods, budget burn | Per-team sliding-window rate limit and budgets, shared across replicas | `store.py`, `metering.py` |
| DoS | Unbounded metric labels from client strings | Labels only from policy values | `metrics.py`, ADR-0004 |
| Elevation | Call a model or tool outside the team's policy | Allowlists checked before forwarding; `tools/list` filtered; batches rejected | `main.py`, `mcp.py` |

### 2. Gateway → Vault

| STRIDE | Threat | Mitigation | Where |
|---|---|---|---|
| Spoofing | Another workload logs in as the gateway | Kubernetes auth bound to one service account and namespace; token audience `vault` | `vault-k8s-setup.sh`, Helm |
| Spoofing | Man-in-the-middle impersonates Vault | **Gap → fixed in Phase 6:** TLS with a pinned CA bundle, optional client certificate | `vault.py` |
| Info disclosure | Stolen gateway credentials | STS creds live 15 min and allow only `bedrock:InvokeModel*`; gateway token limited to the `aigw` policy | ADR-0001 |
| DoS | Vault unavailable | Cached leases keep working until 2/3 of TTL; afterwards only Vault-backed models fail (502), others keep serving | `vault.py`; failure tests |

### 3. Gateway → upstreams

| STRIDE | Threat | Mitigation | Where |
|---|---|---|---|
| Info disclosure | Client's gateway key forwarded to an MCP server | Never forwarded; upstream token comes from Vault | `mcp.py` |
| Info disclosure | Prompt or tool content in logs, metrics, or spans | Metadata only, enforced by tests | `audit.py`, `metrics.py`, tests |
| DoS | Slow or hung upstream ties up the gateway | **Gap → fixed in Phase 6:** explicit Bedrock connect/read timeouts and bounded retries (boto3 defaults allowed ~4 attempts × 60 s) | `bedrock.py`; failure tests |
| Tampering | Malicious tool results (prompt injection) steer the agent | **Out of scope:** the gateway does not inspect content. Mitigated by allowlisting only trusted MCP servers and least-privilege tools per team | policy |

### 4. Gateway → Redis

| STRIDE | Threat | Mitigation | Where |
|---|---|---|---|
| Tampering | Reset a team's spend or rate-limit counters | **Gap → fixed in Phase 6:** `rediss://` with CA pinning and client certificates; Redis reachable only from the gateway (NetworkPolicy) | `store.py`, Helm |
| DoS | Redis down | Fail closed with 503 rather than serve without limits | `main.py`, ADR-0003 |

### 5. Operators

| STRIDE | Threat | Mitigation | Where |
|---|---|---|---|
| Tampering | Unreviewed policy change grants access | Policy is code: reviewed in pull requests, validated in CI (every example policy is loaded by tests) | CI |
| Repudiation | Audit log altered after the fact | **Accepted for now:** stdout logs; in production ship them to append-only storage (CloudWatch Logs with a retention policy, or S3 Object Lock) | deployment |
| Info disclosure | Public demo keys used outside a laptop | Documented as local-only; production policies are generated with `aigw.keys` | README |

## Accepted risks

- **Budget overshoot** of at most the requests in flight when a team crosses its budget (ADR-0003).
- **No content inspection** (PII redaction, prompt-injection filtering). A future policy hook could add it.
- **API keys do not expire.** Rotation is a policy change today; short-lived keys or OIDC client
  identity are future work.
