# WireGuard VPN Client — Feature Manual

## Overview

The companion add-on includes a WireGuard VPN **client**. It can be driven two ways:

1. **Declaratively** from the HA add-on **Configuration** tab (recommended for end users).
2. **Programmatically** via REST (for multi-tunnel setups or scripting from inside the LAN).

The declarative path exists because `hactl` normally talks to HA *over* the VPN — it cannot be the thing that brings the tunnel up. The add-on supervises itself.

## Source of truth: `/config/hactl/<tunnel>.conf`

The tunnel config lives in **one** persistent place — `/config/hactl/<tunnel>.conf` (in the
mapped HA `/config` volume). Both the add-on's startup supervisor **and** the `hactl` CLI
(`POST /v1/wireguard/config`) read and write this file, so they never drift apart.
`/etc/wireguard/<tunnel>.conf` is *ephemeral* (wiped on every restart) and is regenerated
from the canonical file on demand — so it's never the source of truth.

### Providing the config (recommended — no YAML escaping)

Either:

- **From the LAN via hactl:** `hactl companion wireguard config -f wg0.conf` — pushes the
  raw `.conf`; the add-on stores it canonically. Then `hactl companion wireguard up`, or set
  `vpn.enabled: true` to have the add-on bring it up on start.
- **Drop a file:** write `/config/hactl/<tunnel>.conf` via the File Editor / Samba add-on.

### Toggles (HA UI → Configuration)

| Field | Meaning |
|---|---|
| `enabled` | Master switch, persisted across restarts. `true` ⇒ tunnel is brought up on every add-on (re)start (including after a host reboot, since the add-on restarts then). `false` ⇒ tunnel is brought down and kept down. |
| `tunnel` | Interface name. Must match `^[a-zA-Z0-9_]{1,15}$`. Default `wg0`. |
| `config` | *Optional* inline config. Leave empty to use the canonical file. When non-empty it is written into `/config/hactl/<tunnel>.conf` on start (takes precedence over an existing file). |

Save and restart the add-on. On startup the supervisor resolves the config (`vpn.config` if
set, else the file), writes it canonically + into `/etc/wireguard/<tunnel>.conf` (mode `0600`),
and runs `wg-quick up <tunnel>` when `enabled`.

