"""GET / — status page shown when the add-on is opened via HA Ingress or sidebar."""

from __future__ import annotations

from dataclasses import dataclass

from aiohttp import web

from companion import __version__


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


async def get_root(request: web.Request) -> web.Response:
    body = f"hactl-companion v{__version__}\nstatus: running\napi: /v1/health\n"
    return web.Response(text=body, content_type="text/plain")


routes: list[RouteDef] = [
    RouteDef("GET", "/", get_root),
]
