#!/usr/bin/env bash
# Configure Vault running in a local kind cluster for the gateway's Kubernetes auth (ADR-0001).
#
# Prereqs: kind cluster with Vault in dev mode, e.g.
#   helm install vault hashicorp/vault -n vault --create-namespace \
#     --set server.dev.enabled=true --set server.dev.devRootToken=root --set injector.enabled=false
#
# The gateway pod gets no Vault token: it presents its service-account JWT, Vault checks it with
# the Kubernetes TokenReview API, and returns a token limited to the `aigw` policy.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONTEXT="${KUBE_CONTEXT:-kind-aigw}"
NAMESPACE="${AIGW_NAMESPACE:-aigw}"
SERVICE_ACCOUNT="${AIGW_SERVICE_ACCOUNT:-aigw}"

v() {
  kubectl --context "$CONTEXT" -n vault exec -i vault-0 -- env VAULT_TOKEN="${VAULT_ROOT_TOKEN:-root}" vault "$@"
}

echo "==> Kubernetes auth"
auths="$(v auth list -format=json)"  # captured: pipefail + grep -q can SIGPIPE
grep -q '"kubernetes/"' <<<"$auths" || v auth enable kubernetes
# Inside the cluster Vault reviews tokens with its own service account (auth-delegator binding).
v write auth/kubernetes/config kubernetes_host=https://kubernetes.default.svc >/dev/null
v write auth/kubernetes/role/aigw \
  bound_service_account_names="$SERVICE_ACCOUNT" \
  bound_service_account_namespaces="$NAMESPACE" \
  audience=vault policies=aigw ttl=1h >/dev/null

echo "==> Gateway policy"
v policy write aigw - < "$ROOT/vault/policies/aigw.hcl" >/dev/null

echo "==> Upstream key for the self-test model (the gateway calling its own mock-echo)"
v kv put secret/aigw/upstream/self api_key=dev-platform-key >/dev/null

echo "Vault in $CONTEXT is ready for service account $NAMESPACE/$SERVICE_ACCOUNT."
