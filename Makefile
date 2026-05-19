COMPOSE_FILE := docker-compose.integration.yaml
WG_COMPOSE_FILE := docker-compose.wireguard.yaml

.PHONY: test test-int test-wg lint fmt clean spec

test:
	uv run pytest tests/ --ignore=tests/integration -v --tb=short

test-int:
	docker compose -f $(COMPOSE_FILE) down -v 2>/dev/null || true
	uv run pytest tests/integration -v --tb=short -x -s --ignore=tests/integration/test_wireguard.py; \
	status=$$?; \
	docker compose -f $(COMPOSE_FILE) down -v 2>/dev/null || true; \
	exit $$status

test-wg:
	docker compose -f $(WG_COMPOSE_FILE) down -v 2>/dev/null || true
	uv run pytest tests/integration/test_wireguard.py -v --tb=short -x -s; \
	status=$$?; \
	docker compose -f $(WG_COMPOSE_FILE) down -v 2>/dev/null || true; \
	exit $$status

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/
	uv run mypy

fmt:
	uv run ruff format src/ tests/

spec:
	uv run python -c "from companion.openapi import write_spec; write_spec()"

clean:
	docker compose -f $(COMPOSE_FILE) down -v 2>nul || true
	docker compose -f $(WG_COMPOSE_FILE) down -v 2>nul || true
