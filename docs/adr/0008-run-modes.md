# ADR-0008: Development and production run modes

- **Status:** Accepted; the EKS consequence below is superseded by ADR-0013
- **Date:** 2026-11-02
- **Follows:** ADR-0005, ADR-0007

## Context

The same gateway image runs on a laptop with the mock model, in a full local stack, and on EKS. Only the
policy file differs. That is the point, but it means a policy written for a laptop (public demo keys,
in-memory limits, dev Vault over HTTP with a token) can be copied into a shared environment, and nothing
stops it from starting. Each of those settings is a documented gap in the threat model when it leaves
the laptop.

People who find the repo also need an obvious way in. Until now the README listed separate recipes
(Vault, kind, MCP, observability, EKS), and a newcomer had to work out the order.

## Decision

1. **`mode: development | production` in the policy**, default `development`.
2. **Production refuses to start** when the policy has any of:
   - a team using the public demo keys,
   - `store: memory` (limits and budgets per process), or Redis without TLS (`redis://`),
   - Vault over `http://`, or Vault token auth (a long-lived token, the static secret ADR-0001 removes),
   - an OIDC issuer over `http://`,
   - an upstream key read from an environment variable (`api_key_env`) instead of Vault.
   The policy fails validation with every problem listed, so `helm upgrade` ends in a crash loop with
   the reason in the pod log, not in a running but unsafe gateway.
3. **Development starts anyway and prints the same list** at startup, the way `vault server -dev` says
   it is insecure.
4. **`python -m aigw.check [--production] policy.yaml`** runs the same checks without starting the
   service, so CI can gate the production policy.
5. **Three documented levels**, one command each:
   - **Level 1:** `make quickstart` (`compose.yaml`): gateway, mock model, demo MCP server. Docker only.
   - **Level 2:** `make local` (`compose.full.yaml`): adds Keycloak sign-in (ADR-0007), Vault, Redis,
     Bedrock models, Prometheus, Grafana, Jaeger. Development mode, every production component.
   - **Level 3:** Terraform and `scripts/eks-deploy.sh` (EKS), with
     `policies/gateway.production.example.yaml` as the production-mode reference policy.

## Consequences

- A laptop policy cannot quietly become a production policy; switching the mode is an explicit change
  that review can see.
- The checks are a floor, not a certification. They cannot tell a real key from a test one, or check
  NetworkPolicy, IAM scoping, or the identity provider's own settings.
- The guided EKS demo (`scripts/demo-eks.sh`) keeps the demo keys so it runs out of the box, so it stays in
  development mode. A real deployment renders the production policy instead.
- *Update (ADR-0013):* the EKS deploy now runs in production mode with generated keys, and the guided
  demo reads them from `~/.aigw/keys-<cluster>.json`.
