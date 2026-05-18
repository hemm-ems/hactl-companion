"""Helper entity CRUD endpoints.

Manages HA helpers (input_boolean, input_number, input_select, input_text,
input_datetime, counter, timer, schedule) via their per-domain YAML files.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from typing import Any

from aiohttp import web
from ruamel.yaml import YAML

from companion.routes.config import _resolve_config_path

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


def _load_helpers(base: str, domain: str) -> tuple[dict[str, Any], Any]:
    """Load a helper YAML file, returning (data_dict, file_path).

    Helper files are top-level mappings keyed by entity slug.
    If the file doesn't exist, returns an empty dict and the target path.
    """
    filename = _yaml_file_for_domain(domain)
    target = _resolve_config_path(base, filename)
    if not target.is_file():
        return {}, target
    with open(target, encoding="utf-8") as f:
        data = yaml.load(f)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise web.HTTPInternalServerError(text=f"{filename} must be a top-level mapping")
    return data, target


def _save_helpers(target: Any, data: dict[str, Any]) -> None:
    """Write helper data back to file with backup."""
    import shutil
    from datetime import UTC, datetime
    from pathlib import Path

    path = Path(target)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    backup_name = f"{path.name}.bak.{timestamp}"
    if path.is_file():
        shutil.copy2(path, path.parent / backup_name)

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f)


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
        data, _target = _load_helpers(base, domain)
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
        data, _target = _load_helpers(base, domain)
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

    data, target = _load_helpers(base, domain)
    if helper_id in data:
        raise web.HTTPConflict(text=f"Helper already exists: {helper_id}")

    data[helper_id] = helper_body
    _save_helpers(target, data)
    return web.json_response({"status": "created", "id": helper_id}, status=201)


async def put_helper(request: web.Request) -> web.Response:
    """PUT /v1/config/helper?id=<id> — update helper definition."""
    base = request.app["config_base_path"]
    helper_id = request.query.get("id", "")
    if not helper_id:
        raise web.HTTPBadRequest(text="Missing id parameter")

    body = await request.text()
    if not body.strip():
        raise web.HTTPBadRequest(text="Request body must not be empty")

    try:
        new_body = yaml.load(StringIO(body))
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"Invalid YAML: {exc}") from exc

    if not isinstance(new_body, dict):
        raise web.HTTPBadRequest(text="Helper must be a YAML mapping")

    # Find which domain this helper belongs to
    for domain in sorted(ALLOWED_DOMAINS):
        data, target = _load_helpers(base, domain)
        if helper_id in data:
            data[helper_id] = new_body
            _save_helpers(target, data)
            return web.json_response({"status": "applied"})

    raise web.HTTPNotFound(text=f"Helper not found: {helper_id}")


async def delete_helper(request: web.Request) -> web.Response:
    """DELETE /v1/config/helper?id=<id> — delete helper."""
    base = request.app["config_base_path"]
    helper_id = request.query.get("id", "")
    if not helper_id:
        raise web.HTTPBadRequest(text="Missing id parameter")

    # Find which domain this helper belongs to
    for domain in sorted(ALLOWED_DOMAINS):
        data, target = _load_helpers(base, domain)
        if helper_id in data:
            del data[helper_id]
            _save_helpers(target, data)
            return web.json_response({"status": "deleted"})

    raise web.HTTPNotFound(text=f"Helper not found: {helper_id}")


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/config/helpers", get_helpers),
    RouteDef("GET", "/v1/config/helper", get_helper),
    RouteDef("PUT", "/v1/config/helper", put_helper),
    RouteDef("POST", "/v1/config/helper", post_helper),
    RouteDef("DELETE", "/v1/config/helper", delete_helper),
]
