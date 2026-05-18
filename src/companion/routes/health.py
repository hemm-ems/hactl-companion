"""GET /v1/health — liveness check."""

from __future__ import annotations

from dataclasses import dataclass

from aiohttp import web

from companion import __version__


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


async def get_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "version": __version__})


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/health", get_health),
]
