COMPOSE_FILE := docker-compose.integration.yaml
WG_COMPOSE_FILE := docker-compose.wireguard.yaml

.PHONY: test test-int test-wg lint lint-workflows check-markers fmt clean spec

# check-markers — a [NEEDS ORACLE: ...] marker records an assumption about HA
# that has not been verified against a live instance. Markers may exist on a
# branch; they may not merge. Resolve by probing, then delete the marker.
#
# The exclusions are the files that *document* the rule and so must be free to
# name the token: this Makefile, AGENTS.md, and docs/testing.md. Everywhere
# else, naming it means owning an unresolved assumption.
check-markers:
	@if git grep -n --untracked "NEEDS ORACLE" -- ':!Makefile' ':!AGENTS.md' ':!docs/testing.md'; then \
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

# lint-workflows — GitHub does not reject an invalid workflow file at push
# time; it records a zero-job "workflow file issue" run and never executes it.
# monthly-release.yml sat in that state from 2026-08-02 to 2026-09-29 and two
# monthly releases silently never happened. actionlint parses every workflow
# the way GitHub does (plus shellcheck on each `run:` block, warnings and up),
# so a broken workflow fails CI instead. Same pinned image locally and in CI.
ACTIONLINT_IMAGE := rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667

lint-workflows:
	docker run --rm -e SHELLCHECK_OPTS=--severity=warning \
	  -v "$(CURDIR):/repo" -w /repo $(ACTIONLINT_IMAGE) -color

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
