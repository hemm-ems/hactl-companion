"""POST /v1/ha/reload/{domain} — reload an HA integration via the core API."""

from __future__ import annotations

import re
from dataclasses import dataclass

from aiohttp import web

from companion import core_api

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

    if not await core_api.reload_domain(domain):
        raise web.HTTPBadGateway(text=f"Reload failed: {domain}")

    return web.json_response({"status": "ok", "domain": domain})


async def post_check_config(request: web.Request) -> web.Response:
    """POST /v1/ha/check-config — validate HA configuration via the core API."""
    valid, errors = await core_api.check_config()
    if not valid:
        raise web.HTTPBadGateway(text=f"Config check failed: {errors}")

    return web.json_response({"status": "ok"})


routes: list[RouteDef] = [
    RouteDef("POST", "/v1/ha/reload/{domain}", post_reload),
    RouteDef("POST", "/v1/ha/check-config", post_check_config),
]
