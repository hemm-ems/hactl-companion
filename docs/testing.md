# hactl-companion — Testing Guide

This document explains how hactl-companion is tested, what the tests verify, how to run them locally, and where coverage gaps exist.

hactl-companion is a small Python (aiohttp) sidecar service that provides YAML file access to HA configuration — the one thing the HA REST/WS API cannot do. Testing it is simpler than testing hactl itself, but still requires care: the service touches the filesystem, handles security-sensitive paths, and must interoperate with a live Home Assistant instance in production.

---

## The Two Layers

Tests are organized into two layers with different scope and cost.

**Unit tests** are fast, isolated, and require nothing beyond Python. They use `pytest-aiohttp`'s built-in test client to exercise every endpoint against a temporary config directory populated with YAML fixtures. No Docker, no network, no running HA. They run in under 5 seconds.

**Integration tests** start a real Home Assistant instance and the companion container side-by-side using Docker Compose, sharing a `/config` volume. They exercise the full stack: Docker networking, auth middleware, filesystem operations on a real HA config directory, and interplay between the two containers.

Each layer has its own `make` target and its own CI job.

---

## Layer 1: Unit Tests

Unit tests live in `tests/` (excluding `tests/integration/`). They use `pytest-aiohttp` which provides the `aiohttp_client` fixture for testing aiohttp applications without starting a real server.

### Running

```bash
make test
# equivalent: uv run pytest tests/ --ignore=tests/integration -v --tb=short
```

This takes roughly 3–5 seconds and requires only Python 3.12+ and dev dependencies.

### What the unit tests cover

| Test file | What it checks |
|---|---|
| `test_health.py` | Liveness endpoint returns `{"status": "ok", "version": "..."}`, no auth required |
| `test_auth.py` | Bearer token validation, invalid/missing token → 401, Ingress header bypass, health exemption |
| `test_config.py` | File listing, file read, block read, secrets exclusion, path traversal rejection |
| `test_config_write.py` | Dry-run diff, apply with backup, validation failure restores backup, empty/invalid YAML rejected |
| `test_resolver.py` | `!include` resolution, `!include_dir_named`, secrets include denied, circular include, null handling |
| `test_templates.py` | Template CRUD: list, get by unique_id, create, update (dry-run + apply), delete, duplicate/missing errors |
| `test_scripts.py` | Script CRUD: list with fields metadata, get, create, update (dry-run + apply), delete, duplicate errors |
| `test_automations.py` | Automation CRUD: list, get by id, create, update (dry-run + apply), delete, duplicate/missing errors |
| `test_ha.py` | HA reload endpoint: domain allowlist, subprocess mocking, error handling |
| `test_openapi.py` | OpenAPI spec validates, all routes have spec entries, all spec entries have routes, 21 endpoints |
| `test_cli.py` | Argument parsing: `--version`, `--help`, custom host/port, log-level |

**86 unit tests** covering all 21 API endpoints plus security boundaries.

### Test fixtures

Fixtures live in `testdata/fixtures/` and are copied into a fresh `tmp_path` for each test:

```
testdata/fixtures/
├── configuration.yaml    # With !include template.yaml and !include_dir_named packages
├── template.yaml         # 2 sensors + 1 binary_sensor with unique_ids
├── scripts.yaml          # 3 scripts (one with fields)
├── automations.yaml      # 3 automations
└── packages/
    ├── energy.yaml       # Package with template sensors
    └── security.yaml     # Package with security automations
```

The `conftest.py` creates a temporary directory for each test, copying all fixtures into it. This ensures tests never interfere with each other — mutations from write tests don't leak across test boundaries.

### Security test coverage

Security is tested explicitly:

- **Path traversal**: Requests with `../etc/passwd` paths → 400
- **secrets.yaml**: Read, write, include, and listing all deny access → 403
- **Empty/invalid YAML**: Rejected before any file is written → 400
- **Auth enforcement**: Missing/invalid tokens → 401
- **Domain allowlist**: HA reload only accepts known integration domains → 400

---

## Layer 2: Integration Tests

Integration tests live in `tests/integration/` and use Docker Compose to stand up the full stack.

### Architecture

The Docker Compose file (`docker-compose.integration.yaml`) creates:

1. **homeassistant** — Official HA stable image with a shared `ha-config` volume
2. **companion** — Built from the local Dockerfile, same shared volume, `SUPERVISOR_TOKEN` set

Both containers share the `ha-net` network. The companion has direct filesystem access to HA's `/config` directory, exactly as it would in production.

### Running

```bash
make test-int
# Steps: docker compose up -d --build → pytest tests/integration → docker compose down -v
```

The first run takes 2–3 minutes (Docker pulls the HA image, ~1 GB). Subsequent runs are faster with a cached image (~60 seconds).

### HA Onboarding

The integration `conftest.py` automates HA's interactive onboarding:

1. Wait for HA's `/api/onboarding` endpoint to respond
2. `POST /api/onboarding/users` — create owner account
3. Exchange `auth_code` for access token
4. Complete `core_config` and `analytics` wizard steps
5. Create long-lived access token via WebSocket API

This produces a valid HA token that proves HA is fully started and `/config` is populated.

### What the integration tests cover

