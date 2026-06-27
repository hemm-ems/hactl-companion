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
| WireGuard VPN | Declarative tunnel config (HA UI) + REST start/stop/status |

## VPN client (optional)

The add-on can manage a WireGuard tunnel for you. The tunnel config is stored in a
single **source of truth** — the file `/config/hactl/<tunnel>.conf` — which both the
add-on and the `hactl` CLI read and write, so the two never drift apart. `/etc/wireguard`
is regenerated from it on every start, so the tunnel survives restarts/reboots.

**Recommended — provide the config as a file (no YAML escaping):**

- `hactl companion wireguard config -f wg0.conf` (from the LAN), **or**
- drop the file at `/config/hactl/<tunnel>.conf` via the File Editor / Samba add-on.

Then set the toggles in the **Configuration** tab and restart the add-on:

| Option | Default | Meaning |
|---|---|---|
| `vpn.enabled` | `false` | Bring the tunnel up on every add-on (re)start when `true` (so it returns after a host reboot once the add-on starts); bring it down and keep it down when `false`. |
| `vpn.tunnel` | `wg0` | Interface name (`^[a-zA-Z0-9_]{1,15}$`). |
| `vpn.config` | `""` | *Optional* inline config. Leave empty to use the file above. When set, it is written into `/config/hactl/<tunnel>.conf` on start (it wins over an existing file). |

> ℹ️ **Pasting into `vpn.config`:** the default form field is single-line, so a pasted
> multi-line `wg.conf` loses its line breaks — the add-on **normalizes** the collapsed
> value back into a valid config on startup, so it still works. If you instead use the
> **Edit in YAML** mode, a multi-line value must be a block scalar (`config: |` with each
> line indented); raw paste there is invalid YAML and HA rejects it before the add-on runs.
> The file / `hactl config -f` methods above avoid both pitfalls.

Restart the add-on after changing the config. See `docs/wireguard.md` for the full feature manual, including the REST API for multi-tunnel or scripted setups.

## Security

- Accessible only via HA Ingress — no port is exposed to the network.
- `secrets.yaml` is always denied regardless of request.
- Write operations default to dry-run and include automatic rollback on validation failure.
- CLI access is whitelisted; no arbitrary command execution.

## More information

See the [GitHub repository](https://github.com/hemm-ems/hactl-companion) and [releases](https://github.com/hemm-ems/hactl-companion/releases).
