# ADR-0001: Short-lived AWS credentials from Vault for upstream providers

- **Status:** Accepted
- **Date:** 2026-11-01
- **Phase:** 2 (Zero static secrets)

## Context

In Phase 1 the gateway calls Amazon Bedrock with whatever the default AWS credential chain finds.
On a laptop that is an IAM user access key: long-lived, broadly scoped, and copied to every machine
that runs the gateway. Upstream API keys for `openai_compat` providers come from environment variables,
which leak through process listings, crash dumps, and container specs.

The project goal is that no gateway replica holds a credential that outlives a few minutes.

## Decision

1. **Vault is the only source of upstream credentials.** The gateway asks Vault; it never reads cloud
   keys from its environment in the `vault` credential mode.
2. **Bedrock uses Vault's AWS secrets engine with `assumed_role`.** Vault calls `sts:AssumeRole` on a
   dedicated role (`aigw-bedrock`) whose policy allows only `bedrock:InvokeModel` and
   `bedrock:InvokeModelWithResponseStream`. Credentials are issued with a short TTL (15 minutes) and the
   gateway refreshes them before expiry.
3. **Upstream API keys live in Vault KV v2** and are read by path, not from env vars.
4. **Gateway → Vault authentication:**
   - In Kubernetes: Vault Kubernetes auth. The pod presents its service-account token; Vault maps the
     service account to a policy. No secret is shipped with the pod.
   - Locally: a Vault token from the environment (dev only).
5. **Vault → AWS authentication:**
   - On EKS: Vault runs with IRSA / EKS Pod Identity, so its own AWS credentials are short-lived too.
   - Locally: a dedicated IAM user limited to `sts:AssumeRole` on `aigw-bedrock`, stored encrypted in
     Vault and rotatable with `aws/config/rotate-root`.
6. **Client:** plain `httpx` against the Vault HTTP API rather than `hvac`, to avoid a dependency and
   keep requests easy to fake in tests.
7. **Backwards compatible:** a model opts in with `vault_path` (and the policy file then needs a
   top-level `vault:` section). Models without it keep Phase 1 behaviour, so the mock-only
   quickstart needs no Vault.

## Consequences

- A stolen gateway credential is useful for at most one TTL and only for invoking Bedrock.
- Every credential issuance is visible in Vault's audit log, alongside the gateway's own audit log.
- Vault becomes a runtime dependency: if it is unreachable when credentials expire, Bedrock calls fail
  with `502 upstream_error`. Mitigations (HA Vault, refresh well before expiry, alerting) belong to
  Phases 5 and 6.
- Local development still has one long-lived key (Vault's own); it is confined to Vault, scoped to a
  single `AssumeRole`, and absent on EKS.

## Alternatives considered

- **IRSA directly on the gateway pod.** Short-lived and simple, but AWS-only, gives no single place to
  manage non-AWS secrets, and no per-issuance audit outside CloudTrail.
- **External Secrets Operator syncing into Kubernetes Secrets.** Still materialises secrets at rest in
  etcd and as env vars or files; rotation is delayed by sync intervals.
- **`hvac` client library.** Mature, but adds a dependency for the four endpoints we call.
