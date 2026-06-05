"""GET /v1/logs — recent companion log records from the in-memory ring buffer."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from aiohttp import web

from companion import logbuffer

_SINCE_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_SINCE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


def _parse_since(value: str | None) -> float | None:
    """Parse a relative duration like '30m'/'24h' into an epoch cutoff."""
    if not value:
        return None
    m = _SINCE_RE.match(value)
    if not m:
        raise web.HTTPBadRequest(text="Invalid 'since': use forms like 30m, 24h, 7d")
    return time.time() - int(m.group(1)) * _SINCE_UNITS[m.group(2).lower()]


def _parse_limit(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        limit = int(value)
    except ValueError as exc:
        raise web.HTTPBadRequest(text="Invalid 'limit': must be an integer") from exc
    if limit < 0:
        raise web.HTTPBadRequest(text="Invalid 'limit': must be >= 0")
    return limit


async def get_logs(request: web.Request) -> web.Response:
    """GET /v1/logs?component=&level=&since=&limit= — query buffered log records."""
    # Validate query params first so bad input is a 400 even before the buffer
    # is installed.
    since = _parse_since(request.query.get("since"))
    limit = _parse_limit(request.query.get("limit"))

    handler = logbuffer.get_handler()
    if handler is None:
        return web.json_response({"entries": []})

    entries = handler.snapshot(
        component=request.query.get("component"),
        level=request.query.get("level"),
        since=since,
        limit=limit,
    )
    return web.json_response({"entries": [e.as_dict() for e in entries]})


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/logs", get_logs),
]
