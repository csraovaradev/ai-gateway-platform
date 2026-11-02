# Security policy

AI Gateway sits between people, models, tools, and cloud credentials, so security reports get priority.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository. Include what you found, how to reproduce it,
and the impact you expect. You should get a reply within a few days.

## Scope

In scope: the gateway service (`gateway/`), the Helm chart, the Terraform modules, the Vault policies,
the setup scripts, and `scripts/aigw-login`.

Out of scope:

- The public demo keys (`dev-platform-key`, `dev-data-key`) and the demo Keycloak users and passwords.
  They are published on purpose, for local use only, and `mode: production` refuses to start with them.
- Vulnerabilities in third-party components (Vault, Keycloak, Redis, Bedrock); please report those
  upstream.

## Design references

- [Threat model](docs/threat-model.md): every trust boundary, with its mitigations and accepted risks.
- [ADRs](docs/adr/): why credentials are short-lived (0001), how MCP tools are filtered (0002), transport
  security (0005), user identity (0007), and production-mode guardrails (0008).
