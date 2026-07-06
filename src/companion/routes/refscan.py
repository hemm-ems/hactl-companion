"""Cross-file literal reference scan/replace endpoints.

Exposes :mod:`companion.refscan` over HTTP so a caller can find every place a
literal value (typically a renamed/stale entity_id) is still referenced across
the ``!include`` graph, and rewrite it in the file it actually lives in. The
``{location, path, ...}`` shape matches the Go ``jsonwalk`` output, so a client
can merge these YAML hits with dashboard hits into one uniform result set.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiohttp import web

from companion.refscan import replace_yaml_literal, scan_yaml_for_entities, scan_yaml_for_literal


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


async def get_ref_scan(request: web.Request) -> web.Response:
    """GET /v1/ref/scan?target=... — every literal reference to ``target``."""
    target = request.query.get("target", "")
    if not target:
        raise web.HTTPBadRequest(text="Missing target parameter")

    base = request.app["config_base_path"]
    hits = scan_yaml_for_literal(base, target)
    return web.json_response(
        {
            "target": target,
            "hits": [{"location": hit.location, "path": hit.path, "matched_value": hit.matched_value} for hit in hits],
        }
    )


async def get_ref_entities(request: web.Request) -> web.Response:
    """GET /v1/ref/entities — every entity_id-shaped leaf across config files.

    Bulk enumeration for reference validation: the caller diffs the returned
    values against the live entity set to find dangling references. Unfiltered
    by design (service names share the entity_id shape).
    """
    base = request.app["config_base_path"]
    entities = scan_yaml_for_entities(base)
    return web.json_response(
        {
            "entities": [
                {"location": ref.location, "path": ref.path, "key": ref.key, "matched_value": ref.matched_value}
                for ref in entities
            ],
        }
    )


async def post_ref_replace(request: web.Request) -> web.Response:
    """POST /v1/ref/replace {old,new,dry_run} — rewrite a literal across config files.

    ``dry_run`` defaults to true (report only). With ``dry_run=false`` the literal
    is rewritten in every owning file. Both modes return the same ``changes`` list.
    """
    try:
        body = await request.json()
    except ValueError as exc:
        raise web.HTTPBadRequest(text="Request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="Request body must be a JSON object")

    old = body.get("old")
    new = body.get("new")
    if not isinstance(old, str) or not old:
        raise web.HTTPBadRequest(text="Missing 'old' string")
    if not isinstance(new, str) or not new:
        raise web.HTTPBadRequest(text="Missing 'new' string")
    dry_run = bool(body.get("dry_run", True))

    base = request.app["config_base_path"]
    changes = replace_yaml_literal(base, old, new, dry_run=dry_run)
    return web.json_response(
        {
            "status": "dry_run" if dry_run else "applied",
            "changes": changes,
        }
    )


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/ref/scan", get_ref_scan),
    RouteDef("GET", "/v1/ref/entities", get_ref_entities),
    RouteDef("POST", "/v1/ref/replace", post_ref_replace),
]
