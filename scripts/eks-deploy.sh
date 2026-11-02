#!/usr/bin/env bash
# Deploy Vault and the gateway to the EKS cluster from terraform/eks, then smoke-test it.
#
# Prereqs: `terraform apply` in terraform/iam-bedrock and terraform/eks, then re-apply iam-bedrock with
#   -var 'vault_role_arns=["<terraform -chdir=terraform/eks output -raw vault_role_arn>"]'
# Tools: aws, kubectl, helm, docker, openssl, vault (CLI not required on the cluster side).
#
# Safe to re-run: every step checks what already exists. Vault's root token and recovery key are
# written once to ~/.aigw/vault-init-<cluster>.json (mode 600); keep that file safe or revoke the
# root token after setup (see README).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tf() { terraform -chdir="$ROOT/terraform/eks" output -raw "$1"; }

REGION="$(tf region)"
CLUSTER="$(tf cluster_name)"
ECR="$(tf ecr_repository_url)"
VAULT_ROLE="$(tf vault_role_arn)"
KMS_KEY="$(tf vault_kms_key_id)"
REDIS_URL="$(tf redis_url)"
VPC_CIDR="$(tf vpc_cidr)"
BEDROCK_ROLE="$(tf bedrock_role_arn)"
TAG="$(git -C "$ROOT" rev-parse --short HEAD)"
CONTEXT="aigw-eks"
INIT_FILE="$HOME/.aigw/vault-init-$CLUSTER.json"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

k() { kubectl --context "$CONTEXT" "$@"; }
v() { k -n vault exec -i vault-0 -- env VAULT_TOKEN="$ROOT_TOKEN" vault "$@"; }
render() {
  sed -e "s|__VAULT_ROLE_ARN__|$VAULT_ROLE|g" -e "s|__KMS_KEY_ID__|$KMS_KEY|g" \
      -e "s|__REGION__|$REGION|g" -e "s|__IMAGE_REPO__|$ECR|g" -e "s|__IMAGE_TAG__|$TAG|g" \
      -e "s|__REDIS_URL__|$REDIS_URL|g" -e "s|__VPC_CIDR__|$VPC_CIDR|g" "$1"
}

echo "==> kubeconfig ($CONTEXT)"
aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER" --alias "$CONTEXT" >/dev/null

