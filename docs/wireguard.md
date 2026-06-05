# WireGuard VPN Client — Feature Manual

## Overview

The companion add-on now includes a WireGuard VPN **client** that can be fully configured, started, stopped, and monitored via the REST API. No SSH, no UI — everything works through HTTP with a Bearer token.

This implements [issue #12](https://github.com/hemm-ems/hactl-companion/issues/12) (add-on side only; HA integration with entities/config flow is deferred).

> ## ✅ Production-ready
>
> WireGuard is the remote *lifeline* hactl rides over, so a tunnel must survive
> restarts and come back up on its own. Two earlier bugs that broke this — found
> live on a real HA OS install — are now fixed:
>
> 1. **Configs are persisted.** Tunnel `.conf` files are written under
>    `/data/wireguard/` (the add-on's persistent `/data` volume), **not** the
>    ephemeral `/etc/wireguard`. They now survive add-on restarts, updates, and
>    host reboots. (`WG_CONFIG_DIR` overrides the directory for tests/dev.)
>    Previously they lived in the ephemeral container layer and vanished on every
>    restart, leaving `wg-quick` with no config.
>
> 2. **Tunnels auto-start on boot.** `auto_enable=true` now records intent as a
>    persistent marker (`/data/wireguard/<tunnel>.autostart`) instead of calling
>    `systemctl` (the Alpine image has no systemd). On startup the companion runs
>    an `on_startup` hook (`restore_tunnels`) that brings up every marked tunnel —
>    so the Pi reconnects to the VPN automatically after a reboot.
>
> 3. **hactl drives it.** Manage the lifeline end-to-end with
>    `hactl companion wireguard {status,config,up,down}` — it handles the
>    Ingress session auth that a plain bearer-token `curl` (which gets `401`)
>    cannot.
>
> These three together mean: push a config + `up --auto` once, and the tunnel
> comes back on its own across restarts — usable as a real remote lifeline.

## API Endpoints

All endpoints require `Authorization: Bearer <TOKEN>` (same as other companion endpoints).

### `POST /v1/wireguard/config?tunnel=wg0`

Push a WireGuard tunnel config. Accepts **two formats**:

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

### `POST /v1/wireguard/start?tunnel=wg0&auto_enable=false`

Start (bring up) the tunnel. Optional `auto_enable=true` enables auto-start on boot via systemd (if available).

- Returns `200 {"status": "started", "tunnel": "wg0", "auto_enable": false}`
- Returns `409` if already running

### `POST /v1/wireguard/stop?tunnel=wg0&auto_disable=false`

Stop (bring down) the tunnel. Optional `auto_disable=true` disables auto-start.

- Returns `200 {"status": "stopped", "tunnel": "wg0"}`
- Returns `409` if not running

### `GET /v1/wireguard/status?tunnel=wg0`

Get tunnel status.

When **active**:
```json
{
  "tunnel": "wg0",
  "state": "active",
  "auto_enable": false,
  "interface": {
    "public_key": "...",
    "listening_port": 51820
  },
  "peers": [{
    "public_key": "...",
    "endpoint": "1.2.3.4:51820",
    "allowed_ips": "10.0.0.0/24",
    "latest_handshake": "42 seconds ago",
    "transfer_rx": "1.23 KiB received",
    "transfer_tx": "4.56 KiB sent"
  }]
}
```

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

## Auto-Enable on Boot

Pass `auto_enable=true` when starting:
```bash
curl -X POST "$HA/v1/wireguard/start?tunnel=wg0&auto_enable=true" -H "$AUTH"
```

This drops a persistent marker at `/data/wireguard/wg0.autostart`. On startup the
companion's `restore_tunnels` hook brings up every marked tunnel — so the tunnel
reconnects automatically after an add-on restart or host reboot. (No systemd
required; the marker lives on the persistent `/data` volume alongside the config.)
Clear it by stopping with `auto_disable=true`.

## Security

- **Auth required**: All WireGuard endpoints require Bearer token authentication
- **Tunnel name validation**: Only `[a-zA-Z0-9_]{1,15}` — prevents command injection and path traversal
- **Config file permissions**: Written with mode `0600` (owner-only read/write)
- **Private keys**: Never logged or exposed in API responses
- **NET_ADMIN capability**: Required in the add-on manifest for creating WG interfaces

## Testing

### Unit Tests
```bash
uv run pytest tests/test_wireguard.py -v
```
30 tests covering: validation, config parsing (raw + JSON), `wg show` output parsing, start/stop/status with mocked subprocess, auth enforcement, command injection prevention.

### Integration Tests
```bash
make test-wg
```
14 end-to-end tests using two Docker containers:
- **wg-server**: Alpine with WireGuard, generates keypairs, runs as VPN server on `10.13.13.1`
- **companion-wg**: Full companion image with WireGuard tools, acts as VPN client on `10.13.13.2`

Tests verify: config push (raw + JSON) → tunnel start → status active with peer handshake → actual ping through tunnel → stop → status inactive → ping fails → auth enforcement.

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

2. **No systemd on Alpine** (resolved): The `wg-quick@` systemd service doesn't exist on Alpine, so the original `systemctl enable` approach was always a no-op in the add-on. Auto-start is now driven by a persistent `.autostart` marker + the `restore_tunnels` `on_startup` hook, which needs no systemd and works identically everywhere.

3. **wireguard-go not needed**: Alpine's `wireguard-tools` package uses the kernel WireGuard module when available (Linux 5.6+). Docker Desktop and GitHub Actions both have it. `wireguard-go` (userspace) is not needed unless targeting older kernels.

4. **`/dev/net/tun` not needed in compose**: Docker Desktop on Windows automatically provides `/dev/net/tun` when `cap_add: NET_ADMIN` is set. No explicit `devices` mapping needed.

5. **PowerShell stderr noise**: Docker's progress output goes to stderr, causing PowerShell to report "errors" with `RemoteException`. The commands actually succeed — the exit codes in PowerShell are misleading. This is cosmetic only.

6. **wg-quick stderr is normal**: `wg-quick up` prints its shell trace to stderr (`[#] ip link add...`). This is informational, not an error.

7. **Test ordering**: The integration tests are numbered (`test_01_` onward) and run as methods of a single class to ensure execution order. Each test depends on the state left by the previous test (config → start → status → stop).

8. **The bug CI used to miss, and how it's now caught**: the original Docker
   suite pushed a config and started the tunnel **in the same container
   lifetime** — it never recreated `companion-wg`, so the ephemeral
   `/etc/wireguard/wg0.conf` was always present and neither the persistence bug
   nor the no-auto-start bug ever showed up. Both were only reproducible on a real
   add-on across a restart/reboot (verified live on HA OS 2026.5 / companion
   2026.5.12: a full config→start→active→stop cycle succeeded, but `start` after a
   restart failed with `'/etc/wireguard/wg0.conf' does not exist`). The fix moves
   configs to the persistent `/data` volume and auto-restores marked tunnels on
   startup; `test_15_config_and_autostart_survive_recreate` now force-recreates the
   container mid-suite and asserts the config persists **and** the tunnel
   auto-reconnects — so this can't regress unnoticed again.
