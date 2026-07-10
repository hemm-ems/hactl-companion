"""Shared authentication / ingress-trust helpers.

Kept in its own module (rather than in ``server.py``) so ``routes/status.py``
can reuse the exact same trust decision without importing ``server`` — which
would be a circular import, since ``server`` imports every route module.
"""

from __future__ import annotations

import hmac
import os

from aiohttp import web

# Source address of the HA Supervisor ingress proxy. Only requests that provably
# originate here may skip bearer auth on the strength of an ``X-Ingress-Path``
# header — the header itself is client-controlled and therefore spoofable.
# Overridable via ``INGRESS_PROXY_IPS`` (comma-separated) for dev/integration
# stacks that front the companion with a different proxy address.
DEFAULT_INGRESS_PROXY_IPS = "172.30.32.2"


def trusted_ingress_ips() -> set[str]:
    """Return the set of source IPs allowed to assert ingress authentication."""
    raw = os.environ.get("INGRESS_PROXY_IPS", DEFAULT_INGRESS_PROXY_IPS)
    return {ip.strip() for ip in raw.split(",") if ip.strip()}


def is_trusted_ingress(request: web.Request) -> bool:
    """True only when the request carries ``X-Ingress-Path`` *and* provably comes
    from the Supervisor ingress proxy.

    The header alone is not sufficient: it is set by clients, so without the
    source-IP check any peer that can reach the port would authenticate by
    setting a single header (e.g. other add-on containers on the internal
    hassio network).
    """
    if request.headers.get("X-Ingress-Path") is None:
        return False
    return request.remote in trusted_ingress_ips()


def bearer_token_valid(auth_header: str, expected_token: str) -> bool:
    """Constant-time check of an ``Authorization: Bearer <token>`` header.

    Returns ``False`` when no token is configured (``expected_token`` empty), so
    an unset ``SUPERVISOR_TOKEN`` can never be satisfied by an empty credential.
    """
    if not expected_token or not auth_header.startswith("Bearer "):
        return False
    provided = auth_header[len("Bearer ") :]
    return hmac.compare_digest(provided, expected_token)
