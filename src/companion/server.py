"""App factory, middleware, auth, and plugin registry."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from companion import __version__
from companion.auth import bearer_token_valid, is_trusted_ingress
from companion.routes import (
    automations,
    config,
    ha,
    health,
    helpers,
    logs,
    refscan,
    related,
    root,
    scripts,
    status,
    templates,
    wireguard,
)
from companion.yaml_resolver import UnknownIncludeTagError

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
    # Seed status so an unexpected (non-HTTP) exception from the handler doesn't
    # leave `status` unbound in `finally` — that UnboundLocalError would mask the
    # real traceback and turn every unhandled bug into the same opaque error.
    status = 500
    if request.path in AUTH_EXEMPT_PATHS:
        auth_mode = "none"
    elif is_trusted_ingress(request):
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
        level = logging.WARNING if status == 401 else logging.INFO
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
async def error_envelope_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Normalize error bodies to a JSON envelope: ``{"error": {"code", "message"}}``.

    Handlers raise ``HTTPException(text=...)`` with plain-text bodies while success
    responses are JSON — leaving consumers unable to distinguish error kinds
    programmatically. This wraps any >=400 HTTPException that carries a non-JSON
    body into one uniform JSON shape, preserving the status code. Sits inside the
    access-log middleware, so the returned response is still logged normally.
    """
    try:
        return await handler(request)
    except web.HTTPException as exc:
        if exc.status < 400 or (exc.content_type or "").startswith("application/json"):
            raise
        return web.json_response(
            {"error": {"code": exc.status, "message": exc.text or exc.reason or ""}},
            status=exc.status,
        )


@web.middleware
async def unsupported_include_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Turn an unsupported include directive into a 400, on every route at once.

    An include-family tag this build does not implement (:mod:`companion.yaml_resolver`)
    is a deliberate hard failure: whatever the tag names would be silently absent
    from the resolved config, and a partial answer presented as a whole one is the
    class of bug this service exists to stop reporting. Handled here rather than in
    each route so a *new* route that touches the config graph inherits the correct
    status without anyone remembering to catch it — the same reason the auth and
    error-envelope rules live at this level.

    400 rather than 5xx: hactl retries idempotent requests on any 5xx
    (``internal/companion/client.go``: ``shouldRetry`` returns ``idempotent``
    for ``status >= 500``; ``isIdempotentMethod`` covers GET/HEAD/PUT/DELETE/
    OPTIONS, and the caller uses ``backoffs = [500ms, 1s]`` for three attempts
    total). Every route that resolves the config graph is a GET, so a 5xx here
    would buy three round trips and 1.5 s of backoff for a config that cannot
    parse differently the second time. A 4xx is delivered once, with the
    message.

    The status is a compromise and worth naming as one: the *request* is
    well-formed, so 400 is not literally accurate — the server's data is what
    this build cannot handle. It is chosen over a semantically tidier 5xx
    because it is the only status in that family that reaches the user once
    instead of three times, and it matches the precedent already set by the
    helper routes, which answer 400 for "your configuration.yaml is not set up
    for this".
    """
    try:
        return await handler(request)
    except UnknownIncludeTagError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc


@web.middleware
async def auth_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Validate Bearer token for non-exempt paths."""
    if request.path in AUTH_EXEMPT_PATHS:
        return await handler(request)

    # When accessed via HA Ingress, the Supervisor proxy already authenticated the
    # user. Trust that only when the request provably comes from the proxy — the
    # X-Ingress-Path header alone is client-controlled (see companion.auth).
    if is_trusted_ingress(request):
        return await handler(request)

    expected_token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not expected_token:
        # No token configured — there is nothing to authenticate against. Fail
        # closed (an empty expected token must never be satisfied by an empty
        # or any other credential).
        raise web.HTTPServiceUnavailable(text="Server authentication is not configured (SUPERVISOR_TOKEN unset)")

    auth_header = request.headers.get("Authorization", "")
    if not bearer_token_valid(auth_header, expected_token):
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
            error_envelope_middleware,
            unsupported_include_middleware,
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
    register_routes(app, related)
    register_routes(app, refscan)

    return app
