# hactl-companion

Home Assistant app that exposes HA-internal features for the [hactl](https://github.com/hemm-ems/hactl) CLI.

## What it does

The standard HA REST/WebSocket API doesn't give you everything — no direct config file access, no Supervisor queries, no log tailing, no reload triggers. hactl-companion fills that gap. It runs as an aiohttp server inside an HA app, accessible only via Ingress, and gives hactl a bridge to the things that normally require SSH or shell access: reading and writing YAML config with diff previews and automatic backups, querying the Supervisor API for system info and app state, reading Core/Supervisor/app logs, and running whitelisted HA CLI commands like reloads and restarts.

> **Requires HA OS or Supervised.** The app won't work on HA Container or Core installs — those don't have the Supervisor or the app infrastructure.

## Install

Add this custom repository in **Settings → Add-ons → Add-on Store → ⋮ → Repositories**:

```
https://github.com/hemm-ems/hactl-companion
```

After refreshing, "hactl companion" will appear in the store. Install it, then start it. No configuration needed.

> **Note:** This is a custom repository, not part of the default HA add-on catalogue. The "My Home Assistant" one-click badge below only works if `my.home-assistant.io` is configured for your HA instance — the manual method above always works.
>
> [![Add repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fhemm-ems%2Fhactl-companion)

**Auth**: no secret is needed. hactl auto-discovers the companion URL via the Supervisor WebSocket and accesses it through HA Ingress, which handles authentication automatically. `COMPANION_URL` in `.env` is only needed if you want to bypass Ingress with a direct connection.

Supported architectures: `amd64`, `aarch64`.

## Architecture

```
HA OS / Supervised
├── HA Core (REST/WS API)
├── hactl-companion App (aiohttp, Ingress only, port 9100)
│   ├── /config (bind mount, read/write)
│   ├── Supervisor API (http://supervisor)
│   └── ha CLI (subprocess)
└── hactl (Go CLI, external) → HA Ingress → companion
```

## API

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
| GET | `/v1/config/templates` | List template sensor definitions |
| GET | `/v1/config/template?id=...` | Get a template definition |
| PUT | `/v1/config/template?id=...&dry_run=true` | Update a template (dry-run or apply) |
| POST | `/v1/config/template` | Create a template sensor |
| DELETE | `/v1/config/template?id=...` | Delete a template |
| GET | `/v1/config/scripts` | List script definitions |
| GET | `/v1/config/script?id=...` | Get a script definition |
| PUT | `/v1/config/script?id=...&dry_run=true` | Update a script |
| POST | `/v1/config/script` | Create a script |
| DELETE | `/v1/config/script?id=...` | Delete a script |
| GET | `/v1/config/automations` | List automation definitions |
| GET | `/v1/config/automation?id=...` | Get an automation definition |
| PUT | `/v1/config/automation?id=...&dry_run=true` | Update an automation |
| POST | `/v1/config/automation` | Create an automation |
| DELETE | `/v1/config/automation?id=...` | Delete an automation |
| POST | `/v1/ha/reload/{domain}` | Reload an HA integration domain |
| POST | `/v1/ha/check-config` | Validate HA configuration |

## Security

The app is only reachable via HA Ingress — no port is exposed to the network. Auth is handled via the Supervisor token or the Ingress session header. All config endpoints prevent path traversal, and `secrets.yaml` is always denied regardless of the request. Write operations default to dry-run and include automatic rollback on validation failure. CLI access is whitelisted; there's no arbitrary command execution.

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

### Integration tests

The integration suite is unusually thorough for an app — it spins up real HA Core (stable) and the companion in Docker, performs a headless HA onboarding (user creation, token generation), tests every API endpoint against the live stack, and tears everything down on completion. It requires Docker Desktop.

```bash
uv pip install -e ".[dev,integration]"

# Compose up → test → compose down
make test-int
```

```bash
# Or manually:
docker compose -f docker-compose.integration.yaml up -d --build
pytest tests/integration -v --tb=short
docker compose -f docker-compose.integration.yaml down -v
```

## hactl integration

For instructions on implementing companion support in the hactl Go CLI (downloading from GitHub, Docker test setup, Go client, end-to-end tests), see [HACTL_INTEGRATION.md](HACTL_INTEGRATION.md).

## License

MIT