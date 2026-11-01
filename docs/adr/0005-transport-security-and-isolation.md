# ADR-0005: Transport security and network isolation

- **Status:** Accepted
- **Date:** 2026-11-01
- **Phase:** 6 (Hardening); closes gaps from the [threat model](../threat-model.md)

## Context

The gateway talks to Vault (credentials), Redis (limits and spend), and upstreams, and serves an
internal-only `/metrics` endpoint. On EKS, Vault and Redis are likely managed services outside the
cluster (HCP Vault or Vault on its own nodes, ElastiCache), so a service mesh would not cover those hops.

## Decision

1. **Application-level TLS to Vault and Redis**, with a pinned CA bundle and an optional client
   certificate (mutual TLS): `vault.tls` and `store.tls` in the policy file, `rediss://` for Redis.
   This works the same for in-cluster and managed services, and does not depend on a mesh.
2. **NetworkPolicies on the gateway pod** (Helm, on by default): ingress only from the ingress
   controller's namespace (API traffic) and the monitoring namespace (Prometheus); egress only to DNS,
   Vault, Redis, and HTTPS. NetworkPolicy works at layers 3 and 4, so it cannot filter by URL path.
3. **`/metrics` stays off the public path** in two layers: the Ingress routes only `/v1` and `/mcp`, and
   the NetworkPolicy keeps everything except the ingress controller and Prometheus away from the pod.
4. **No service mesh for now.** Pod-to-pod traffic inside the cluster is only ingress controller →
   gateway and Prometheus → gateway. A mesh (Linkerd, Istio ambient) is the right step once more
   internal services call the gateway directly; the application-level TLS above stays useful either way.
5. **Bounded upstream waits.** Bedrock calls use a 5 s connect timeout, the model's `timeout_seconds`
   as read timeout, and at most 2 attempts in total; request bodies are capped at 1 MiB (413).
6. **Generic upstream errors.** Clients get a fixed message plus `request_id`; the detail (which can
   include ARNs, account IDs, or Vault paths) goes only to the audit log.

## Consequences

- A stolen network position inside the cluster cannot read or reset spend counters, or impersonate
  Vault, without a certificate from the pinned CA.
- Certificates must be issued and rotated; on EKS that is cert-manager (with a private CA such as
  AWS Private CA) mounting files the gateway already reads.
- Python 3.13+ verifies certificates in strict X.509 mode. Private CAs must carry proper extensions
  (`basicConstraints`, `keyUsage` with `keyCertSign`); older internal CAs that `curl` accepts can still
  be rejected here.
- The NetworkPolicy egress rule allows HTTPS to any address, because Bedrock and MCP endpoints have no
  fixed IPs; tighter egress needs an FQDN-aware policy (Cilium, or AWS VPC endpoints for Bedrock).
