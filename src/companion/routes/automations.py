"""Automation definition CRUD endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from aiohttp import web
from ruamel.yaml import YAML

from companion import core_api
from companion.params import parse_bool_param
from companion.wiring import require_wired_target, wired_target_or_default

yaml = YAML()
yaml.preserve_quotes = True

AUTOMATION_DOMAIN = "automation"
AUTOMATIONS_FILE = "automations.yaml"


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


def _load_automations(base: str) -> tuple[list[Any], Any]:
    """Load the automation file, returning (data_list, file_path).

    The file is whichever one ``configuration.yaml`` wires ``automation:`` to,
    falling back to the conventional name when that cannot be established.
    """
    return _load_automations_from(wired_target_or_default(base, AUTOMATION_DOMAIN, AUTOMATIONS_FILE))


def _load_automations_from(target: Path) -> tuple[list[Any], Any]:
    """Load an explicit automation file, returning (data_list, path)."""
    if not target.is_file():
        raise web.HTTPNotFound(text=f"File not found: {target.name}")
    with open(target, encoding="utf-8") as f:
        data = yaml.load(f)
    if data is None:
        data = []
    if not isinstance(data, list):
        raise web.HTTPInternalServerError(text="automations.yaml must be a top-level list")
    return data, target


def _find_automation(data: list[Any], candidate: str) -> tuple[int, dict[str, Any]] | None:
    """Find an automation by config id, falling back to alias.

    HA derives the live entity_id from `alias`, not the config `id`, so a
    caller may reasonably pass either.
    """
    for idx, item in enumerate(data):
        if isinstance(item, dict) and item.get("id") == candidate:
            return idx, item
    for idx, item in enumerate(data):
        if isinstance(item, dict) and item.get("alias") == candidate:
            return idx, item
    return None


async def _resolve_automation_id_via_states(candidate: str) -> str | None:
    """Resolve a live entity_id (or unrecognized identifier) to its config id.

    HA always sets an automation entity's `attributes.id` to its config id,
    regardless of the alias-derived entity_id — this bridges "the id used
    for storage" and "the id used for the live entity" without reimplementing
    HA's slugify. Returns None if no live state matches or states can't be
    fetched (best-effort).
    """
    states = await core_api.get_states()
    if states is None:
        return None
    for state in states:
        if not isinstance(state, dict):
            continue
        config_id: str | None = state.get("attributes", {}).get("id")
        if state.get("entity_id") == candidate or config_id == candidate:
            return config_id
    return None


async def _poll_automation_entity_id(config_id: str, attempts: int = 5, delay: float = 0.4) -> str | None:
    """Poll /api/states for the entity whose attributes.id matches config_id."""
    import asyncio

    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(delay)
        states = await core_api.get_states()
        if states is None:
            continue
        for state in states:
            if isinstance(state, dict) and state.get("attributes", {}).get("id") == config_id:
                entity_id: str | None = state.get("entity_id")
                return entity_id
    return None


def _save_automations(target: Any, data: list[Any]) -> None:
    """Write automation data back to file with backup."""
    from pathlib import Path

    from companion.backups import make_backup

    path = Path(target)
    make_backup(path)

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f)


async def get_automations(request: web.Request) -> web.Response:
    """GET /v1/config/automations — list all automation definitions."""
    base = request.app["config_base_path"]
    data, _target = _load_automations(base)

    result: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "id": item.get("id", ""),
                "alias": item.get("alias", ""),
                "mode": item.get("mode", "single"),
                "description": item.get("description", ""),
            }
        )

    return web.json_response({"automations": result})


async def get_automation(request: web.Request) -> web.Response:
    """GET /v1/config/automation?id=<id> — get single automation definition."""
    base = request.app["config_base_path"]
    automation_id = request.query.get("id", "")
    if not automation_id:
        raise web.HTTPBadRequest(text="Missing id parameter")

    data, _target = _load_automations(base)

    for item in data:
        if isinstance(item, dict) and item.get("id") == automation_id:
            stream = StringIO()
            yaml.dump(item, stream)
            return web.json_response({"id": automation_id, "content": stream.getvalue()})

    raise web.HTTPNotFound(text=f"Automation not found: {automation_id}")


async def put_automation(request: web.Request) -> web.Response:
    """PUT /v1/config/automation?id=<id>&dry_run=true — update automation definition."""
    base = request.app["config_base_path"]
    automation_id = request.query.get("id", "")
    dry_run = parse_bool_param(request, "dry_run", default=True)

    if not automation_id:
        raise web.HTTPBadRequest(text="Missing id parameter")

    body = await request.text()
    if not body.strip():
        raise web.HTTPBadRequest(text="Request body must not be empty")

    try:
        new_item = yaml.load(StringIO(body))
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"Invalid YAML: {exc}") from exc

    if not isinstance(new_item, dict):
        raise web.HTTPBadRequest(text="Automation must be a YAML mapping")

    data, target = _load_automations(base)

    for idx, item in enumerate(data):
        if isinstance(item, dict) and item.get("id") == automation_id:
            if dry_run:
                import difflib

                old_stream = StringIO()
                yaml.dump(item, old_stream)
                new_stream = StringIO()
                yaml.dump(new_item, new_stream)
                diff = "".join(
                    difflib.unified_diff(
                        old_stream.getvalue().splitlines(keepends=True),
                        new_stream.getvalue().splitlines(keepends=True),
                        fromfile=f"a/{automation_id}",
                        tofile=f"b/{automation_id}",
                    )
                )
                return web.json_response({"status": "dry_run", "diff": diff})

            data[idx] = new_item
            _save_automations(target, data)
            reload = await core_api.reload_domain("automation")
            return web.json_response({"status": "applied", **core_api.reload_fields(reload)})

    raise web.HTTPNotFound(text=f"Automation not found: {automation_id}")


async def post_automation(request: web.Request) -> web.Response:
    """POST /v1/config/automation — create new automation."""
    base = request.app["config_base_path"]
    body = await request.text()
    if not body.strip():
        raise web.HTTPBadRequest(text="Request body must not be empty")

    try:
        new_item = yaml.load(StringIO(body))
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"Invalid YAML: {exc}") from exc

    if not isinstance(new_item, dict):
        raise web.HTTPBadRequest(text="Automation must be a YAML mapping")

    if "id" not in new_item:
        raise web.HTTPBadRequest(text="Automation must have an id field")

    # C-10: prove HA reads this file before claiming the automation was created.
    data, target = _load_automations_from(require_wired_target(base, AUTOMATION_DOMAIN, AUTOMATIONS_FILE))

    # Check for duplicate id
    for item in data:
        if isinstance(item, dict) and item.get("id") == new_item["id"]:
            raise web.HTTPConflict(text=f"Automation already exists: {new_item['id']}")

    data.append(new_item)
    _save_automations(target, data)
    reload = await core_api.reload_domain("automation")
    entity_id = await _poll_automation_entity_id(new_item["id"]) if reload.ok else None
    return web.json_response(
        {"status": "created", "id": new_item["id"], "entity_id": entity_id, **core_api.reload_fields(reload)},
        status=201,
    )


async def delete_automation(request: web.Request) -> web.Response:
    """DELETE /v1/config/automation?id=<id> — delete automation.

    `id` may be the config id, the alias, or (if neither matches the config
    file) the live entity_id — HA derives entity_id from alias, so callers
    working from `hactl auto` output may only have the display identifier.
    """
    base = request.app["config_base_path"]
    automation_id = request.query.get("id", "")
    if not automation_id:
        raise web.HTTPBadRequest(text="Missing id parameter")

    data, target = _load_automations(base)

    match = _find_automation(data, automation_id)
    if match is None:
        resolved_id = await _resolve_automation_id_via_states(automation_id)
        if resolved_id is not None:
            match = _find_automation(data, resolved_id)

    if match is not None:
        idx, _item = match
        data.pop(idx)
        _save_automations(target, data)
        reload = await core_api.reload_domain("automation")
        return web.json_response({"status": "deleted", **core_api.reload_fields(reload)})

    raise web.HTTPNotFound(text=f"Automation not found: {automation_id}")


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/config/automations", get_automations),
    RouteDef("GET", "/v1/config/automation", get_automation),
    RouteDef("PUT", "/v1/config/automation", put_automation),
    RouteDef("POST", "/v1/config/automation", post_automation),
    RouteDef("DELETE", "/v1/config/automation", delete_automation),
]
