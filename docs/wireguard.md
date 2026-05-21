# WireGuard VPN Client — Feature Manual

## Overview

The companion add-on includes a WireGuard VPN **client**. It can be driven two ways:

1. **Declaratively** from the HA add-on **Configuration** tab (recommended for end users).
2. **Programmatically** via REST (for multi-tunnel setups or scripting from inside the LAN).

The declarative path exists because `hactl` normally talks to HA *over* the VPN — it cannot be the thing that brings the tunnel up. The add-on supervises itself.

## Declarative configuration (recommended)

In the HA UI, open **Settings → Add-ons → hactl companion → Configuration** and fill in the `vpn` block:

```yaml
vpn:
  enabled: true
  autostart: true
  tunnel: wg0
  config: |
    [Interface]
    PrivateKey = <client-private-key>
    Address = 10.13.13.2/24

    [Peer]
    PublicKey = <server-public-key>
    Endpoint = vpn.example.com:51820
    AllowedIPs = 0.0.0.0/0
    PersistentKeepalive = 25
```

Save and restart the add-on. The supervisor reads `/data/options.json` at startup, writes the config to `/etc/wireguard/<tunnel>.conf` (mode `0600`), and runs `wg-quick up <tunnel>`.

### Option semantics

| Field | Meaning |
|---|---|
| `enabled` | Master switch. `false` ⇒ tunnel is brought down (and kept down) on add-on (re)start. `true` ⇒ tunnel is brought up. |
| `autostart` | After `wg-quick up`, also runs `systemctl enable wg-quick@<tunnel>` so the tunnel survives a host reboot before the add-on starts (HA OS only — silently no-op where systemd is absent). |
| `tunnel` | Interface name. Must match `^[a-zA-Z0-9_]{1,15}$`. Default `wg0`. |
| `config` | The full `wg.conf` text. Leave empty to fall back to a file (see below). |

### File fallback

If you'd rather not paste the config into the UI, drop the file at:

```
/config/hactl/<tunnel>.conf
```

(reachable from the HA File Editor add-on, Samba share, or any other tool that can write into `/config`). Leave `vpn.config` empty in the add-on Configuration tab. On the next add-on start the supervisor will copy that file into `/etc/wireguard/` and bring the tunnel up.

`vpn.config` takes precedence if both are present.

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

This uses `systemctl enable wg-quick@wg0` under the hood. Works on systems with systemd (HA OS). On Alpine/Docker (no systemd), auto-enable is silently ignored.

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

2. **No systemd on Alpine**: The `wg-quick@` systemd service doesn't exist on Alpine. The auto-enable feature gracefully falls back to no-op. On HA OS (Debian-based), systemd is available and auto-start works.

3. **wireguard-go not needed**: Alpine's `wireguard-tools` package uses the kernel WireGuard module when available (Linux 5.6+). Docker Desktop and GitHub Actions both have it. `wireguard-go` (userspace) is not needed unless targeting older kernels.

4. **`/dev/net/tun` not needed in compose**: Docker Desktop on Windows automatically provides `/dev/net/tun` when `cap_add: NET_ADMIN` is set. No explicit `devices` mapping needed.

5. **PowerShell stderr noise**: Docker's progress output goes to stderr, causing PowerShell to report "errors" with `RemoteException`. The commands actually succeed — the exit codes in PowerShell are misleading. This is cosmetic only.

6. **wg-quick stderr is normal**: `wg-quick up` prints its shell trace to stderr (`[#] ip link add...`). This is informational, not an error.

7. **Test ordering**: The integration tests are numbered (`test_01_` through `test_14_`) and run as methods of a single class to ensure execution order. Each test depends on the state left by the previous test (config → start → status → stop).
