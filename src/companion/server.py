"""App factory, middleware, auth, and plugin registry."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from companion import __version__
from companion.routes import (
    automations,
    config,
    ha,
    health,
    helpers,
    logs,
    root,
    scripts,
    status,
    templates,
    wireguard,
)

logger = logging.getLogger("companion.access")

# Paths that do not require authentication
AUTH_EXEMPT_PATHS: set[str] = {"/v1/health", "/v1/status"}


@web.middleware
async def access_log_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Log every request with method, path, status, duration, and auth mode."""
    start = time.monotonic()
    if request.path in AUTH_EXEMPT_PATHS:
        auth_mode = "none"
    elif request.headers.get("X-Ingress-Path") is not None:
        auth_mode = "ingress"
    else:
        auth_mode = "bearer"

    try:
        response = await handler(request)
        status = response.status
    except web.HTTPException as exc:
        status = exc.status
        raise
    finally:
        duration_ms = int((time.monotonic() - start) * 1000)
        # Health/status pings (auth-exempt) are high-frequency noise — log them
        # at DEBUG so real API calls stand out in the add-on log. Keep 401s at
        # WARNING so auth problems are always visible.
        if status == 401:
            level = logging.WARNING
        elif request.path in AUTH_EXEMPT_PATHS:
            level = logging.DEBUG
        else:
            level = logging.INFO
        logger.log(
            level,
            "%s %s status=%d duration=%dms auth=%s",
            request.method,
            request.path,
            status,
            duration_ms,
            auth_mode,
        )

    return response


@web.middleware
async def auth_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Validate Bearer token for non-exempt paths."""
    if request.path in AUTH_EXEMPT_PATHS:
        return await handler(request)

    # When accessed via HA Ingress, the proxy already authenticated the user.
    # The Ingress header is set by the HA Ingress proxy.
    ingress_header = request.headers.get("X-Ingress-Path")
    if ingress_header is not None:
        return await handler(request)

    expected_token = os.environ.get("SUPERVISOR_TOKEN", "")
    auth_header = request.headers.get("Authorization", "")

    if not auth_header.startswith("Bearer ") or auth_header[7:] != expected_token:
        raise web.HTTPUnauthorized(text="Invalid or missing authentication token")

    return await handler(request)


def register_routes(app: web.Application, module: Any) -> None:
    """Register all routes from a route module."""
    for route_def in module.routes:
        app.router.add_route(route_def.method, route_def.path, route_def.handler)


def create_app(config_base_path: str = "/config") -> web.Application:
    """Create and configure the aiohttp application."""
    app = web.Application(
        middlewares=[
            web.normalize_path_middleware(merge_slashes=True, append_slash=False),
            access_log_middleware,
            auth_middleware,
        ]
    )

    # Store shared config
    app["version"] = __version__
    app["config_base_path"] = config_base_path

    # Register route modules
    register_routes(app, root)
    register_routes(app, health)
    register_routes(app, status)
    register_routes(app, config)
    register_routes(app, templates)
    register_routes(app, scripts)
    register_routes(app, automations)
    register_routes(app, helpers)
    register_routes(app, ha)
    register_routes(app, wireguard)
    register_routes(app, logs)

    return app
