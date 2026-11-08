#!/usr/bin/env bash
# Deploy Vault, the gateway, and (with a domain) the public edge to the EKS cluster from
# terraform/eks, then smoke-test it.
#
# Prereqs: `terraform apply` in terraform/iam-bedrock and terraform/eks, then re-apply iam-bedrock with
#   -var 'vault_role_arns=["<terraform -chdir=terraform/eks output -raw vault_role_arn>"]'
# With `domain_name` set in terraform/eks, also deploys the ALB controller, ExternalDNS, and Keycloak,
# and serves https://api.<domain> and https://sso.<domain> (ADR-0013).
# Tools: aws, kubectl, helm, docker, openssl, python3.
#
# Safe to re-run: every step checks what already exists. Secrets are generated once and kept in
# ~/.aigw/ (mode 600): vault-init-<cluster>.json (Vault root token and recovery key),
# keys-<cluster>.json (team API keys), sso-<cluster>.json (Keycloak admin, gateway client secret,
# your sign-in). Nothing secret is written to the repository.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tf() { terraform -chdir="$ROOT/terraform/eks" output -raw "$1"; }

REGION="$(tf region)"
CLUSTER="$(tf cluster_name)"
ECR="$(tf ecr_repository_url)"
KEYCLOAK_ECR="$(tf keycloak_repository_url)"
VAULT_ROLE="$(tf vault_role_arn)"
KMS_KEY="$(tf vault_kms_key_id)"
REDIS_URL="$(tf redis_url)"
VPC_CIDR="$(tf vpc_cidr)"
VPC_ID="$(tf vpc_id)"
ALB_CIDR="$(tf alb_cidr)"
BEDROCK_ROLE="$(tf bedrock_role_arn)"
AUDIT_BUCKET="$(tf audit_bucket)"
FLUENT_BIT_ROLE="$(tf fluent_bit_role_arn)"
DOMAIN="$(tf domain_name)"
API_HOST="$(tf api_host)"
SSO_HOST="$(tf sso_host)"
CERT_ARN="$(tf certificate_arn)"
WAF_ARN="$(tf waf_acl_arn)"
LB_ROLE="$(tf lb_controller_role_arn)"
DNS_ROLE="$(tf external_dns_role_arn)"
TAG="$(git -C "$ROOT" rev-parse --short HEAD)"
CONTEXT="aigw-eks"
SECRETS="$HOME/.aigw"
INIT_FILE="$SECRETS/vault-init-$CLUSTER.json"
KEYS_FILE="$SECRETS/keys-$CLUSTER.json"
SSO_FILE="$SECRETS/sso-$CLUSTER.json"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

k() { kubectl --context "$CONTEXT" "$@"; }
v() { k -n vault exec -i vault-0 -- env VAULT_TOKEN="$ROOT_TOKEN" vault "$@"; }
json() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"; }
sha256() { printf '%s' "$1" | shasum -a 256 | cut -d' ' -f1; }

echo "==> secrets (generated once, kept in $SECRETS)"
mkdir -p "$SECRETS" && chmod 700 "$SECRETS"
if [ ! -f "$KEYS_FILE" ]; then
  (umask 077 && python3 -c 'import json,secrets; print(json.dumps({
      t: f"aigw_{t}_{secrets.token_urlsafe(32)}" for t in ("platform", "data")}))' > "$KEYS_FILE")
fi
if [ ! -f "$SSO_FILE" ]; then
  (umask 077 && python3 -c 'import json,secrets; print(json.dumps({
      "admin_password": secrets.token_urlsafe(24), "gateway_client_secret": secrets.token_urlsafe(32),
      "jira_mcp_client_secret": secrets.token_urlsafe(32), "owner_username": "owner",
      "owner_password": secrets.token_urlsafe(18)}))' > "$SSO_FILE")
fi
PLATFORM_KEY="$(json "$KEYS_FILE" platform)"
DATA_KEY="$(json "$KEYS_FILE" data)"