echo "==> image $ECR:$TAG"
if ! aws ecr describe-images --region "$REGION" --repository-name "${ECR##*/}" \
    --image-ids imageTag="$TAG" >/dev/null 2>&1; then
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${ECR%%/*}"
  # Nodes are Graviton (arm64), like Apple-silicon laptops; build for arm64 explicitly anyway.
  docker build --platform linux/arm64 -t "$ECR:$TAG" "$ROOT/gateway"
  docker push "$ECR:$TAG"
fi

echo "==> namespaces"
for ns in vault aigw api-clients monitoring; do
  k create namespace "$ns" --dry-run=client -o yaml | k apply -f - >/dev/null
done

echo "==> Vault TLS certificate"
if ! k -n vault get secret vault-tls >/dev/null 2>&1; then
  # A private CA for this cluster. Its key is discarded after signing; re-issuing means a new CA.
  # The extensions matter: Python 3.13+ rejects CAs without keyUsage (ADR-0005).
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=$CLUSTER-vault-ca" \
    -keyout "$WORK/ca.key" -out "$WORK/ca.crt" -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" -addext "subjectKeyIdentifier=hash" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=vault.vault.svc" \
    -keyout "$WORK/tls.key" -out "$WORK/tls.csr" 2>/dev/null
  cat > "$WORK/tls.ext" <<EOF
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid
subjectAltName=DNS:vault,DNS:vault.vault,DNS:vault.vault.svc,DNS:vault.vault.svc.cluster.local,DNS:vault-0.vault-internal,DNS:localhost,IP:127.0.0.1
EOF
  openssl x509 -req -in "$WORK/tls.csr" -CA "$WORK/ca.crt" -CAkey "$WORK/ca.key" \
    -CAcreateserial -days 365 -extfile "$WORK/tls.ext" -out "$WORK/tls.crt" 2>/dev/null
  k -n vault create secret generic vault-tls --from-file="$WORK/tls.crt" \
    --from-file="$WORK/tls.key" --from-file="$WORK/ca.crt" >/dev/null
  k -n aigw create secret generic vault-ca --from-file="$WORK/ca.crt" \
    --dry-run=client -o yaml | k apply -f - >/dev/null
fi

echo "==> Vault"
helm repo add hashicorp https://helm.releases.hashicorp.com >/dev/null 2>&1 || true
render "$ROOT/deploy/eks/vault-values.yaml" > "$WORK/vault-values.yaml"
helm --kube-context "$CONTEXT" upgrade --install vault hashicorp/vault --version 0.34.1 \
  -n vault -f "$WORK/vault-values.yaml" >/dev/null
k -n vault wait --for=jsonpath='{.status.phase}'=Running pod/vault-0 --timeout=300s >/dev/null

# `vault status` exits 2 while sealed, so read its output instead of trusting the exit code.
status="$(k -n vault exec vault-0 -- vault status -format=json 2>/dev/null || true)"
if ! grep -q '"initialized": true' <<<"$status"; then
  echo "    initializing (KMS auto-unseal: one recovery key, no unseal keys)"
  mkdir -p "$(dirname "$INIT_FILE")"
  (umask 077 && k -n vault exec vault-0 -- vault operator init -recovery-shares=1 \
    -recovery-threshold=1 -format=json > "$INIT_FILE")
fi
ROOT_TOKEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["root_token"])' "$INIT_FILE")"
k -n vault wait --for=condition=Ready pod/vault-0 --timeout=120s >/dev/null

echo "==> Vault configuration"
# Capture before grep: with pipefail, `grep -q` exiting early makes the producer fail (SIGPIPE).
mounts="$(v secrets list -format=json)"
grep -q '"secret/"' <<<"$mounts" || v secrets enable -path=secret kv-v2 >/dev/null
grep -q '"aws/"' <<<"$mounts" || v secrets enable aws >/dev/null
# No access keys: Vault uses its IRSA role (web identity) to call sts:AssumeRole.
v write aws/config/root region="$REGION" >/dev/null
v write aws/roles/bedrock credential_type=assumed_role role_arns="$BEDROCK_ROLE" \
  default_sts_ttl=15m max_sts_ttl=1h >/dev/null
auths="$(v auth list -format=json)"
grep -q '"kubernetes/"' <<<"$auths" || v auth enable kubernetes >/dev/null
v write auth/kubernetes/config kubernetes_host=https://kubernetes.default.svc >/dev/null
v write auth/kubernetes/role/aigw bound_service_account_names=aigw \
  bound_service_account_namespaces=aigw audience=vault policies=aigw ttl=1h >/dev/null
v policy write aigw - < "$ROOT/vault/policies/aigw.hcl" >/dev/null

echo "==> gateway"
render "$ROOT/deploy/eks/aigw-values.yaml" > "$WORK/aigw-values.yaml"
helm --kube-context "$CONTEXT" upgrade --install aigw "$ROOT/helm/aigw" -n aigw \
  -f "$WORK/aigw-values.yaml" --wait --timeout 5m >/dev/null

echo "==> smoke test (from the api-clients namespace, as the NetworkPolicy requires)"
probe() {
  k -n api-clients run "probe-$RANDOM" --rm -i --restart=Never --quiet \
    --image=curlimages/curl:8.11.1 --command -- curl -s -m 30 "$@" 2>/dev/null || true
}
for model in mock-echo bedrock-claude; do
  printf '    %-15s ' "$model"
  probe -o /dev/null -w '%{http_code}\n' http://aigw.aigw.svc:8080/v1/chat/completions \
    -H 'Authorization: Bearer dev-platform-key' -H 'Content-Type: application/json' \
    -d "{\"model\":\"$model\",\"max_tokens\":20,\"messages\":[{\"role\":\"user\",\"content\":\"Say hi.\"}]}"
done
echo
echo "Done. Audit log: kubectl --context $CONTEXT -n aigw logs deploy/aigw | grep '^{'"
