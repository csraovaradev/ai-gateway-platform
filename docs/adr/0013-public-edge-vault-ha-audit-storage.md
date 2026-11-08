# ADR-0013: Public HTTPS edge, Vault HA, and tamper-resistant audit storage

- **Status:** Accepted
- **Date:** 2026-11-08
- **Follows:** ADR-0001, ADR-0005, ADR-0007, ADR-0008, ADR-0012

## Context

On EKS the gateway was reachable only from inside the cluster or through `kubectl port-forward`, so
nobody could use it from a laptop without cluster access. Three production gaps were also open in
`docs/production.md`: Vault ran as a single node, audit lines went only to container stdout where
anyone with cluster access could lose or alter them, and the EKS deploy used the public demo keys.

Exposing the gateway to the internet changes the threat model: anything public is scanned within
minutes, and a public endpoint that accepts the demo keys would spend the account's Bedrock budget
for anyone who read the README.

## Decision

1. **One ALB, two names, created from Kubernetes.**
   - The AWS Load Balancer Controller builds the ALB from Ingress objects; ExternalDNS points
     `api.<domain>` (the gateway) and `sso.<domain>` (Keycloak) at it.
   - Both run with IRSA roles; ExternalDNS may change records only in the one hosted zone and runs
     `upsert-only`, so it never deletes anything it did not create.
   - Terraform creates the ACM certificate (DNS-validated), the WAF, and the roles, only when
     `domain_name` is set. Without a domain everything stays internal, as before.
2. **TLS ends at the ALB** with `ELBSecurityPolicy-TLS13-1-2-2021-06` (TLS 1.2 and 1.3 only); HTTPS
   is the only listener.
3. **Only API paths are routed.**
   - On `api.`: `/v1` and `/mcp`. `/metrics` and `/healthz` stay internal (ADR-0005).
   - On `sso.`: `/realms/aigw` and `/resources`. Keycloak's admin console and master realm are not
     reachable from the internet at all.
4. **AWS WAF in front:**
   - A per-IP rate limit (2,000 requests per 5 minutes by default).
   - Amazon's IP reputation list, known-bad-inputs rules, and the common rule set.
   - The managed rules' body checks are switched to count-only, because request bodies here are
     prompts full of code, file paths, and security discussion that those rules would block. Prompt
     content is the gateway's job (ADR-0010).
5. **Production mode on EKS (ADR-0008).**
   - The deploy script generates team API keys once and keeps them in `~/.aigw/keys-<cluster>.json`
     (mode 600); only their hashes reach the cluster.
   - Keycloak runs without demo users and without the password grant. The deploy script creates
     one account for the operator with a random password, and the realm (which contains it) is a
     Kubernetes Secret.
   - The gateway's client secret for token exchange (ADR-0012) lives only in Vault and Keycloak.
6. **NetworkPolicies admit the ALB, not the VPC.** With pod IPs from the VPC, allowing the VPC range
   would admit every pod. The two public subnets where the ALB lives form one `/23`, which is the only
   range allowed in.
7. **Vault: three replicas with integrated Raft storage,** one per node (three Spot nodes), spread
   across zones where possible. Replicas join through `retry_join` over TLS and each unseals itself
   with KMS, so only `vault-0` is ever initialised. A PodDisruptionBudget keeps a quorum during node
   drains.
8. **Audit lines to S3 with Object Lock.**
   - Fluent Bit reads only the gateway's containers and only audit JSON lines, and writes gzipped
     batches to `s3://<bucket>/audit/`.
   - Its IRSA role may only `PutObject` under that prefix: it cannot read, list, or delete.
   - The bucket has versioning, Object Lock in GOVERNANCE mode (30 days by default), TLS-only access,
     no public access, and a one-year lifecycle.
9. **Teardown.** The ALB and DNS records are created by controllers, not Terraform.
   `scripts/eks-teardown.sh` deletes the Ingresses, waits for the ALB to go, and removes only the
   records ExternalDNS marked as its own, before `terraform destroy`.

## Alternatives considered

- **COMPLIANCE-mode Object Lock:** nobody, including the root user, can delete records before the
  retention ends. Right for a real audit trail, but it would make the demo bucket impossible to
  remove for 30 days. GOVERNANCE needs an explicit bypass permission to delete, which the gateway and
  Fluent Bit do not have.
- **Terraform-managed DNS records for the ALB:** the ALB's name is only known after the controller
  creates it, which would need a second apply. ExternalDNS is the standard answer and keeps DNS in
  step with Ingresses.
- **A managed Keycloak database (RDS):** more cost and moving parts for a demo. Keycloak uses its
  file database and re-imports the realm from the Secret on every start, so a restart loses only
  sessions, not configuration or the operator's account.

## Consequences

- The gateway can be used from any laptop over HTTPS with SSO, including Claude Code through
  `apiKeyHelper` with `--issuer https://sso.<domain>/realms/aigw`.
- Running cost rises by roughly $0.05/hour (a third node, the ALB, WAF), and the domain costs about
  $16 a year whether or not the cluster runs.
- Audit records survive the loss of the cluster and cannot be altered by the gateway or anyone with
  only cluster access.
- Keycloak sessions do not survive a Keycloak restart; people sign in again.
- WAF body rules run in count mode, so request-body attacks rely on the gateway's own validation and
  content guardrails.