render() {
  sed -e "s|__VAULT_ROLE_ARN__|$VAULT_ROLE|g" -e "s|__KMS_KEY_ID__|$KMS_KEY|g" \
      -e "s|__REGION__|$REGION|g" -e "s|__IMAGE_REPO__|$ECR|g" -e "s|__IMAGE_TAG__|$TAG|g" \
      -e "s|__REDIS_URL__|$REDIS_URL|g" -e "s|__VPC_CIDR__|$VPC_CIDR|g" \
      -e "s|__VPC_ID__|$VPC_ID|g" -e "s|__ALB_CIDR__|$ALB_CIDR|g" -e "s|__CLUSTER__|$CLUSTER|g" \
      -e "s|__AUDIT_BUCKET__|$AUDIT_BUCKET|g" -e "s|__FLUENT_BIT_ROLE_ARN__|$FLUENT_BIT_ROLE|g" \
      -e "s|__DOMAIN__|$DOMAIN|g" -e "s|__API_HOST__|$API_HOST|g" -e "s|__SSO_HOST__|$SSO_HOST|g" \
      -e "s|__CERT_ARN__|$CERT_ARN|g" -e "s|__WAF_ARN__|$WAF_ARN|g" \
      -e "s|__LB_ROLE_ARN__|$LB_ROLE|g" -e "s|__DNS_ROLE_ARN__|$DNS_ROLE|g" \
      -e "s|__KEYCLOAK_IMAGE__|$KEYCLOAK_ECR:$TAG|g" \
      -e "s|__PLATFORM_KEY_SHA256__|$(sha256 "$PLATFORM_KEY")|g" \
      -e "s|__DATA_KEY_SHA256__|$(sha256 "$DATA_KEY")|g" "$1"
}

echo "==> kubeconfig ($CONTEXT)"
aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER" --alias "$CONTEXT" >/dev/null

push_image() {  # repository, build context, [dockerfile]
  if ! aws ecr describe-images --region "$REGION" --repository-name "${1##*/}" \
      --image-ids imageTag="$TAG" >/dev/null 2>&1; then
    aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "${1%%/*}"
    # Nodes are Graviton (arm64), like Apple-silicon laptops; build for arm64 explicitly anyway.
    docker build --platform linux/arm64 -t "$1:$TAG" ${3:+-f "$3"} "$2"
    docker push "$1:$TAG"
  fi
}
echo "==> images ($TAG)"
push_image "$ECR" "$ROOT/gateway"
[ -n "$DOMAIN" ] && push_image "$KEYCLOAK_ECR" "$ROOT/deploy/local/keycloak" "$ROOT/deploy/eks/keycloak/Dockerfile"

echo "==> namespaces"
for ns in vault aigw api-clients monitoring logging ${DOMAIN:+auth}; do
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
  # Every replica's own name too: Raft peers verify each other (ADR-0013).
  cat > "$WORK/tls.ext" <<EOF
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth,clientAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid
subjectAltName=DNS:vault,DNS:vault.vault,DNS:vault.vault.svc,DNS:vault.vault.svc.cluster.local,DNS:vault-active.vault.svc,DNS:*.vault-internal,DNS:*.vault-internal.vault.svc.cluster.local,DNS:localhost,IP:127.0.0.1
EOF
  openssl x509 -req -in "$WORK/tls.csr" -CA "$WORK/ca.crt" -CAkey "$WORK/ca.key" \
    -CAcreateserial -days 365 -extfile "$WORK/tls.ext" -out "$WORK/tls.crt" 2>/dev/null
  k -n vault create secret generic vault-tls --from-file="$WORK/tls.crt" \
    --from-file="$WORK/tls.key" --from-file="$WORK/ca.crt" >/dev/null
  k -n aigw create secret generic vault-ca --from-file="$WORK/ca.crt" \
    --dry-run=client -o yaml | k apply -f - >/dev/null
fi

echo "==> Vault (3 replicas, Raft)"
helm repo add hashicorp https://helm.releases.hashicorp.com >/dev/null 2>&1 || true
render "$ROOT/deploy/eks/vault-values.yaml" > "$WORK/vault-values.yaml"
helm --kube-context "$CONTEXT" upgrade --install vault hashicorp/vault --version 0.34.1 \
  -n vault -f "$WORK/vault-values.yaml" >/dev/null
