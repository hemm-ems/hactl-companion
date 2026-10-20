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
| `vpn.forward_ha` | `true` | Make Home Assistant reachable through the tunnel at `<tunnel-address>:<HA port>` (usually `http://10.x.y.z:8123`). This is what lets `hactl` on the other end of the tunnel talk to HA. |

> ℹ️ **Pasting into `vpn.config`:** just paste what you were given into the field — the
> bare WireGuard config, or the whole `vpn:` block including `enabled:` and `config: |`.
> The field is single-line and eats the line breaks; the add-on rebuilds the config on
> startup and ignores the YAML around it. If something in it is not WireGuard, the add-on
> log names the line (never the key) and the tunnel stays down. In **Edit in YAML** mode a
> multi-line value must be a block scalar (`config: |` with each line indented); raw paste
> there is invalid YAML and HA rejects it before the add-on runs.

After a restart the log shows where HA can be reached, e.g.
`HA reachable through tunnel wg0 at http://10.6.0.4:8123`. Use that address as `HA_URL`.

> ⚠️ HA sees tunnel clients as this add-on's address (`172.30.33.x`). If your
> `http: trusted_proxies` covers that subnet (common with a reverse-proxy add-on) **and**
> you use the `trusted_networks` auth provider, a tunnel client could claim a trusted
> address via `X-Forwarded-For`. In that setup, set `vpn.forward_ha: false`.

Restart the add-on after changing the config. See `docs/wireguard.md` for the full feature manual, including the REST API for multi-tunnel or scripted setups.

## Security

- Accessible only via HA Ingress — no port is exposed to the network.
- `secrets.yaml` is always denied regardless of request.
- Write operations default to dry-run and include automatic rollback on validation failure.
- CLI access is whitelisted; no arbitrary command execution.

## More information

See the [GitHub repository](https://github.com/hemm-ems/hactl-companion) and [releases](https://github.com/hemm-ems/hactl-companion/releases).
