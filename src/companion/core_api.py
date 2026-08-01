"""Thin client for the HA Core REST API via the Supervisor proxy.

Add-on containers reach HA core at http://supervisor/core/api using the
SUPERVISOR_TOKEN (requires homeassistant_api: true in the add-on manifest).
CORE_API_URL overrides the base URL for dev/integration stacks where no
Supervisor is present.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, NamedTuple
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_CORE_API_URL = "http://supervisor/core/api"
_TIMEOUT = aiohttp.ClientTimeout(total=30)

#: Untrusted text (HA's response body, an exception message) is bounded to this
#: many characters before it enters a log line or a response field.
_EXCERPT_CHARS = 200


class CoreAPIUnavailableError(Exception):
    """The HA core API could not be reached (transport failure, not a result)."""


class ServiceResult(NamedTuple):
    """Outcome of a core-API service call, plus *why* when it failed.

    A bare ``bool`` was the defect: this module knew ``HTTP 400: Service not
    found``, logged it into the add-on log, and returned ``False`` — so every
    caller could only say "HA did not confirm the reload", and hactl ended up
    printing a rhetorical question ("is `template: !include template.yaml` in
    configuration.yaml?") at an operator who had no way to see the real reason.

    The ``(ok, reason)`` pair is the shape :func:`check_config` already uses for
    the same job in this module, so the two read alike at the call site.
    """

    ok: bool
    error: str | None = None

    def __bool__(self) -> bool:
        """``ok`` — so a bare truth test cannot silently invert.

        A two-field tuple is truthy even when ``ok`` is ``False``: without this,
        the pre-existing ``if not await reload_domain(domain)`` in
        ``routes/ha.py`` would have started reporting every failed reload as a
        success, and no type checker or linter would have said a word. Callers
        should read ``.ok``; this makes the terse form correct as well.
        """
        return self.ok


def _excerpt(text: str) -> str:
    """Bound untrusted text so a reason can go on the wire without a surprise."""
    return text[:_EXCERPT_CHARS]


async def call_service(domain: str, service: str, data: dict[str, Any] | None = None) -> ServiceResult:
    """Call an HA service via the core API.

    On failure the reason is HA's own HTTP status plus a bounded excerpt of its
    response body, or the transport exception's class and message. It never
    carries a request header, so the ``SUPERVISOR_TOKEN`` cannot travel with it.
    """
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        logger.warning("SUPERVISOR_TOKEN not set; cannot call %s.%s", domain, service)
        return ServiceResult(False, "SUPERVISOR_TOKEN not set")

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
                reason = f"HTTP {resp.status}: {_excerpt(body)}"
                logger.error("%s.%s failed: %s", domain, service, reason)
                return ServiceResult(False, reason)
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        reason = f"{type(exc).__name__}: {_excerpt(str(exc))}"
        logger.error("%s.%s failed: %s", domain, service, reason)
        return ServiceResult(False, reason)

    logger.info("Called service %s.%s", domain, service)
    return ServiceResult(True)


async def reload_domain(domain: str) -> ServiceResult:
    """Reload an integration domain (``<domain>.reload``), and say why not."""
    return await call_service(domain, "reload")


def reload_fields(result: ServiceResult) -> dict[str, Any]:
    """The reload fields a write route puts on the wire: ``reloaded`` (+ reason).

    ``reloaded`` keeps exactly the meaning it always had. ``reload_error`` is
    **absent** on success — not empty, not null — so a successful response is
    byte-identical to the one this service sent before the field existed and no
    consumer can be confused by it (D45 is what that costs when it goes wrong).

    One helper rather than twelve inline literals: the routes that report a
    reload are twelve, and "present only on failure" is the kind of rule that
    gets re-derived correctly eleven times (#94).
    """
    if result.ok:
        return {"reloaded": True}
    return {"reloaded": False, "reload_error": result.error or "reload failed, no reason recorded"}


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
    url = f"{base}/states/{quote(entity_id, safe='')}"
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


def is_live_state(state: dict[str, Any] | None) -> bool:
    """True if ``state`` (a ``GET /states/<id>`` payload) is a genuinely live entity.

    Presence alone is not the answer: HA keeps a dropped entity in the entity
    registry and can serve it back as a *restored ghost* — ``state:
    unavailable`` with a ``restored: true`` attribute — until its platform sets
    it up for real, so a bare "the state exists" check reports "still there"
    for something HA has actually dropped. That is precisely the dishonesty
    this poll exists to catch (the same shape as ``reload_error`` answering
    "reloaded" while the entity never came up), so the same ghost is excluded
    here. Mirrors ``tests/integration/test_live.py::_entity_is_live``, which
    established this exact rule (and its two-sided reasoning: neither "absent"
    nor "ghost" counts as loaded) against real HA restarts.
    """
    if state is None:
        return False
    return not (state.get("state") == "unavailable" and state.get("attributes", {}).get("restored"))


async def poll_for_entity(entity_id: str, attempts: int = 5, delay: float = 0.4) -> bool:
    """Poll ``GET /states/<entity_id>`` until it appears live, or give up after ``attempts`` tries.

    A reload service call answering success only means HA accepted the YAML —
    HA validates each entry independently and silently drops one that fails
    schema validation (logs it, raises a persistent notification, moves on)
    without failing the reload, so ``reload_error`` cannot see this failure
    mode by construction. This is the check that can: it asks whether the
    entity the write just declared actually came up.

    Shared by every create route whose target's entity_id is knowable in
    advance (``helper``, ``script`` — both `<domain>.<id>`, the id being the
    exact key the write filed under). A route that cannot predict the id first
    (``template``, whose entity_id is derived from `name`, not `unique_id`)
    resolves it via the entity registry before polling — see
    :mod:`companion.registry` — but polls with this same loop shape and the
    same :func:`is_live_state` check.
    """
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(delay)
        if is_live_state(await get_state(entity_id)):
            return True
    return False


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
