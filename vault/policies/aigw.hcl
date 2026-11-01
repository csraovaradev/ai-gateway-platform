# Vault policy for the gateway: read-only access to exactly the credentials it serves.
# Attached to the gateway's token (local) or Kubernetes auth role (cluster).

path "aws/creds/bedrock" {
  capabilities = ["read"]
}

path "secret/data/aigw/upstream/*" {
  capabilities = ["read"]
}