k -n vault wait --for=jsonpath='{.status.phase}'=Running pod/vault-0 --timeout=300s >/dev/null

# `vault status` exits 2 while sealed, so read its output instead of trusting the exit code.
status="$(k -n vault exec vault-0 -- vault status -format=json 2>/dev/null || true)"
if ! grep -q '"initialized": true' <<<"$status"; then
  echo "    initializing vault-0 (KMS auto-unseal: one recovery key, no unseal keys)"
  (umask 077 && k -n vault exec vault-0 -- vault operator init -recovery-shares=1 \
    -recovery-threshold=1 -format=json > "$INIT_FILE")
fi
ROOT_TOKEN="$(json "$INIT_FILE" root_token)"
# vault-1 and vault-2 join through retry_join and unseal themselves with the same KMS key.
k -n vault wait --for=condition=Ready pod/vault-0 pod/vault-1 pod/vault-2 --timeout=300s >/dev/null
echo "    raft peers: $(v operator raft list-peers -format=json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["data"]["config"]["servers"]))')"

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
if [ -n "$DOMAIN" ]; then
  # The gateway's client secret for token exchange (ADR-0012): only Vault and Keycloak hold it.
  json "$SSO_FILE" gateway_client_secret | k -n vault exec -i vault-0 -- env VAULT_TOKEN="$ROOT_TOKEN" \
    sh -c 'vault kv put secret/aigw/oidc client_secret=- >/dev/null'
fi

if [ -n "$DOMAIN" ]; then
  echo "==> public edge: ALB controller, ExternalDNS ($DOMAIN)"
  helm repo add eks https://aws.github.io/eks-charts >/dev/null 2>&1 || true
  helm repo add external-dns https://kubernetes-sigs.github.io/external-dns/ >/dev/null 2>&1 || true
  helm repo update eks external-dns >/dev/null
  render "$ROOT/deploy/eks/lb-controller-values.yaml" > "$WORK/lbc.yaml"
  helm --kube-context "$CONTEXT" upgrade --install aws-load-balancer-controller \
    eks/aws-load-balancer-controller --version 3.6.2 -n kube-system -f "$WORK/lbc.yaml" --wait >/dev/null
  render "$ROOT/deploy/eks/external-dns-values.yaml" > "$WORK/edns.yaml"
  helm --kube-context "$CONTEXT" upgrade --install external-dns external-dns/external-dns \
    --version 1.23.0 -n kube-system -f "$WORK/edns.yaml" --wait >/dev/null

  echo "==> Keycloak (https://$SSO_HOST)"
  # The local demo realm, minus its demo users and the password grant, plus one account for you.
  python3 - "$ROOT/deploy/local/keycloak/aigw-realm.json" "$SSO_FILE" "$DOMAIN" > "$WORK/realm.json" <<'PY'
import json, sys
realm, sso, domain = json.load(open(sys.argv[1])), json.load(open(sys.argv[2])), sys.argv[3]
realm["users"] = [{
    "username": sso["owner_username"], "enabled": True, "emailVerified": True,
    "email": f"{sso['owner_username']}@{domain}", "firstName": "Gateway", "lastName": "Owner",
    "credentials": [{"type": "password", "value": sso["owner_password"], "temporary": False}],
    "groups": ["/eng-platform"]}]
secrets = {"aigw": sso["gateway_client_secret"], "jira-mcp": sso["jira_mcp_client_secret"]}
for client in realm["clients"]:
    if client["clientId"] in secrets:
        client["secret"] = secrets[client["clientId"]]
    if client["clientId"] == "aigw-cli":
        client["directAccessGrantsEnabled"] = False  # no password grant on a public endpoint
realm.update(internationalizationEnabled=True, supportedLocales=["en"], defaultLocale="en",
             localizationTexts={"en": {"aigwEnvironment": "Demo environment"}})
