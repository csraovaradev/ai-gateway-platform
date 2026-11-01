CONFIG ?= ../policies/gateway.example.yaml
IMAGE  ?= ai-gateway:dev

.PHONY: install test lint run docker docker-run

install:
	python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check .

run:
	GATEWAY_CONFIG=$(CONFIG) .venv/bin/uvicorn --factory aigw.main:create_app --reload --port 8080

docker:
	docker build -t $(IMAGE) .

docker-run:
	docker run --rm -p 8080:8080 -v $(abspath $(CONFIG)):/etc/aigw/gateway.yaml:ro $(IMAGE)
