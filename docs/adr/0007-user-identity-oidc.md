# ADR-0007: User identity through the company identity provider (OIDC)

- **Status:** Accepted
- **Date:** 2026-11-02
- **Follows:** ADR-0001, ADR-0002, ADR-0006

## Context

Teams authenticate with API keys (SHA-256 hashes in the policy). That is enough for services, but for
people it has three problems:

- **No person in the audit log.** Everyone on a team shares one key, so a line says `team: platform`,
  never who.
- **Keys do not expire.** Revoking one person means rotating the key for the whole team.
- **Joiners and leavers are manual.** Access lives in the policy file instead of the directory the
  company already manages (Entra ID, Okta, Google Workspace).

Enterprises solve this with single sign-on: the identity provider (IdP) authenticates the person and
issues a short-lived signed token, and applications trust the token. OpenID Connect (OIDC) is the
standard all of those IdPs speak, so the gateway can support every one of them with one implementation.
It works like STS `AssumeRoleWithWebIdentity`: AWS does not care which IdP signed the token, only that
it trusts the issuer and the signature checks out.

## Decision

1. **The gateway verifies IdP-issued JWT access tokens** (`aigw/oidc.py`), configured by an `oidc:`
   section: `issuer` (must equal the token's `iss`), `audience` (must be in `aud`), and optionally
   `jwks_url`, `groups_claim`, `user_claim`.
   - Signature checked against the IdP's published keys (JWKS), found through the discovery
     document unless `jwks_url` is set.
   - Asymmetric algorithms only (RS/PS/ES). `HS*` and `none` cannot be configured, which rules out
     algorithm-confusion attacks.
   - `exp`, `iat`, `iss`, `aud`, `sub` are required; 30 s clock-skew leeway.
   - Keys are cached for 5 minutes. An unknown `kid` (the IdP rotated keys) triggers a refetch, at most
     every 30 s, so made-up kids cannot become a flood of requests to the IdP. If the IdP is down, cached
     keys keep working; with no keys at all the gateway answers 503.
2. **Groups map to teams.** A team lists `oidc_groups`; a user acts as the first team (in policy order)
   that shares a group with their token. Everything else (models, tools, limits, budgets) is unchanged.
   A valid user with no mapped group gets 403 and an audited `no_team_for_groups`.
3. **API keys stay**, alongside tokens, for CI jobs and services. The gateway tells them apart by shape
   (a JWT is three dot-separated parts; generated keys contain no dots).
4. **The audit log records `user`** (`user_claim`, default `sub`) on every line for signed-in requests,
   including denials. It is never a metric label: users are unbounded (ADR-0004).
5. **MCP sessions belong to the user** who opened them, not just their team, so a teammate cannot
   reuse someone's session ID.
6. **Developers sign in with `scripts/aigw-login`**, which works like `aws sso login`:
   - `aigw-login login` runs the OAuth device flow once (browser sign-in, MFA if the IdP requires it).
   - `aigw-login token` is Claude Code's `apiKeyHelper`. It prints the cached access token and silently
     refreshes it near expiry. Claude Code reruns it every 5 minutes and on any 401, so a 15-minute
     token never logs anyone out; the IdP's session lifetime (8–10 h in the demo realm) decides when the
     browser sign-in comes back.
   - Standard library Python only, so developers install nothing; tokens cached with mode 600.
7. **Local demo IdP: Keycloak** in level 2 (`compose.full.yaml`, `deploy/local/keycloak/aigw-realm.json`).
   Users alice (eng-platform), bob (eng-data), carol (no team). The realm also allows the password grant
   so scripts can get tokens without a browser; that is local-only and disabled in any real IdP setup.

## Consequences

- Swapping Keycloak for Entra ID or Okta is configuration: the issuer URL, audience, and group names
  (in Entra ID, group object IDs, unless the app is set to emit names).
- Rate limits and budgets stay per team. Per-user limits are a follow-up.
- Revoking a person is done in the IdP. Their current access token keeps working until it expires
  (15 minutes in the demo realm); keep access token lifetimes short.
- MCP upstream calls still use the server's service token from Vault. Forwarding the user's identity
  to MCP servers (OAuth token exchange, on-behalf-of) is a follow-up.
- Entra ID puts at most 200 groups in a token ("groups overage"); large directories should use app
  roles or group filtering on the app registration.
