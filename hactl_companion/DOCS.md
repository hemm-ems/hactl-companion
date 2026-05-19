# hactl companion

Bridge add-on for the [hactl](https://github.com/hemm-ems/hactl) CLI. Gives hactl access to things that aren't possible through the standard HA REST/WebSocket API: reading and writing YAML config files, querying the Supervisor, tailing logs, and running reload/restart commands.

## Requirements

**HA OS or Supervised only.** This add-on requires the Supervisor. It will not work on HA Container or Core installs.

## Installation

1. Add the custom repository in **Settings → Add-ons → Add-on Store → ⋮ → Repositories**:
   ```
   https://github.com/hemm-ems/hactl-companion
   ```
2. Refresh the store — "hactl companion" will appear.
3. Install and start it. No configuration is required.

## Authentication

No secret is needed. hactl auto-discovers the companion URL via the Supervisor WebSocket and accesses it through HA Ingress, which handles authentication automatically.

## What it exposes

| Area | Capabilities |
|------|-------------|
| Config files | List, read, write YAML files with diff preview and automatic backup |
| Helpers | Full CRUD for input booleans, input numbers, counters, and other helper domains |
| Template sensors | Create, read, update, delete |
| Scripts & automations | Full CRUD |
| HA commands | `reload/{domain}`, `check-config` |
| Health | `GET /v1/health` — liveness check |

## Security

- Accessible only via HA Ingress — no port is exposed to the network.
- `secrets.yaml` is always denied regardless of request.
- Write operations default to dry-run and include automatic rollback on validation failure.
- CLI access is whitelisted; no arbitrary command execution.

## More information

See the [GitHub repository](https://github.com/hemm-ems/hactl-companion) and [releases](https://github.com/hemm-ems/hactl-companion/releases).
