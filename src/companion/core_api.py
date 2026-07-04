"""Thin client for the HA Core REST API via the Supervisor proxy.

Add-on containers reach HA core at http://supervisor/core/api using the
SUPERVISOR_TOKEN (requires homeassistant_api: true in the add-on manifest).
CORE_API_URL overrides the base URL for dev/integration stacks where no
Supervisor is present.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_CORE_API_URL = "http://supervisor/core/api"
_TIMEOUT = aiohttp.ClientTimeout(total=30)


class CoreAPIUnavailableError(Exception):
    """The HA core API could not be reached (transport failure, not a result)."""


async def call_service(domain: str, service: str, data: dict[str, Any] | None = None) -> bool:
    """Call an HA service via the core API. Returns True on success."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        logger.warning("SUPERVISOR_TOKEN not set; cannot call %s.%s", domain, service)
        return False

    base = os.environ.get("CORE_API_URL", DEFAULT_CORE_API_URL).rstrip("/")
    url = f"{base}/services/{domain}/{service}"
    try:
        async with (
            aiohttp.ClientSession(timeout=_TIMEOUT) as session,
            session.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=data or {},
            ) as resp,
        ):
            if resp.status >= 400:
                body = await resp.text()
                logger.error("%s.%s failed: %d %s", domain, service, resp.status, body[:200])
                return False
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        logger.error("%s.%s failed: %s", domain, service, exc)
        return False

    logger.info("Called service %s.%s", domain, service)
    return True


async def reload_domain(domain: str) -> bool:
    """Reload an integration domain (<domain>.reload). Returns True on success."""
    return await call_service(domain, "reload")


async def get_state(entity_id: str) -> dict[str, Any] | None:
    """Fetch a single entity's state via GET /states/<entity_id>.

    Returns None on any transport failure or if the entity doesn't exist
    (best-effort — callers treat this as "could not confirm", not fatal).
    """
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        logger.warning("SUPERVISOR_TOKEN not set; cannot fetch state for %s", entity_id)
        return None

    base = os.environ.get("CORE_API_URL", DEFAULT_CORE_API_URL).rstrip("/")
    url = f"{base}/states/{entity_id}"
    try:
        async with (
            aiohttp.ClientSession(timeout=_TIMEOUT) as session,
            session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp,
        ):
            if resp.status >= 400:
                return None
            return await resp.json()  # type: ignore[no-any-return]
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        logger.error("get_state(%s) failed: %s", entity_id, exc)
        return None


async def get_states() -> list[dict[str, Any]] | None:
    """Fetch all entity states via GET /states.

    Returns None on any transport failure (best-effort).
    """
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        logger.warning("SUPERVISOR_TOKEN not set; cannot fetch states")
        return None

    base = os.environ.get("CORE_API_URL", DEFAULT_CORE_API_URL).rstrip("/")
    url = f"{base}/states"
    try:
        async with (
            aiohttp.ClientSession(timeout=_TIMEOUT) as session,
            session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp,
        ):
            if resp.status >= 400:
                return None
            return await resp.json()  # type: ignore[no-any-return]
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        logger.error("get_states() failed: %s", exc)
        return None


async def check_config() -> tuple[bool, str]:
    """Validate HA core configuration via POST /config/core/check_config.

    Returns (valid, errors) when the check completed. Raises
    CoreAPIUnavailableError when the core API itself could not be reached,
    so callers can distinguish "config is invalid" from "could not check".
    """
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise CoreAPIUnavailableError("SUPERVISOR_TOKEN not set")

    base = os.environ.get("CORE_API_URL", DEFAULT_CORE_API_URL).rstrip("/")
    url = f"{base}/config/core/check_config"
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session,
            session.post(url, headers={"Authorization": f"Bearer {token}"}) as resp,
        ):
            if resp.status >= 400:
                body = await resp.text()
                raise CoreAPIUnavailableError(f"HTTP {resp.status}: {body[:200]}")
            result = await resp.json()
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        raise CoreAPIUnavailableError(str(exc)) from exc

    if result.get("result") == "valid":
        return True, ""
    return False, str(result.get("errors", "unknown error"))
