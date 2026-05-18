"""Automation definition CRUD endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from typing import Any

from aiohttp import web
from ruamel.yaml import YAML

from companion.routes.config import _resolve_config_path

yaml = YAML()
yaml.preserve_quotes = True

AUTOMATIONS_FILE = "automations.yaml"


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


def _load_automations(base: str) -> tuple[list[Any], Any]:
    """Load automations.yaml, returning (data_list, file_path)."""
    target = _resolve_config_path(base, AUTOMATIONS_FILE)
    if not target.is_file():
        raise web.HTTPNotFound(text=f"File not found: {AUTOMATIONS_FILE}")
    with open(target, encoding="utf-8") as f:
        data = yaml.load(f)
    if data is None:
        data = []
    if not isinstance(data, list):
        raise web.HTTPInternalServerError(text="automations.yaml must be a top-level list")
    return data, target


def _save_automations(target: Any, data: list[Any]) -> None:
    """Write automation data back to file with backup."""
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
    dry_run = request.query.get("dry_run", "true").lower() != "false"

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
            return web.json_response({"status": "applied"})

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

    data, target = _load_automations(base)

    # Check for duplicate id
    for item in data:
        if isinstance(item, dict) and item.get("id") == new_item["id"]:
            raise web.HTTPConflict(text=f"Automation already exists: {new_item['id']}")

    data.append(new_item)
    _save_automations(target, data)
    return web.json_response({"status": "created", "id": new_item["id"]}, status=201)


async def delete_automation(request: web.Request) -> web.Response:
    """DELETE /v1/config/automation?id=<id> — delete automation."""
    base = request.app["config_base_path"]
    automation_id = request.query.get("id", "")
    if not automation_id:
        raise web.HTTPBadRequest(text="Missing id parameter")

    data, target = _load_automations(base)

    for idx, item in enumerate(data):
        if isinstance(item, dict) and item.get("id") == automation_id:
            data.pop(idx)
            _save_automations(target, data)
            return web.json_response({"status": "deleted"})

    raise web.HTTPNotFound(text=f"Automation not found: {automation_id}")


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/config/automations", get_automations),
    RouteDef("GET", "/v1/config/automation", get_automation),
    RouteDef("PUT", "/v1/config/automation", put_automation),
    RouteDef("POST", "/v1/config/automation", post_automation),
    RouteDef("DELETE", "/v1/config/automation", delete_automation),
]
