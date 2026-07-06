## 2026.7.1

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.7.0...v2026.7.1

## 2026.7.0

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.6.10...v2026.7.0

## 2026.6.10

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.6.9...v2026.6.10

## 2026.6.9

<!-- Release notes generated using configuration in .github/release.yml at main -->


## New Contributors
* @swifty99 made their first contribution in https://github.com/hemm-ems/hactl-companion/pull/53

**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.6.8...v2026.6.9

## 2026.6.8

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.6.7...v2026.6.8

## 2026.6.7

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.6.6...v2026.6.7

## 2026.6.6

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.6.5...v2026.6.6

## 2026.6.5

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.6.4...v2026.6.5

## 2026.6.4

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.6.3...v2026.6.4

## 2026.6.3

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.6.2...v2026.6.3

## 2026.6.2

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.6.1...v2026.6.2

## 2026.6.1

<!-- Release notes generated using configuration in .github/release.yml at main -->

## What's Changed
### 🔧 Maintenance
* release: bump version to 2026.6.0 by @github-actions[bot] in https://github.com/hemm-ems/hactl-companion/pull/35

## New Contributors
* @github-actions[bot] made their first contribution in https://github.com/hemm-ems/hactl-companion/pull/35

**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.6.0...v2026.6.1

## 2026.6.0

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.5.17...v2026.6.0

## 2026.5.17

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.5.16...v2026.5.17

## 2026.5.16

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.5.15...v2026.5.16

## 2026.5.15

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.5.14...v2026.5.15

## 2026.5.14

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.5.13...v2026.5.14

## 2026.5.13

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.5.12...v2026.5.13

## 2026.5.13

### WireGuard VPN client — declarative configuration

The WireGuard tunnel can now be configured from the HA add-on **Configuration** tab. New options:

- `vpn.enabled` — master on/off switch. On (re)start the add-on reconciles the tunnel to match.
- `vpn.autostart` — also enable `wg-quick@<tunnel>` via systemd so the tunnel survives a host reboot.
- `vpn.tunnel` — interface name (default `wg0`).
- `vpn.config` — the full `wg.conf` text, pasted into the UI. As an alternative, drop a file at `/config/hactl/<tunnel>.conf` and leave `vpn.config` empty.

This is now the recommended path: the `hactl` CLI normally talks to HA *over* the VPN, so the add-on has to bring the tunnel up itself. The existing REST API (`/v1/wireguard/*`) is unchanged and still works for multi-tunnel or scripted setups.

## 2026.5.12

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.5.11...v2026.5.12

## 2026.5.11

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.5.10...v2026.5.11

## 2026.5.10

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.5.9...v2026.5.10

## 2026.5.6

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.5.5...v2026.5.6

## 2026.5.5

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.5.4...v2026.5.5

## 2026.5.4

### HTTP access log middleware
Every request is now logged with method, path, HTTP status, duration, and auth mode (`ingress`, `bearer`, or `none`). Auth failures log at WARNING level. Visible in the HA add-on log panel.

### Startup diagnostics
On startup the companion logs its version, config path, whether `SUPERVISOR_TOKEN` is present, and a one-line explanation of the auth model — making it much easier to diagnose issues from the HA add-on logs.

### `POST /v1/ha/check-config`
New endpoint that calls `ha core check` to validate HA configuration. Returns 502 with a clear message if the `ha` CLI is not available.

### HTTP path normalization
`GET ////` (and similar Ingress double-slash paths) are now normalized to `GET /` before routing, fixing the 404 seen when opening the add-on via the HA sidebar.

### Add-on store improvements
Added DOCS.md (install/auth/API reference), improved user-facing description, and release version-bump PRs now auto-merge after CI passes.

## 2026.5.3

- CI workflow alignment with hactl patterns
- Dependency updates: ruff, mypy ≥2.1, pytest ≥9, pytest-asyncio, aiohttp ≥3.13.5, openapi-spec-validator ≥0.8.5, setuptools ≥82

## 2026.5.2

- Dependency updates

## 2026.5.1

Initial release on hemm-ems org (migrated from swifty99/hactl_companion). Config file access, Supervisor API bridge, HA CLI commands.
