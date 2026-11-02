# Entry points for the three ways to run the gateway (docs/getting-started.md).
# Service development (tests, lint, running from source) lives in gateway/Makefile.

AIGW_PORT ?= 8080
export AIGW_PORT

.PHONY: quickstart local local-setup down check

quickstart:  ## Level 1: gateway + mock model + demo MCP server, Docker only
	docker compose up -d --build
	@echo "Gateway on http://127.0.0.1:$(AIGW_PORT)  (keys: dev-platform-key, dev-data-key)"

local:  ## Level 2: adds Keycloak sign-in, Vault, Redis, Bedrock models, dashboards
	docker compose -f compose.yaml -f compose.full.yaml up -d --build
	scripts/local-setup.sh

local-setup:  ## Level 2: re-run Vault setup (dev Vault forgets everything on restart)
	scripts/local-setup.sh

down:  ## Stop either level
	docker compose -f compose.yaml -f compose.full.yaml down

check:  ## Would these policies start in production mode?
	@for f in policies/*.yaml; do gateway/.venv/bin/python -m aigw.check "$$f"; done