> **Pasting `vpn.config` — two cases:**
>
> - **Form field (default UI):** the `config` box is single-line, so a pasted multi-line
>   `wg.conf` arrives with its **line breaks stripped** (`[Interface]PrivateKey=…Address=…`).
>   The add-on **normalizes** this back into a valid config on startup (the keys are a known
>   vocabulary), so a mangled paste now works.
> - **YAML editor mode:** a multi-line value must still be a block scalar — paste under
>   `config: |` with every line indented:
>   ```yaml
>   vpn:
>     enabled: true
>     config: |
>       [Interface]
>       PrivateKey = <client-private-key>
>       Address = 10.13.13.2/24
>       [Peer]
>       PublicKey = <server-public-key>
>       Endpoint = vpn.example.com:51820
>       AllowedIPs = 0.0.0.0/0
>       PersistentKeepalive = 25
>   ```
>   Pasting raw without `config: |` is invalid YAML; HA rejects it with a syntax error *before*
>   the add-on runs (so the normalizer can't help there). The file / `hactl config -f` methods
>   above avoid all of this.

### Verifying

After the restart, check the add-on log — you should see one of:

```
vpn.enabled=true; bringing tunnel wg0 up
WireGuard tunnel wg0 started
```

or for the disabled path:

```
vpn.enabled=false; bringing tunnel wg0 down
```

You can also query `GET /v1/wireguard/status?tunnel=wg0` (see below) for live state.

## API Endpoints

All endpoints require `Authorization: Bearer <TOKEN>` (same as other companion endpoints).

### `POST /v1/wireguard/config?tunnel=wg0`

Push a WireGuard tunnel config. It's written to the canonical `/config/hactl/<tunnel>.conf`
(persists across restarts) and mirrored into `/etc/wireguard`. Accepts **two formats**:

**Raw `.conf`** (`Content-Type: text/plain`):
```
[Interface]
PrivateKey = <client-private-key>
Address = 10.13.13.2/24

[Peer]
PublicKey = <server-public-key>
Endpoint = vpn.example.com:51820
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
```

**Structured JSON** (`Content-Type: application/json`):
```json
{
  "tunnel_name": "wg0",
  "interface": {
    "private_key": "<client-private-key>",
    "address": "10.13.13.2/24",
    "dns": "1.1.1.1"
  },
  "peers": [{
    "public_key": "<server-public-key>",
    "endpoint": "vpn.example.com:51820",
    "allowed_ips": "0.0.0.0/0",
    "persistent_keepalive": 25
  }]
}
```

Response: `{"status": "configured", "tunnel": "wg0"}`

### `POST /v1/wireguard/start?tunnel=wg0`

Start (bring up) the tunnel.

- Returns `200 {"status": "started", "tunnel": "wg0"}`
- Returns `409` if already running

### `POST /v1/wireguard/stop?tunnel=wg0`

Stop (bring down) the tunnel.

- Returns `200 {"status": "stopped", "tunnel": "wg0"}`
- Returns `409` if not running

### `GET /v1/wireguard/status?tunnel=wg0`

Get tunnel status.

When **active**:
```json
{
  "tunnel": "wg0",
  "state": "active",
  "interface": {
    "public_key": "...",
    "listening_port": 51820
  },
  "peers": [{
    "public_key": "...",
    "endpoint": "1.2.3.4:51820",
    "allowed_ips": "10.0.0.0/24",
    "latest_handshake": "42s",
    "latest_handshake_secs": 42,
    "transfer_rx": "1.23 KiB",
    "transfer_tx": "4.56 KiB",
    "transfer_rx_bytes": 1260,
    "transfer_tx_bytes": 4669
  }],
  "monitor": {
    "running": true,
    "hostnames": ["pi.example.dyndns.org"],
    "healthy": true,
    "resolved": {"pi.example.dyndns.org": "87.123.51.187"},
    "last_check_secs_ago": 12,
    "last_reresolve_secs_ago": 305,
    "attempt": 0,
    "next_retry_secs": null,
    "last_error": null
  }
}
```

Status fields are derived from `wg show <tunnel> dump`, so `transfer_rx`/`transfer_tx`
and the numeric `latest_handshake_secs` are always populated. The `monitor` block
reflects the live dyndns re-resolution monitor (`{"running": false}` when no
hostname-endpoint peer is being watched). When `healthy` is `false` the monitor is
in its backoff loop: `attempt` and `next_retry_secs` show progress and `last_error`
the most recent failure.

When **inactive**: `{"tunnel": "wg0", "state": "inactive"}`

## Quick Start (curl)

```bash
HA=http://homeassistant.local:9100
TOKEN=<your-token>
AUTH="Authorization: Bearer $TOKEN"

# 1. Push config
curl -X POST "$HA/v1/wireguard/config?tunnel=wg0" \
  -H "$AUTH" -H "Content-Type: text/plain" \
  --data-binary @wg-client.conf

# 2. Start tunnel
curl -X POST "$HA/v1/wireguard/start?tunnel=wg0" -H "$AUTH"

# 3. Check status
curl "$HA/v1/wireguard/status?tunnel=wg0" -H "$AUTH"

# 4. Stop tunnel
curl -X POST "$HA/v1/wireguard/stop?tunnel=wg0" -H "$AUTH"
```

## Surviving a reboot

There's no separate auto-start switch. Set `vpn.enabled: true` in the add-on
Configuration tab: the add-on reconciles the tunnel up on every (re)start, so after a
host reboot the tunnel comes back as soon as the add-on starts. Transient
`POST /v1/wireguard/start|stop` calls (and `hactl companion wireguard up|down`) only
affect the live interface — they do not change the persisted `enabled` option.

## Dynamic DNS Endpoints

WireGuard resolves hostnames only once — at tunnel startup — and never re-resolves them. If your VPN server's IP changes (common with dynamic DNS / dyndns providers), the tunnel goes silent without any error.

The companion add-on handles this automatically in two ways:

**1. Pre-flight DNS check**

Before running `wg-quick up`, the add-on verifies that every hostname `Endpoint` in the config resolves. If any hostname fails DNS lookup, the tunnel is **not started** and the API returns `HTTP 502` with the list of failing hostnames:

```
HTTP 502 Bad Gateway
DNS resolution failed for endpoint(s): vpn.example.com
```

This catches typos and DNS outages at startup rather than letting the tunnel come up silently broken.

**2. Live re-resolution monitor**

After a successful start, the add-on runs a background monitor for every peer whose `Endpoint` is a hostname. Every **30 seconds** it checks whether the peer's latest WireGuard handshake is recent. If a handshake is older than **180 seconds**, the add-on re-resolves the hostname and pushes the updated IP via `wg set peer … endpoint <new-ip>:<port>`. Re-resolve attempts follow a backoff schedule (5 s → 10 s → 30 s → 60 s → … up to 1 h) and never give up.

**What this means for operators:**

- A tunnel with a bad hostname in `Endpoint` will be rejected with HTTP 502 instead of starting silently broken.
- After a dyndns IP change, the tunnel self-heals within ~35 seconds (30 s health check + 5 s first retry). No add-on restart needed.
- Peers whose `Endpoint` is a bare IP address are unaffected — the monitor only tracks hostname-based endpoints.

## Security

- **Auth required**: All WireGuard endpoints require Bearer token authentication
- **Tunnel name validation**: Only `[a-zA-Z0-9_]{1,15}` — prevents command injection and path traversal
- **Config file permissions**: Written with mode `0600` (owner-only read/write)
- **Private keys**: Never logged or exposed in API responses
- **NET_ADMIN capability**: Required in the add-on manifest for creating WG interfaces

## Testing

### Unit Tests
```bash
uv run pytest tests/test_wireguard.py tests/test_wg_dns.py tests/test_wg_monitor.py -v
```
226 tests covering: validation, config parsing (raw + JSON), `wg show` output parsing, start/stop/status with mocked subprocess, auth enforcement, command injection prevention, DNS pre-flight check, hostname peer parsing, monitor liveness/backoff/reconnect logic.

### Integration Tests
```bash
make test-wg
```
16 end-to-end tests using two Docker containers:
- **wg-server**: Alpine with WireGuard, generates keypairs, runs as VPN server on `10.13.13.1`
- **companion-wg**: Full companion image with WireGuard tools, acts as VPN client on `10.13.13.2`

Tests verify: config push (raw + JSON) → tunnel start → status active with peer handshake → actual ping through tunnel → stop → status inactive → ping fails → auth enforcement → declarative reconcile across container recreate → DNS pre-flight 502 for unresolvable hostname → monitor re-resolve after dyndns IP change (~42 s wait).

Works on **Windows** (Docker Desktop) and **GitHub Actions** (Ubuntu, `ubuntu-latest`).

## Architecture

```
┌─────────────────┐         ┌─────────────────┐
│   hactl CLI     │ HTTP    │   companion     │
│   or scripts    │────────▶│   add-on        │
│                 │ Bearer  │                 │
└─────────────────┘ token   │  /v1/wireguard  │
                            │  /config        │
                            │  /start → wg-quick up
                            │  /stop  → wg-quick down
                            │  /status→ wg show
                            └────────┬────────┘
                                     │ wg0 interface
                                     ▼
                            ┌─────────────────┐
                            │  VPN Server     │
                            │  (remote)       │
                            └─────────────────┘
```

## Discoveries & Notes

1. **Alpine TLS cert issue**: `python:3.12-alpine` has a known issue where `apk` fails with "TLS: server certificate not trusted" behind corporate proxies or with certain Docker Desktop versions. Fixed by switching repos to HTTP (`sed -i 's|https://|http://|g' /etc/apk/repositories`). This is a build-time workaround — the actual WireGuard traffic uses its own encryption.

2. **wireguard-go not needed**: Alpine's `wireguard-tools` package uses the kernel WireGuard module when available (Linux 5.6+). Docker Desktop and GitHub Actions both have it. `wireguard-go` (userspace) is not needed unless targeting older kernels.

3. **`/dev/net/tun` not needed in compose**: Docker Desktop on Windows automatically provides `/dev/net/tun` when `cap_add: NET_ADMIN` is set. No explicit `devices` mapping needed.

4. **PowerShell stderr noise**: Docker's progress output goes to stderr, causing PowerShell to report "errors" with `RemoteException`. The commands actually succeed — the exit codes in PowerShell are misleading. This is cosmetic only.

5. **wg-quick stderr is normal**: `wg-quick up` prints its shell trace to stderr (`[#] ip link add...`). This is informational, not an error.

6. **Test ordering**: The integration tests are numbered (`test_01_` through `test_14_`) and run as methods of a single class to ensure execution order. Each test depends on the state left by the previous test (config → start → status → stop).
