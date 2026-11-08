# Running it in production

What an enterprise rollout looks like, what this repository already provides, and what is left.
[getting-started.md](getting-started.md) level 3 gets the platform running on EKS; this page is about
making it the company's front door for AI.

## The picture

```
 Developer laptops (managed by IT)                 Corporate network / VPN
 ┌───────────────────────────────┐
 │ Claude Code, Cursor, SDKs      │  HTTPS   ┌──────────────────────────────────────┐
 │  base URL from managed settings│ ───────▶ │ ai-gateway.example.com                │
 │  token from SSO (aigw-login)   │          │ internal ALB + ACM certificate + WAF  │
 └───────────────────────────────┘          └──────────────────┬───────────────────┘
                                                                ▼
        ┌─────────────── shared-services AWS account, EKS across 3 AZs ───────────────┐
        │ aigw pods (autoscaled) ──▶ Vault HA ──▶ STS: aigw-bedrock (15 min)            │
        │        │               ──▶ ElastiCache (TLS): limits, budgets, sessions       │
        │        │               ──▶ internal MCP servers (Jira, GitHub, wiki)          │
        │        ├── audit ──▶ S3 with Object Lock / SIEM                              │
        │        └── metrics, traces ──▶ Prometheus, Grafana, tracing backend           │
        └────────┬────────────────────────────────────────────────────────────────────┘
                 ▼  VPC endpoint (PrivateLink): traffic stays on the AWS network
          Amazon Bedrock (Claude), with quotas raised for the organization
```

## The five things that make it production

### 1. People sign in; nobody shares keys

The gateway verifies tokens from the company identity provider and maps IdP groups to teams
([ADR-0007](adr/0007-user-identity-oidc.md)). Joiners and leavers are handled in the directory, and the
audit log names a person.

- Register two apps in the IdP: the gateway API (its audience) and a public client for `aigw-login`
  with the device flow enabled.
- Put the issuer, audience, and group IDs in the policy:
  [`policies/gateway.production.example.yaml`](../policies/gateway.production.example.yaml) shows
  Entra ID.
- Keep access tokens short (15–60 minutes). Session length is the IdP's call.

### 2. IT configures the tools, not each developer

Claude Code reads a managed settings file that users cannot override. Push
[`examples/claude-code/managed-settings.json`](../examples/claude-code/managed-settings.json) with MDM
(Jamf, Intune) to `/Library/Application Support/ClaudeCode/managed-settings.json` on macOS
(`/etc/claude-code/managed-settings.json` on Linux), and install `aigw-login` as
`/usr/local/bin/aigw-login`. Developers then run `aigw-login login` once a day and `claude` as usual.

### 3. The gateway is the only way in

A gateway that can be bypassed controls nothing. Close the other paths:

- **Bedrock:** an SCP in AWS Organizations denies `bedrock:InvokeModel*` to every principal except the
  `aigw-bedrock` role. SCPs do not apply to the management account, so run Bedrock in a member account.
- **Public model APIs:** the egress proxy (Zscaler, Netskope, a NAT firewall) blocks `api.anthropic.com`
  and `api.openai.com` from company devices.
- **Cursor:** its custom model URL is called from Cursor's servers, so it needs a public endpoint (WAF,
  SSO tokens only). Many companies allow Cursor's MCP features through the gateway and use Cursor's own
  enterprise plan for models.

### 4. Nothing has a single copy

| Component | Demo setup (level 3) | Production |
|---|---|---|
| Gateway | 2 replicas, Spot | 3+ replicas across AZs, HPA, PodDisruptionBudget, on-demand base capacity |
| Vault | 1 node, file storage, KMS auto-unseal | 3-node Raft (or HCP Vault), audit device enabled |
| Redis | ElastiCache, single node | ElastiCache multi-AZ with IAM auth |
| Edge | port-forward | Internal ALB, ACM certificate on a real domain, WAF |
| Bedrock | default quotas | quotas raised, cross-region inference profiles, VPC endpoint |

### 5. Changes are reviewed; evidence is kept

- The policy is code: changes go through pull requests, CI runs
  `python -m aigw.check --production`, and Argo CD or Flux deploys it. Onboarding a team is one PR.
- `mode: production` refuses laptop-only settings at startup ([ADR-0008](adr/0008-run-modes.md)).
- Audit lines go to append-only storage (S3 Object Lock, CloudWatch Logs with retention) and the SIEM.
  `python -m aigw.report` turns them into per-team cost for chargeback.
- SLO burn-rate alerts ([`observability/prometheus/rules.yaml`](../observability/prometheus/rules.yaml))
  page the on-call rotation.

## What is built, what is left

| Area | Built and verified | Left for a real rollout |
|---|---|---|
| One endpoint for OpenAI and Anthropic APIs, MCP | ✅ | |
| Short-lived AWS credentials (Vault, STS, IRSA, KMS unseal) | ✅ | Vault HA (Raft x3) |
| Per-team models, tools, rate limits, budgets, cost | ✅ | |
| Per-person rate limits and budgets (ADR-0011) | ✅ | |
| Model fallback with circuit breaker (ADR-0009) | ✅ | Choose fallbacks within approved regions |
| SSO sign-in (any OIDC IdP), per-user audit | ✅ (Keycloak) | Register apps in your IdP |
| Production-mode guardrails, CI policy gate | ✅ | |
| Audit, metrics, traces, SLOs | ✅ | Ship audit to S3 Object Lock / SIEM |
| TLS to Vault and Redis, NetworkPolicy, threat model | ✅ | ALB + ACM + WAF at the edge |
| Managed Claude Code settings | Example file | MDM rollout |
| Enforcement (SCP, egress blocking) | | Organization-level change |
| MCP calls as the user (on-behalf-of) | | Token exchange per MCP server |
| Content controls: secret and PII detection, redact or block (ADR-0010) | ✅ | Tune detectors; prompt-injection filtering is still open |
| Disaster recovery | Rebuild from Terraform | Second region, tested restore |

## Rollout order

1. **Pilot:** one team, SSO tokens, internal ALB, production-mode policy.
2. **Enforce:** managed settings on that team's laptops; SCP on Bedrock.
3. **Expand:** onboard teams by policy PR; turn on chargeback from the cost report.
