"""POST /v1/ha/reload/{domain} — reload an HA integration via ha CLI."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from aiohttp import web

logger = logging.getLogger(__name__)

ALLOWED_DOMAINS: set[str] = {
    "automation",
    "counter",
    "group",
    "input_boolean",
    "input_button",
    "input_datetime",
    "input_number",
    "input_select",
    "input_text",
    "mqtt",
    "rest",
    "scene",
    "schedule",
    "script",
    "shell_command",
    "template",
    "timer",
    "zone",
}

_DOMAIN_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


async def post_reload(request: web.Request) -> web.Response:
    """POST /v1/ha/reload/{domain} — reload an integration domain."""
    domain = request.match_info["domain"]

    if not _DOMAIN_RE.fullmatch(domain) or domain not in ALLOWED_DOMAINS:
        raise web.HTTPBadRequest(text=f"Domain not allowed: {domain}")

    try:
        proc = await asyncio.create_subprocess_exec(
            "ha",
            "core",
            "reload",
            domain,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except FileNotFoundError as exc:
        raise web.HTTPBadGateway(text="ha CLI not available") from exc
    except TimeoutError as exc:
        raise web.HTTPGatewayTimeout(text="Reload timed out") from exc

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        logger.error("ha core reload %s failed (rc=%s): %s", domain, proc.returncode, err)
        raise web.HTTPBadGateway(text=f"Reload failed: {err}")

    logger.info("Reloaded domain: %s", domain)
    return web.json_response({"status": "ok", "domain": domain})


routes: list[RouteDef] = [
    RouteDef("POST", "/v1/ha/reload/{domain}", post_reload),
]
