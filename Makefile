COMPOSE_FILE := docker-compose.integration.yaml
WG_COMPOSE_FILE := docker-compose.wireguard.yaml

.PHONY: test test-int test-wg lint check-markers fmt clean spec

# check-markers — a [NEEDS ORACLE: ...] marker records an assumption about HA
# that has not been verified against a live instance. Markers may exist on a
# branch; they may not merge. Resolve by probing, then delete the marker.
check-markers:
	@if git grep -n --untracked "NEEDS ORACLE" -- ':!Makefile' ':!AGENTS.md'; then \
	  echo "ERROR: unresolved [NEEDS ORACLE] markers — probe a live HA, then remove them."; \
	  exit 1; \
	fi

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

lint: check-markers
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
