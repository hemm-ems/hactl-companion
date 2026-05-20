## 2026.5.7

<!-- Release notes generated using configuration in .github/release.yml at main -->



**Full Changelog**: https://github.com/hemm-ems/hactl-companion/compare/v2026.5.6...v2026.5.7

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
