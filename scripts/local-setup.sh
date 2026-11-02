#!/usr/bin/env bash
# Configure the level 2 stack's dev Vault (compose.full.yaml): gateway policy and token, and
# Bedrock credentials if terraform/iam-bedrock has been applied (docs/getting-started.md).
#
# Dev Vault keeps everything in memory, so re-run this (make local-setup) after it restarts.
# Needs only Docker; the Bedrock part also needs terraform and the aws CLI.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="$ROOT/terraform/iam-bedrock"
REGION="${AWS_REGION:-us-west-2}"
GATEWAY_TOKEN="aigw-local-gateway-token"  # matches VAULT_TOKEN in compose.full.yaml

compose() { docker compose -f "$ROOT/compose.yaml" -f "$ROOT/compose.full.yaml" "$@"; }
v() { compose exec -T -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN=root vault vault "$@"; }

echo "==> waiting for Vault"
for _ in $(seq 1 30); do v status >/dev/null 2>&1 && break; sleep 1; done
v status >/dev/null

echo "==> gateway policy and token (aigw policy only)"
v policy write aigw - < "$ROOT/vault/policies/aigw.hcl" >/dev/null
if ! v token lookup "$GATEWAY_TOKEN" >/dev/null 2>&1; then
  # Vault warns that custom token IDs are hashed with SHA1; acceptable for a local-only token.
  v token create -id="$GATEWAY_TOKEN" -policy=aigw -period=24h -display-name=aigw-local \
    >/dev/null 2>&1 || { echo "creating the gateway token failed" >&2; exit 1; }
fi

echo "==> Bedrock credentials"
if role_arn="$(terraform -chdir="$TF_DIR" output -raw bedrock_role_arn 2>/dev/null)" &&
   vault_user="$(terraform -chdir="$TF_DIR" output -raw vault_user_name 2>/dev/null)"; then
  mounts="$(v secrets list -format=json)"  # captured: pipefail + grep -q can SIGPIPE
  grep -q '"aws/"' <<<"$mounts" || v secrets enable aws >/dev/null
  # Vault's own IAM key: replace any old one and hand it straight to Vault, which rotates it so
  # no person ever sees the live key (same as scripts/vault-dev-setup.sh).
  for key in $(aws iam list-access-keys --user-name "$vault_user" \
      --query 'AccessKeyMetadata[].AccessKeyId' --output text); do
    aws iam delete-access-key --user-name "$vault_user" --access-key-id "$key"
  done
  aws iam create-access-key --user-name "$vault_user" --output json \
      --query "AccessKey.{access_key:AccessKeyId,secret_key:SecretAccessKey,region:'$REGION'}" \
    | v write aws/config/root - >/dev/null
  for attempt in $(seq 1 12); do  # new IAM keys take a few seconds to work
    if v write -f aws/config/rotate-root >/dev/null 2>&1; then break; fi
    [ "$attempt" = 12 ] && { echo "rotate-root failed" >&2; exit 1; }
    sleep 5
  done
  v write aws/roles/bedrock credential_type=assumed_role role_arns="$role_arn" \
    default_sts_ttl=15m max_sts_ttl=1h >/dev/null
  echo "    Bedrock models use 15-minute credentials for $role_arn"
else
  echo "    skipped: terraform/iam-bedrock not applied, so the claude-* models will return 502."
  echo "    mock-echo, sign-in, MCP, limits, and dashboards all work without it."
fi

cat <<EOF

Level 2 is up.
  Sign in:   scripts/aigw-login login        (alice / alice-dev-password)
  Claude Code: claude --settings examples/claude-code/settings.local.json
  Keycloak http://localhost:8180  Grafana http://127.0.0.1:3000  Jaeger http://127.0.0.1:16686
EOF