print(json.dumps(realm))
PY
  k -n auth create secret generic keycloak-realm --from-file=aigw-realm.json="$WORK/realm.json" \
    --dry-run=client -o yaml | k apply -f - >/dev/null
  k -n auth create secret generic keycloak-admin \
    --from-literal=password="$(json "$SSO_FILE" admin_password)" \
    --dry-run=client -o yaml | k apply -f - >/dev/null
  render "$ROOT/deploy/eks/keycloak/keycloak.yaml" | k apply -f - >/dev/null
  k -n auth rollout restart deploy/keycloak >/dev/null  # re-import the realm from the Secret
  k -n auth rollout status deploy/keycloak --timeout=300s >/dev/null
fi

echo "==> Fluent Bit (audit log to s3://$AUDIT_BUCKET, Object Lock)"
helm repo add fluent https://fluent.github.io/helm-charts >/dev/null 2>&1 || true
render "$ROOT/deploy/eks/fluent-bit-values.yaml" > "$WORK/fluent-bit.yaml"
helm --kube-context "$CONTEXT" upgrade --install fluent-bit fluent/fluent-bit --version 0.58.3 \
  -n logging -f "$WORK/fluent-bit.yaml" --wait >/dev/null

echo "==> gateway (mode: production)"
render "$ROOT/deploy/eks/aigw-values.yaml" > "$WORK/aigw-values.yaml"
overlay=()
if [ -n "$DOMAIN" ]; then
  render "$ROOT/deploy/eks/aigw-values.edge.yaml" > "$WORK/aigw-edge.yaml"
  overlay=(-f "$WORK/aigw-edge.yaml")
fi
helm --kube-context "$CONTEXT" upgrade --install aigw "$ROOT/helm/aigw" -n aigw \
  -f "$WORK/aigw-values.yaml" ${overlay[@]+"${overlay[@]}"} --wait --timeout 5m >/dev/null

echo "==> smoke test (in-cluster, from the api-clients namespace, as the NetworkPolicy requires)"
probe() {
  k -n api-clients run "probe-$RANDOM" --rm -i --restart=Never --quiet \
    --image=curlimages/curl:8.11.1 --command -- curl -s -m 30 "$@" 2>/dev/null || true
}
for model in mock-echo bedrock-claude; do
  printf '    %-15s ' "$model"
  probe -o /dev/null -w '%{http_code}\n' http://aigw.aigw.svc:8080/v1/chat/completions \
    -H "Authorization: Bearer $PLATFORM_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$model\",\"max_tokens\":20,\"messages\":[{\"role\":\"user\",\"content\":\"Say hi.\"}]}"
done

if [ -n "$DOMAIN" ]; then
  echo "==> smoke test (public: DNS and the ALB can take a few minutes the first time)"
  for _ in $(seq 1 40); do
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "https://$API_HOST/v1/models" \
      -H "Authorization: Bearer $PLATFORM_KEY" || true)"
    [ "$code" = 200 ] && break
    sleep 15
  done
  echo "    https://$API_HOST/v1/models                    $code (want 200)"
  echo "    https://$SSO_HOST/realms/aigw (discovery)       $(curl -s -o /dev/null -w '%{http_code}' -m 10 "https://$SSO_HOST/realms/aigw/.well-known/openid-configuration") (want 200)"
  echo "    https://$SSO_HOST/admin (must not be routed)  $(curl -s -o /dev/null -w '%{http_code}' -m 10 "https://$SSO_HOST/admin/") (want 404)"
  echo "    https://$API_HOST/metrics (must not be routed) $(curl -s -o /dev/null -w '%{http_code}' -m 10 "https://$API_HOST/metrics") (want 404)"
fi

cat <<EOF

Done. Team API keys: $KEYS_FILE  Sign-in and Keycloak secrets: $SSO_FILE (both mode 600)
Audit log: kubectl --context $CONTEXT -n aigw logs deploy/aigw | grep '^{'  (and s3://$AUDIT_BUCKET/audit/)
EOF
if [ -n "$DOMAIN" ]; then
  cat <<EOF
Sign in from your laptop as '$(json "$SSO_FILE" owner_username)' (password in $SSO_FILE):
  scripts/aigw-login login --issuer https://$SSO_HOST/realms/aigw
Before 'terraform destroy': scripts/eks-teardown.sh (removes the ALB and DNS records it made).
EOF
fi
