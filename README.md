# hactl-companion

Home Assistant Add-on that exposes HA-internal features (config files, Supervisor API, logs, HA CLI) for the [hactl](https://github.com/hemm-ems/hactl) CLI.

## What it does

hactl-companion runs as an HA Add-on (aiohttp server) accessible only via Ingress. It bridges the gap between hactl (external Go CLI) and HA internals that aren't reachable through the standard REST/WS API:

- **Config Read/Write** — List, read, and write YAML config files with dry-run diffs and automatic backups
- **Supervisor Proxy** — Query system info, add-ons, and backups via the Supervisor API
- **Log Access** — Read Core, Supervisor, and Add-on logs with filtering
- **HA CLI Bridge** — Trigger reloads, restarts, and config checks

## Architecture

```
HA OS / Supervised
├── HA Core (REST/WS API)
├── hactl-companion Add-on (aiohttp, Ingress only, port 9100)
│   ├── /config (bind mount, read/write)
│   ├── Supervisor API (http://supervisor)
│   └── ha CLI (subprocess)
└── hactl (Go CLI, external) → HA Ingress → companion
```

## API Overview

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/v1/health` | Liveness check |
| GET | `/v1/config/files` | List YAML config files |
| GET | `/v1/config/file?path=...` | Read a config file |
| GET | `/v1/config/block?path=...&id=...` | Read a specific block |
| PUT | `/v1/config/file?path=...&dry_run=true` | Diff preview |
| PUT | `/v1/config/file?path=...&dry_run=false` | Write with backup + validation |
| GET | `/v1/config/helpers?domain=...` | List helpers (optional domain filter) |
| GET | `/v1/config/helper?id=...` | Get a single helper definition |
| POST | `/v1/config/helper` | Create a helper (JSON: domain, id, content) |
| PUT | `/v1/config/helper` | Update a helper (JSON: domain, id, content) |
| DELETE | `/v1/config/helper?domain=...&id=...` | Delete a helper |

## Development

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv venv .venv
uv pip install -e ".[dev]"

# Lint
ruff check src/ tests/

# Unit tests
pytest -v

# Format
ruff format src/ tests/
```

### Integration Tests

Live tests against real HA Core + companion Docker containers. Requires Docker Desktop.

```bash
# Install integration deps
uv pip install -e ".[dev,integration]"

# Run integration tests (compose up → test → compose down)
make test-int
```

The integration suite:
- Spins up HA Core (stable) + companion on a shared Docker bridge network
- Performs headless HA onboarding (user creation + long-lived token)
- Tests all API endpoints against the live stack
- Tears down containers and volumes on completion

```bash
# Or manually:
docker compose -f docker-compose.integration.yaml up -d --build
pytest tests/integration -v --tb=short
docker compose -f docker-compose.integration.yaml down -v
```

## hactl Integration

For instructions on implementing companion support in the hactl Go CLI (downloading from GitHub, Docker test setup, Go client, end-to-end tests), see [HACTL_INTEGRATION.md](HACTL_INTEGRATION.md).

## Security

- Accessible only via HA Ingress (no exposed port)
- Bearer token auth (Supervisor token) or Ingress header bypass
- Path traversal prevention on all config endpoints
- `secrets.yaml` access is always denied
- Write operations default to dry-run
- Config validation + automatic rollback on failure
- No arbitrary command execution (CLI commands are whitelisted)

## License

MIT
