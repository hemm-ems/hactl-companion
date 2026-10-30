"""``GET /v1/ref/grep`` — read-only substring/regex search across the config file graph.

Its own route module rather than a fourth handler in ``routes/refscan.py``: the
``ref`` family's write route lives there, and hactl D-48 keeps the substring
matcher out of its reach. Everything a config-walking read route owes its caller
applies here unchanged — the auth middleware (C-1), the path guard inside the
resolver the walk reads through (C-3: ``secrets.yaml`` and ``.storage`` are
never opened, and say so in ``skipped``), the unsupported-include 400 (C-11) and
the error envelope (C-8) all come from the app, not from this handler.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiohttp import web

from companion.params import parse_bool_param
from companion.refgrep import GrepRequestError, grep_config
from companion.refscan import SkipLog, skipped_fields


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


async def get_ref_grep(request: web.Request) -> web.Response:
    """GET /v1/ref/grep?term=...[&regex=true][&case_sensitive=true] — where a term appears.

    ``truncated`` and ``total`` are always present: a capped answer is stated,
    never implied. ``skipped`` (present only when non-empty) names the config
    files the walk could not read.
    """
    term = request.query.get("term", "")
    regex = parse_bool_param(request, "regex", default=False)
    case_sensitive = parse_bool_param(request, "case_sensitive", default=False)

    base = request.app["config_base_path"]
    skipped = SkipLog()
    try:
        result = await grep_config(base, term, regex=regex, case_sensitive=case_sensitive, skipped=skipped)
    except GrepRequestError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    return web.json_response(
        {
            "term": term,
            "regex": regex,
            "case_sensitive": case_sensitive,
            "hits": [
                {
                    "location": hit.location,
                    "path": hit.path,
                    "key": hit.key,
                    "kind": hit.kind,
                    "matched_value": hit.matched_value,
                }
                for hit in result.hits
            ],
            "total": result.total,
            "truncated": result.truncated,
            "sources": [
                {"location": s.location, "key_hits": s.key_hits, "value_hits": s.value_hits} for s in result.sources
            ],
            **skipped_fields(skipped),
        }
    )


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/ref/grep", get_ref_grep),
]
