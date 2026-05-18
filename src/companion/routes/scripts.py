"""Script definition CRUD endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from typing import Any

from aiohttp import web
from ruamel.yaml import YAML

from companion.routes.config import _resolve_config_path

yaml = YAML()
yaml.preserve_quotes = True

SCRIPTS_FILE = "scripts.yaml"


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


def _load_scripts(base: str) -> tuple[dict[str, Any], Any]:
    """Load scripts.yaml, returning (data_dict, file_path)."""
    target = _resolve_config_path(base, SCRIPTS_FILE)
    if not target.is_file():
        raise web.HTTPNotFound(text=f"File not found: {SCRIPTS_FILE}")
    with open(target, encoding="utf-8") as f:
        data = yaml.load(f)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise web.HTTPInternalServerError(text="scripts.yaml must be a top-level mapping")
    return data, target


def _save_scripts(target: Any, data: dict[str, Any]) -> None:
    """Write script data back to file with backup."""
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


def _extract_fields(script: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract fields metadata from a script definition."""
    fields_raw = script.get("fields")
    if not isinstance(fields_raw, dict):
        return []
    result: list[dict[str, Any]] = []
    for name, spec in fields_raw.items():
        if not isinstance(spec, dict):
            continue
        result.append(
            {
                "name": name,
                "description": spec.get("description", ""),
                "required": spec.get("required", False),
                "selector": spec.get("selector"),
            }
        )
    return result


async def get_scripts(request: web.Request) -> web.Response:
    """GET /v1/config/scripts — list all script definitions."""
    base = request.app["config_base_path"]
    data, _target = _load_scripts(base)

    result: list[dict[str, Any]] = []
    for script_id, script in data.items():
        if not isinstance(script, dict):
            continue
        result.append(
            {
                "id": script_id,
                "alias": script.get("alias", ""),
                "mode": script.get("mode", "single"),
                "fields": _extract_fields(script),
            }
        )

    return web.json_response({"scripts": result})


async def get_script(request: web.Request) -> web.Response:
    """GET /v1/config/script?id=<script_id> — get single script definition."""
    base = request.app["config_base_path"]
    script_id = request.query.get("id", "")
    if not script_id:
        raise web.HTTPBadRequest(text="Missing id parameter")

    data, _target = _load_scripts(base)
    if script_id not in data:
        raise web.HTTPNotFound(text=f"Script not found: {script_id}")

    stream = StringIO()
    yaml.dump({script_id: data[script_id]}, stream)
    return web.json_response({"id": script_id, "content": stream.getvalue()})


async def put_script(request: web.Request) -> web.Response:
    """PUT /v1/config/script?id=<script_id>&dry_run=true — update script definition."""
    base = request.app["config_base_path"]
    script_id = request.query.get("id", "")
    dry_run = request.query.get("dry_run", "true").lower() != "false"

    if not script_id:
        raise web.HTTPBadRequest(text="Missing id parameter")

    body = await request.text()
    if not body.strip():
        raise web.HTTPBadRequest(text="Request body must not be empty")

    try:
        new_data = yaml.load(StringIO(body))
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"Invalid YAML: {exc}") from exc

    if not isinstance(new_data, dict):
        raise web.HTTPBadRequest(text="Script must be a YAML mapping")

    data, target = _load_scripts(base)
    if script_id not in data:
        raise web.HTTPNotFound(text=f"Script not found: {script_id}")

    if dry_run:
        import difflib

        old_stream = StringIO()
        yaml.dump({script_id: data[script_id]}, old_stream)
        new_stream = StringIO()
        yaml.dump({script_id: new_data}, new_stream)
        diff = "".join(
            difflib.unified_diff(
                old_stream.getvalue().splitlines(keepends=True),
                new_stream.getvalue().splitlines(keepends=True),
                fromfile=f"a/{script_id}",
                tofile=f"b/{script_id}",
            )
        )
        return web.json_response({"status": "dry_run", "diff": diff})

    data[script_id] = new_data
    _save_scripts(target, data)
    return web.json_response({"status": "applied"})


async def post_script(request: web.Request) -> web.Response:
    """POST /v1/config/script — create new script."""
    base = request.app["config_base_path"]
    body = await request.text()
    if not body.strip():
        raise web.HTTPBadRequest(text="Request body must not be empty")

    try:
        new_data = yaml.load(StringIO(body))
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"Invalid YAML: {exc}") from exc

    if not isinstance(new_data, dict) or len(new_data) != 1:
        raise web.HTTPBadRequest(text="Body must be a YAML mapping with exactly one top-level key (the script id)")

    script_id = next(iter(new_data))
    script_body = new_data[script_id]

    data, target = _load_scripts(base)
    if script_id in data:
        raise web.HTTPConflict(text=f"Script already exists: {script_id}")

    data[script_id] = script_body
    _save_scripts(target, data)
    return web.json_response({"status": "created", "id": script_id}, status=201)


async def delete_script(request: web.Request) -> web.Response:
    """DELETE /v1/config/script?id=<script_id> — delete script."""
    base = request.app["config_base_path"]
    script_id = request.query.get("id", "")
    if not script_id:
        raise web.HTTPBadRequest(text="Missing id parameter")

    data, target = _load_scripts(base)
    if script_id not in data:
        raise web.HTTPNotFound(text=f"Script not found: {script_id}")

    del data[script_id]
    _save_scripts(target, data)
    return web.json_response({"status": "deleted"})


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/config/scripts", get_scripts),
    RouteDef("GET", "/v1/config/script", get_script),
    RouteDef("PUT", "/v1/config/script", put_script),
    RouteDef("POST", "/v1/config/script", post_script),
    RouteDef("DELETE", "/v1/config/script", delete_script),
]
