"""GET /v1/status — companion capability report."""

from __future__ import annotations

import os
import shutil

from aiohttp import web

from companion import __version__
from companion.auth import is_trusted_ingress
from companion.routes.health import RouteDef


async def get_status(request: web.Request) -> web.Response:
    ingress_active = is_trusted_ingress(request)
    auth_mode = "ingress" if ingress_active else "bearer"

    # Check if the Supervisor API is reachable (environment variable set by HA OS).
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    supervisor_reachable = bool(supervisor_token)

    has_ha_cli = shutil.which("ha") is not None

    config_base = request.app.get("config_base_path", "/config")
    config_writable = os.access(config_base, os.W_OK)

    return web.json_response(
        {
            "version": __version__,
            "supervisor_reachable": supervisor_reachable,
            "has_ha_cli": has_ha_cli,
            "config_writable": config_writable,
            "ingress_active": ingress_active,
            "auth_mode": auth_mode,
        }
    )


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/status", get_status),
]
