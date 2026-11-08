# Vault policy for the gateway: read-only access to exactly the credentials it serves.
# Attached to the gateway's token (local) or Kubernetes auth role (cluster).

path "aws/creds/bedrock" {
  capabilities = ["read"]
}

path "secret/data/aigw/upstream/*" {
  capabilities = ["read"]
}

# MCP servers' service tokens, for API-key callers (ADR-0002).
path "secret/data/aigw/mcp/*" {
  capabilities = ["read"]
}

# The gateway's own client secret at the identity provider, for token exchange (ADR-0012).
path "secret/data/aigw/oidc" {
  capabilities = ["read"]
}
