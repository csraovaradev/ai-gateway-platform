#!/usr/bin/env bash
# Configure a local dev-mode Vault for the gateway (ADR-0001).
#
# Prereqs: `terraform apply` in terraform/iam-bedrock, and a dev Vault running:
#   vault server -dev -dev-root-token-id=root
#
# Dev Vault keeps everything in memory, so re-run this after every Vault restart.
# Prints the export line for a gateway token limited to the `aigw` policy.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TF_DIR="$ROOT/terraform/iam-bedrock"
REGION="${AWS_REGION:-us-west-2}"
export VAULT_ADDR="${VAULT_ADDR:-http://127.0.0.1:8200}"
export VAULT_TOKEN="${VAULT_ROOT_TOKEN:-root}"

role_arn="$(terraform -chdir="$TF_DIR" output -raw bedrock_role_arn)"
vault_user="$(terraform -chdir="$TF_DIR" output -raw vault_user_name)"

echo "==> AWS secrets engine"
vault secrets list -format=json | grep -q '"aws/"' || vault secrets enable aws

# Vault needs its own AWS key to call sts:AssumeRole. Dev Vault forgets it on restart, so
# drop any old keys, create a new one, and pipe it straight into Vault (never echoed or
# written to disk). Vault then rotates it, so no human has ever seen the live key.
for key in $(aws iam list-access-keys --user-name "$vault_user" \
    --query 'AccessKeyMetadata[].AccessKeyId' --output text); do
  aws iam delete-access-key --user-name "$vault_user" --access-key-id "$key"
done
aws iam create-access-key --user-name "$vault_user" --output json \
    --query "AccessKey.{access_key:AccessKeyId,secret_key:SecretAccessKey,region:'$REGION'}" \
  | vault write aws/config/root - >/dev/null

echo "==> Rotating Vault's root key (waits for IAM to propagate)"
for attempt in $(seq 1 12); do
  if vault write -f aws/config/rotate-root >/dev/null 2>&1; then break; fi
  [ "$attempt" = 12 ] && { echo "rotate-root failed" >&2; exit 1; }
  sleep 5
done

vault write aws/roles/bedrock credential_type=assumed_role role_arns="$role_arn" \
  default_sts_ttl=15m max_sts_ttl=1h >/dev/null

echo "==> Upstream API key placeholder (Ollama ignores it)"
vault kv put secret/aigw/upstream/local-llama api_key=not-needed-for-ollama >/dev/null

echo "==> Gateway policy and token"
vault policy write aigw "$ROOT/vault/policies/aigw.hcl" >/dev/null
token="$(vault token create -policy=aigw -period=24h -display-name=aigw-gateway -field=token)"

echo
echo "Vault ready. In the shell where you run the gateway:"
echo "  export VAULT_TOKEN=$token"
