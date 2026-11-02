# Getting started

The gateway runs the same image everywhere; only the policy file changes. Pick the level that matches
what you want to see. Each one adds to the one before it.

| Level | Command | You need | Time |
|---|---|---|---|
| [1. Try it](#level-1-try-it) | `make quickstart` | Docker | 2 minutes |
| [2. Full local stack](#level-2-full-local-stack) | `make local` | Docker; AWS for real models | 10 minutes |
| [3. Production on EKS](#level-3-production-on-eks) | Terraform + `scripts/eks-deploy.sh` | An AWS account (~$0.18/hour) | 30 minutes |

All commands run from the repository root. If port 8080 is taken, prefix them with `AIGW_PORT=8081`.

## Level 1: try it

What runs: the gateway with [`policies/gateway.quickstart.yaml`](../policies/gateway.quickstart.yaml),
a mock model that echoes your message, and a demo MCP server ("jira") with three tools.

```bash
make quickstart
```

**Call the OpenAI-compatible API** with a team key:

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions -H "Authorization: Bearer dev-platform-key" \
  -H "Content-Type: application/json" \
  -d '{"model": "mock-echo", "messages": [{"role": "user", "content": "hello"}]}'
```

**See policy at work:**

```bash
# wrong key: 401
curl -s http://127.0.0.1:8080/v1/models -H "Authorization: Bearer guessed"
# the data team's view of the MCP tools: delete_issue is hidden and refused
pip install "mcp>=2"
AIGW_MCP_URL=http://127.0.0.1:8080/mcp/jira python examples/mcp-demo/client.py dev-data-key
```

**Point Claude Code at it.** The mock only echoes, but it shows the wiring: Claude Code talks to the
gateway with a gateway key, and has no Anthropic or AWS credentials.

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8080 ANTHROPIC_AUTH_TOKEN=dev-platform-key \
  ANTHROPIC_MODEL=mock-echo ANTHROPIC_DEFAULT_HAIKU_MODEL=mock-echo claude
```

**The audit log** is the gateway's stdout, one JSON line per request (metadata only, never prompts):

```bash
docker compose logs gateway --no-log-prefix | grep '^{'
```

The gateway prints a warning at startup listing what makes this setup laptop-only (public demo keys,
in-memory limits). That list is exactly what `mode: production` refuses ([ADR-0008](adr/0008-run-modes.md)).

## Level 2: full local stack

What runs, all in Docker:

| Component | Role | Address |
|---|---|---|
| Gateway | [`policies/gateway.local.yaml`](../policies/gateway.local.yaml) | http://127.0.0.1:8080 |
| Keycloak | Identity provider: users sign in, groups map to teams | http://localhost:8180 (admin / admin) |
| Vault | Issues 15-minute AWS credentials for Bedrock | http://127.0.0.1:8200 (token `root`) |
| Redis | Rate limits, budgets, MCP sessions shared across replicas | internal |
| Prometheus, Grafana, Jaeger | Metrics, SLO alerts, dashboard, traces | :9090, :3000, :16686 |

```bash
# Optional, once: the IAM role Vault hands out credentials for (Bedrock invoke only).
# Without it, everything except the claude-* models still works.
terraform -chdir=terraform/iam-bedrock init && terraform -chdir=terraform/iam-bedrock apply

make local        # starts the stack and configures Vault (re-run `make local-setup` if Vault restarts)
```

**Sign in like a developer at a company with SSO.** The demo realm has three users:
alice (group eng-platform → team platform), bob (eng-data → team data), carol (no team).
Passwords are `<name>-dev-password`.

```bash
scripts/aigw-login login     # opens the browser; sign in as alice
scripts/aigw-login whoami    # Signed in as alice, groups ['eng-platform'], token expires in 14 min
```

**Use Claude Code with your sign-in**, not a shared key. The settings file sets the gateway URL and makes
`aigw-login token` Claude Code's `apiKeyHelper`. Claude Code reruns it every 5 minutes and on any 401,
and it refreshes the token silently, so the 15-minute token never logs you out:

```bash
claude --settings examples/claude-code/settings.local.json
```

(On another port, copy the file and change `ANTHROPIC_BASE_URL`.)

Every audit line now carries `user: alice` next to `team: platform`. Try `aigw-login logout`, sign in as
bob, and ask for `claude-sonnet-4-5`: the data team only has Haiku, so the request is refused (403).
Carol signs in fine but gets 403 on every request, because none of her groups grants a team.

**Look around:** the Grafana dashboard "AI Gateway" (requests, latency, tokens, cost per team), Jaeger
traces for service `aigw` with GenAI attributes, and Vault's `aws/creds/bedrock` leases.

API keys keep working next to sign-in (`dev-platform-key`), for scripts and CI.

## Level 3: production on EKS

Terraform builds the platform; the deploy script installs Vault and the gateway and smoke-tests them.
No AWS access key exists anywhere: Vault gets AWS credentials through IRSA and auto-unseals with KMS, and
the gateway logs in to Vault with its Kubernetes service account.

```bash
terraform -chdir=terraform/iam-bedrock apply
echo 'admin_cidrs = ["'"$(curl -s https://checkip.amazonaws.com)"'/32"]' > terraform/eks/terraform.tfvars
terraform -chdir=terraform/eks init && terraform -chdir=terraform/eks apply        # ~15 minutes
terraform -chdir=terraform/iam-bedrock apply \
  -var "vault_role_arns=[\"$(terraform -chdir=terraform/eks output -raw vault_role_arn)\"]"
scripts/eks-deploy.sh     # image to ECR, Vault (init, config), gateway, smoke test
scripts/demo-eks.sh       # guided tour of what is running and why
terraform -chdir=terraform/eks destroy                                              # when done
```

The EKS deploy keeps the demo keys so the guided demo works out of the box, so it runs in development
mode. For a real rollout, start from
[`policies/gateway.production.example.yaml`](../policies/gateway.production.example.yaml): it signs
people in through your identity provider and passes `python -m aigw.check --production`. What else a
production rollout needs (DNS and TLS at the edge, SSO, managed Claude Code settings, enforcement) is in
[production.md](production.md).

## Clean up

```bash
make down                    # levels 1 and 2
scripts/aigw-login logout    # removes cached tokens from ~/.aigw/tokens
```
