# Security Policy

## Supported Versions

Only the latest release is supported with security updates.

| Version | Supported          |
| ------- | ------------------ |
| latest  | :white_check_mark: |
| < latest | :x:               |

## Reporting a Vulnerability

Please report security vulnerabilities through GitHub's
[Private Vulnerability Reporting](https://github.com/hemm-ems/hactl-companion/security/advisories/new).

**Do not open a public issue for security vulnerabilities.**

You can expect an initial response within 72 hours. We will work with you to
understand the issue and coordinate a fix before any public disclosure.

## Trust model

`hactl-companion` exposes a small HTTP API on port `9100`. Every request to a
path other than the auth-exempt `/v1/health` and `/v1/status` is gated by
`companion.auth` / `server.auth_middleware`.

### Authentication

A request is accepted only if **one** of the following holds:

1. **Trusted ingress.** The request carries an `X-Ingress-Path` header *and* its
   source address (`request.remote`) is one of the trusted ingress-proxy IPs.
   In production that is the HA Supervisor ingress proxy, `172.30.32.2`.
2. **Bearer token.** `Authorization: Bearer <token>` matches `SUPERVISOR_TOKEN`,
   compared in constant time (`hmac.compare_digest`).

`X-Ingress-Path` is set by clients and cannot, on its own, prove a request came
through the Supervisor ingress proxy. Because the server binds `0.0.0.0,[::]`,
anything that can reach port `9100` — other add-on containers on the internal
hassio network, or peers on a dev/WireGuard stack — could otherwise
authenticate by sending a single header. Requiring the request to originate from
a known proxy address closes that hole; the header alone is never sufficient.

### Fail-closed when unconfigured

If `SUPERVISOR_TOKEN` is unset there is nothing to authenticate against, so
bearer auth **fails closed**: every bearer request (including an empty
credential, `Authorization: Bearer `) is rejected with `503 Service
Unavailable`. An unset token is a misconfiguration, never an open door.

### Configuration

| Env var             | Default       | Meaning                                                                                                                                    |
| ------------------- | ------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `SUPERVISOR_TOKEN`  | *(unset)*     | Bearer token expected from direct (non-ingress) callers.                                                                                  |
| `INGRESS_PROXY_IPS` | `172.30.32.2` | Comma-separated source IPs allowed to assert ingress auth. Override for dev/integration stacks that front the companion with a different proxy address. |

### Path containment

All config file access is confined to the configured base directory (`/config`
in production). Paths are validated with `Path.is_relative_to`, which rejects
both `../` traversal and sibling directories that merely share the base as a
string prefix (e.g. `/config2`). `secrets.yaml` is denied outright.
