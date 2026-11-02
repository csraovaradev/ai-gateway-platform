# Contributing

Thanks for taking a look. Bug reports, questions, and pull requests are all welcome.

## Run it

```bash
make quickstart              # Docker only: gateway, mock model, demo MCP server
make local                   # adds SSO (Keycloak), Vault, Redis, Bedrock models, dashboards
```

See [docs/getting-started.md](docs/getting-started.md) for both.

## Develop

```bash
cd gateway
make install                 # virtualenv and dependencies
make lint                    # ruff
make test                    # pytest; set AIGW_TEST_REDIS_URL=redis://127.0.0.1:6379/0 for the Redis tests
make run                     # from source, http://localhost:8080
```

CI runs lint, tests, the production-mode policy check, Terraform validate, Helm lint, ShellCheck, and a
Docker build. Please make sure `make lint test` passes before opening a pull request.

## Guidelines

- **Small pull requests with tests.** Every behaviour change comes with a test; security-relevant
  changes also need a failure-mode test (see `gateway/tests/test_failure_modes.py`).
- **Never log content.** Audit lines, metrics, and spans carry metadata only, never prompts, completions,
  or tool arguments. Metric labels come only from policy values, never from client input.
- **Fail closed.** If a policy check cannot be made (for example, Redis is down), refuse the request.
- **Record significant decisions** as an ADR in [`docs/adr/`](docs/adr/), and update the
  [threat model](docs/threat-model.md) when a trust boundary changes.
- **No secrets in the repository**: no real keys, `.env`, `*.tfvars`, `*.tfstate`, or `*.pem`.

Security problems go through [SECURITY.md](SECURITY.md), not public issues.
