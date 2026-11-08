# ADR-0012: MCP calls on behalf of the signed-in person

- **Status:** Accepted
- **Date:** 2026-11-08
- **Follows:** ADR-0002, ADR-0007

## Context

The gateway calls MCP servers with one service token per server, read from Vault (ADR-0002). The
gateway knows which person is calling (ADR-0007), but the MCP server does not: to Jira, every request
is "the gateway". The server's own permissions and audit trail therefore cannot apply to the person,
and a tool that should only show a person their own records cannot tell whose they are.

Forwarding the person's sign-in token would be wrong: it was issued to the gateway (its audience is
the gateway), so any server holding it could replay it against the gateway or anything else that
trusts that audience.

## Decision

1. **OAuth 2.0 Token Exchange (RFC 8693).** For an MCP server with `on_behalf_of: <audience>`, the
   gateway sends the person's token to the identity provider's token endpoint, authenticating as its
   own confidential client (`oidc.client_id` plus a client secret), and receives a new token:
   - issued for that audience only (`aud: jira-mcp`), so it is useless anywhere else;
   - naming the same person and groups, so the server can apply its own permissions;
   - recording that the gateway requested it (`azp: aigw`).

   In AWS terms: AssumeRole with session tags, a narrower credential that keeps the caller's
   identity.
2. **The person's own token never leaves the gateway.** It is kept in request memory only, never
   logged, and replaced by the exchanged token in the upstream request.
3. **API-key callers** (services, CI) have no person to act as, so they keep the server's service
   token from Vault, as before.
4. **Fail closed.** If the exchange is refused or the identity provider is unreachable, the call is
   refused with 502 and the reason is audited (`token_exchange_refused`,
   `token_exchange_unavailable`). The gateway never falls back to the service token for a
   signed-in person.
5. **Caching.** Exchanged tokens are cached per person and audience until 30 seconds before they
   expire, measured on the wall clock (the lesson of ADR-0009). The cache holds at most 1,024
   entries.
6. **The gateway's client secret** comes from Vault KV (`client_secret_vault_path`, field
   `client_secret`) in production. An environment variable (`client_secret_env`) is allowed for
   laptops, and `mode: production` refuses it (ADR-0008).

## Consequences

- An MCP server can verify, with the identity provider's public keys, which person is acting and
  through which gateway, and enforce its own permissions for that person.
- The identity provider must allow the exchange. In Keycloak 26 that means standard token exchange
  enabled on the gateway's client, and the MCP server's client ID reachable as an audience from it
  (an audience mapper). Entra ID's equivalent is the on-behalf-of flow; Okta supports RFC 8693.
- One extra round trip to the identity provider per person, server, and token lifetime.
- MCP servers must actually validate the token (signature, issuer, `aud`). One that ignores it gains
  nothing from this.
- Verified live with Keycloak: a call as alice reached a test MCP server with a token that the server
  verified against Keycloak's keys as `aud=jira-mcp`, `azp=aigw`, user alice. A call with a service
  key carried no person's token.