| Test file | What it checks |
|---|---|
| `test_auth.py` | Auth enforcement on a real container: no token → 401, wrong token → 401, valid token → 200, Ingress bypass |
| `test_live.py` `TestHealth` | Health endpoint responds on the live container |
| `test_live.py` `TestConfigRead` | File listing on real HA config, secrets exclusion, path traversal, read real `configuration.yaml` |
| `test_live.py` `TestConfigWrite` | Dry-run no-op, write new file + verify, path traversal rejected, invalid YAML rejected |
| `test_live.py` `TestAutomationsCRUD` | Create automations.yaml, list, get by id |
| `test_live.py` `TestScriptsCRUD` | Create scripts.yaml, list, get by id |
| `test_live.py` `TestTemplatesCRUD` | Create template.yaml, list, get by id |
| `test_no_supervisor.py` | `POST /v1/ha/reload` returns 502 (no `ha` CLI in container), invalid domain → 400 |

---

## CI/CD Enforcement

The CI pipeline (`.github/workflows/ci.yml`) runs on every push to `main` and every PR targeting `main`. It has four parallel jobs:

| Job | What it does | Blocks merge |
|---|---|---|
| **Lint** | `ruff check` + `ruff format --check` + `mypy` strict mode | Yes |
| **Unit Tests** | `uv run pytest tests/ --ignore=tests/integration` | Yes |
| **OpenAPI Contract** | Regenerates spec from code and diffs against committed file | Yes |
| **Docker Build** | `docker build` — ensures the image still builds | Yes |

The release workflow (`.github/workflows/release.yml`) triggers on version tags (`v*`):
1. Runs the unit test suite
2. Builds multi-platform Docker images (amd64 + arm64)
3. Pushes to GitHub Container Registry (`ghcr.io`)

### Branch protection

All four CI jobs must pass before a PR can merge.

---

## Running Tests Locally

The only prerequisite for unit tests is Python 3.12 and `uv`. For integration tests, you also need Docker.

| Goal | Command | Docker needed | Approx. time |
|---|---|---|---|
| Quick sanity check | `make test` | No | ~5 seconds |
| Lint + type check | `make lint` | No | ~3 seconds |
| Full integration suite | `make test-int` | Yes | ~2 min first, ~60s cached |
| Regenerate OpenAPI spec | `make spec` | No | ~1 second |
| Format code | `make fmt` | No | ~1 second |

**Prerequisites check**:

```bash
# Python + uv
uv --version
python --version  # must be 3.12+

# Docker (only for integration tests)
docker info
```

**Troubleshooting**:

- *`aiohttp_client` fixture not found*: Run `uv sync --extra dev` to install `pytest-aiohttp`.
- *Docker Compose fails*: Ensure Docker daemon is running and no port conflicts exist.
- *HA container times out*: First start downloads ~1 GB image. Increase timeout or pre-pull: `docker pull ghcr.io/home-assistant/home-assistant:stable`.
- *Orphaned containers*: `make clean` or `docker compose -f docker-compose.integration.yaml down -v`.

---

## What Is Covered

| Feature | Unit | Integration |
|---|---|---|
| Health endpoint | ✓ | ✓ |
| Auth middleware | ✓ | ✓ |
| Config file list | ✓ | ✓ |
| Config file read | ✓ | ✓ |
| Config file write (dry-run + apply) | ✓ | ✓ |
| Config block read | ✓ | — |
| `!include` resolution | ✓ | — |
| `!include_dir_named` resolution | ✓ | — |
| Template CRUD | ✓ | ✓ |
| Script CRUD | ✓ | ✓ |
| Automation CRUD | ✓ | ✓ |
| HA reload | ✓ | ✓ (502) |
| Path traversal blocked | ✓ | ✓ |
| secrets.yaml denied | ✓ | ✓ |
| Backup creation | ✓ | ✓ |
| OpenAPI spec validity | ✓ | — |
| CLI arg parsing | ✓ | — |
| Docker image builds | — | ✓ (CI) |

---

## Honest Gaps

- **Concurrent writes**: No tests verify behaviour when two requests write the same file simultaneously. The backup mechanism is not atomic.
- **Large files**: Fixtures are small. Performance on a real HA config with hundreds of automations is untested.
- **HA version matrix**: Integration tests run against `stable` only. Changes in HA's default `configuration.yaml` format could surface issues.
- **`!include_dir_list` and `!include_dir_merge_named`**: Implemented and unit-tested via the resolver, but no dedicated integration test exercises them against a real HA directory.
- **Config validation fallback**: The `_validate_config()` function calls `ha core check-config`, which is only available in HAOS. The fallback (skip validation) is tested, but the success path is not exercised in CI.
- **WebSocket/streaming**: The companion does not use WebSocket, but if HA ever changes how it populates `/config`, the companion would not know.

---

## Quick Reference

```bash
# Development workflow
make test                    # Unit tests only (~5s)
make lint                    # ruff + mypy
make fmt                     # Auto-format
make spec                    # Regenerate OpenAPI spec

# Integration testing
make test-int                # Full Docker Compose cycle (~2 min)
make clean                   # Tear down orphaned containers

# CI pipeline (runs automatically on push/PR)
# Lint → Unit Tests → OpenAPI Contract → Docker Build
```
