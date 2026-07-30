"""Helper entity CRUD endpoints.

Manages HA helpers (input_boolean, input_number, input_select, input_text,
input_datetime, counter, timer, schedule) via their per-domain YAML files.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from aiohttp import web
from ruamel.yaml import YAML

from companion import core_api
from companion.surgical import Edit, read_source, save_entry, write_fields
from companion.wiring import require_wired_target, wired_target_or_default

yaml = YAML()
yaml.preserve_quotes = True

ALLOWED_DOMAINS: set[str] = {
    "input_boolean",
    "input_number",
    "input_select",
    "input_text",
    "input_datetime",
    "counter",
    "timer",
    "schedule",
}


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


def _yaml_file_for_domain(domain: str) -> str:
    """Return the YAML config file name for a helper domain."""
    return f"{domain}.yaml"


def _load_helpers_from(target: Path) -> tuple[dict[str, Any], str]:
    """Load a helper YAML file's dict content from an explicit path.

    Returns ``(data, source_text)``; an absent file is an empty mapping and empty
    text. The source travels with the parsed data so a write can rewrite only the
    entry it was asked to change (see :mod:`companion.surgical`).
    """
    if not target.is_file():
        return {}, ""
    source = read_source(target)
    data = yaml.load(StringIO(source))
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise web.HTTPInternalServerError(text=f"{target.name} must be a top-level mapping")
    return data, source


def _load_helpers(base: str, domain: str) -> tuple[dict[str, Any], Path, str]:
    """Load a helper YAML file, returning (data_dict, file_path, source_text).

    Helper files are top-level mappings keyed by entity slug. The file is the
    one ``configuration.yaml`` wires the domain to, falling back to the
    conventional ``<domain>.yaml`` when the wiring cannot be established — used
    by the read/update/delete paths, which operate on helpers that (by
    definition) already exist. Resolving through the same function the create
    path uses is what stops a helper created in ``my_booleans.yaml`` from being
    invisible to the ``helper ls`` that follows it.
    """
    target = wired_target_or_default(base, domain, _yaml_file_for_domain(domain))
    data, source = _load_helpers_from(target)
    return data, target, source


async def _poll_entity_created(entity_id: str, attempts: int = 5, delay: float = 0.4) -> bool:
    """Poll /api/states until entity_id appears, or give up after `attempts` tries."""
    import asyncio

    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(delay)
        if await core_api.get_state(entity_id) is not None:
            return True
    return False


def _validate_domain(domain: str) -> None:
    """Raise 400 if domain is not an allowed helper domain."""
    if domain not in ALLOWED_DOMAINS:
        raise web.HTTPBadRequest(text=f"Invalid helper domain: {domain}. Allowed: {', '.join(sorted(ALLOWED_DOMAINS))}")


async def get_helpers(request: web.Request) -> web.Response:
    """GET /v1/config/helpers — list all helper definitions.

    Optional query param: domain (filters to a single domain).
    """
    base = request.app["config_base_path"]
    domain_filter = request.query.get("domain", "")

    domains = [domain_filter] if domain_filter else sorted(ALLOWED_DOMAINS)

    result: list[dict[str, Any]] = []
    for domain in domains:
        _validate_domain(domain)
        data, _target, _source = _load_helpers(base, domain)
        for helper_id, helper in data.items():
            if not isinstance(helper, dict):
                continue
            result.append(
                {
                    "id": helper_id,
                    "name": helper.get("name", helper_id),
                    "domain": domain,
                    "icon": helper.get("icon", ""),
                }
            )

    return web.json_response({"helpers": result})


async def get_helper(request: web.Request) -> web.Response:
    """GET /v1/config/helper?id=<id> — get single helper definition.

    The id should be the slug (e.g. 'my_toggle'). We search all domains.
    """
    base = request.app["config_base_path"]
    helper_id = request.query.get("id", "")
    if not helper_id:
        raise web.HTTPBadRequest(text="Missing id parameter")

    for domain in sorted(ALLOWED_DOMAINS):
        data, _target, _source = _load_helpers(base, domain)
        if helper_id in data:
            stream = StringIO()
            yaml.dump({helper_id: data[helper_id]}, stream)
            return web.json_response({"id": helper_id, "domain": domain, "content": stream.getvalue()})

    raise web.HTTPNotFound(text=f"Helper not found: {helper_id}")


async def post_helper(request: web.Request) -> web.Response:
    """POST /v1/config/helper?domain=<domain> — create new helper."""
    base = request.app["config_base_path"]
    domain = request.query.get("domain", "")
    if not domain:
        raise web.HTTPBadRequest(text="Missing domain parameter")
    _validate_domain(domain)
    # C-10: prove HA reads this file before writing a new helper into it.
    target = require_wired_target(base, domain, _yaml_file_for_domain(domain))

    body = await request.text()
    if not body.strip():
        raise web.HTTPBadRequest(text="Request body must not be empty")

    try:
        new_data = yaml.load(StringIO(body))
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"Invalid YAML: {exc}") from exc

    if not isinstance(new_data, dict) or len(new_data) != 1:
        raise web.HTTPBadRequest(text="Body must be a YAML mapping with exactly one top-level key (the helper id)")

    helper_id = next(iter(new_data))
    helper_body = new_data[helper_id]

    data, source = _load_helpers_from(target)
    if helper_id in data:
        raise web.HTTPConflict(text=f"Helper already exists: {helper_id}")

    data[helper_id] = helper_body
    surgical = save_entry(target, data, source, Edit("append", helper_id), yaml)
    reload = await core_api.reload_domain(domain)
    entity_id = f"{domain}.{helper_id}"
    entity_created = await _poll_entity_created(entity_id) if reload.ok else False
    return web.json_response(
        {
            "status": "created",
            "id": helper_id,
            "entity_id": entity_id,
            **write_fields(surgical),
            **core_api.reload_fields(reload),
            "entity_created": entity_created,
        },
        status=201,
    )


def _locate_helper(base: str, helper_id: str, domain: str | None) -> tuple[str, dict[str, Any], Path, str]:
    """Locate the domain file that owns ``helper_id`` for update/delete.

    With an explicit ``domain`` only that domain is considered. Without one, all
    domains are scanned. The same slug can legitimately exist in two domains
    (e.g. ``counter.kitchen`` and ``input_boolean.kitchen``), so scanning and
    silently acting on the alphabetically-first match would touch the wrong
    entity — instead the ambiguity is surfaced as a 409 listing the candidates.

    Returns ``(domain, data_dict, file_path, source_text)``.
    """
    if domain:
        _validate_domain(domain)
        data, target, source = _load_helpers(base, domain)
        if helper_id in data:
            return domain, data, target, source
        raise web.HTTPNotFound(text=f"Helper not found: {helper_id} (domain {domain})")

    matches: list[tuple[str, dict[str, Any], Path, str]] = []
    for candidate in sorted(ALLOWED_DOMAINS):
        data, target, source = _load_helpers(base, candidate)
        if helper_id in data:
            matches.append((candidate, data, target, source))

    if not matches:
        raise web.HTTPNotFound(text=f"Helper not found: {helper_id}")
    if len(matches) > 1:
        domains = ", ".join(candidate for candidate, _, _, _ in matches)
        raise web.HTTPConflict(
            text=(f"Helper id '{helper_id}' is ambiguous across domains: {domains}. Retry with ?domain=<one of these>.")
        )
    return matches[0]


async def put_helper(request: web.Request) -> web.Response:
    """PUT /v1/config/helper?id=<id>[&domain=<domain>] — update helper definition."""
    base = request.app["config_base_path"]
    helper_id = request.query.get("id", "")
    if not helper_id:
        raise web.HTTPBadRequest(text="Missing id parameter")
    domain_param = request.query.get("domain") or None

    body = await request.text()
    if not body.strip():
        raise web.HTTPBadRequest(text="Request body must not be empty")

    try:
        new_body = yaml.load(StringIO(body))
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"Invalid YAML: {exc}") from exc

    if not isinstance(new_body, dict):
        raise web.HTTPBadRequest(text="Helper must be a YAML mapping")

    domain, data, target, source = _locate_helper(base, helper_id, domain_param)
    data[helper_id] = new_body
    surgical = save_entry(target, data, source, Edit("replace", helper_id), yaml)
    reload = await core_api.reload_domain(domain)
    return web.json_response({"status": "applied", **write_fields(surgical), **core_api.reload_fields(reload)})


async def delete_helper(request: web.Request) -> web.Response:
    """DELETE /v1/config/helper?id=<id>[&domain=<domain>] — delete helper."""
    base = request.app["config_base_path"]
    helper_id = request.query.get("id", "")
    if not helper_id:
        raise web.HTTPBadRequest(text="Missing id parameter")
    domain_param = request.query.get("domain") or None

    domain, data, target, source = _locate_helper(base, helper_id, domain_param)
    del data[helper_id]
    surgical = save_entry(target, data, source, Edit("delete", helper_id), yaml)
    reload = await core_api.reload_domain(domain)
    return web.json_response({"status": "deleted", **write_fields(surgical), **core_api.reload_fields(reload)})


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/config/helpers", get_helpers),
    RouteDef("GET", "/v1/config/helper", get_helper),
    RouteDef("PUT", "/v1/config/helper", put_helper),
    RouteDef("POST", "/v1/config/helper", post_helper),
    RouteDef("DELETE", "/v1/config/helper", delete_helper),
]
