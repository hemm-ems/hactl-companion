"""Script definition CRUD endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from aiohttp import web
from ruamel.yaml import YAML

from companion import core_api
from companion.params import parse_bool_param
from companion.surgical import Edit, read_source, save_entry, write_fields
from companion.wiring import require_wired_target, wired_target_or_default

yaml = YAML()
yaml.preserve_quotes = True

SCRIPT_DOMAIN = "script"
SCRIPTS_FILE = "scripts.yaml"


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


def _load_scripts(base: str) -> tuple[dict[str, Any], Path, str]:
    """Load the script file, returning (data_dict, file_path, source_text).

    The file is whichever one ``configuration.yaml`` wires ``script:`` to,
    falling back to the conventional name when that cannot be established.
    """
    return _load_scripts_from(base, wired_target_or_default(base, SCRIPT_DOMAIN, SCRIPTS_FILE))


def _load_scripts_from(base: str, target: Path) -> tuple[dict[str, Any], Path, str]:
    """Load an explicit script file, returning (data_dict, path, source_text).

    The source text travels with the parsed data so a write can rewrite only the
    entry it was asked to change (see :mod:`companion.surgical`).
    """
    if not target.is_file():
        raise web.HTTPNotFound(text=f"File not found: {target.name}")
    source = read_source(base, target)
    data = yaml.load(StringIO(source))
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise web.HTTPInternalServerError(text="scripts.yaml must be a top-level mapping")
    return data, target, source


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


def _is_script_definition_key(key: str) -> bool:
    return key in {
        "alias",
        "description",
        "sequence",
        "mode",
        "fields",
        "variables",
        "icon",
        "max",
        "max_exceeded",
        "trace",
    }


def _normalize_script_body(script_id: str, new_data: dict[str, Any]) -> dict[str, Any]:
    """Accept UI-style script YAML or scripts.yaml wrapper form."""
    if len(new_data) == 1:
        key = next(iter(new_data))
        value = new_data[key]
        if key == script_id:
            if not isinstance(value, dict):
                raise web.HTTPBadRequest(text=f"Script {key} must be a YAML mapping")
            return value
        if not _is_script_definition_key(key):
            raise web.HTTPBadRequest(text=f"Script wrapper id {key} does not match target {script_id}")
    return new_data


async def get_scripts(request: web.Request) -> web.Response:
    """GET /v1/config/scripts — list all script definitions."""
    base = request.app["config_base_path"]
    data, _target, _source = _load_scripts(base)

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

    data, _target, _source = _load_scripts(base)
    if script_id not in data:
        raise web.HTTPNotFound(text=f"Script not found: {script_id}")

    stream = StringIO()
    yaml.dump({script_id: data[script_id]}, stream)
    return web.json_response({"id": script_id, "content": stream.getvalue()})


async def put_script(request: web.Request) -> web.Response:
    """PUT /v1/config/script?id=<script_id>&dry_run=true — update script definition."""
    base = request.app["config_base_path"]
    script_id = request.query.get("id", "")
    dry_run = parse_bool_param(request, "dry_run", default=True)

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
    script_body = _normalize_script_body(script_id, new_data)

    data, target, source = _load_scripts(base)
    if script_id not in data:
        raise web.HTTPNotFound(text=f"Script not found: {script_id}")

    if dry_run:
        import difflib

        old_stream = StringIO()
        yaml.dump({script_id: data[script_id]}, old_stream)
        new_stream = StringIO()
        yaml.dump({script_id: script_body}, new_stream)
        diff = "".join(
            difflib.unified_diff(
                old_stream.getvalue().splitlines(keepends=True),
                new_stream.getvalue().splitlines(keepends=True),
                fromfile=f"a/{script_id}",
                tofile=f"b/{script_id}",
            )
        )
        return web.json_response({"status": "dry_run", "diff": diff})

    data[script_id] = script_body
    surgical = save_entry(base, target, data, source, Edit("replace", script_id), yaml)
    reload = await core_api.reload_domain("script")
    return web.json_response({"status": "applied", **write_fields(surgical), **core_api.reload_fields(reload)})


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

    # C-10: prove HA reads this file before claiming the script was created.
    data, target, source = _load_scripts_from(base, require_wired_target(base, SCRIPT_DOMAIN, SCRIPTS_FILE))
    if script_id in data:
        raise web.HTTPConflict(text=f"Script already exists: {script_id}")

    data[script_id] = script_body
    surgical = save_entry(base, target, data, source, Edit("append", script_id), yaml)
    reload = await core_api.reload_domain("script")
    return web.json_response(
        {"status": "created", "id": script_id, **write_fields(surgical), **core_api.reload_fields(reload)},
        status=201,
    )


async def delete_script(request: web.Request) -> web.Response:
    """DELETE /v1/config/script?id=<script_id> — delete script."""
    base = request.app["config_base_path"]
    script_id = request.query.get("id", "")
    if not script_id:
        raise web.HTTPBadRequest(text="Missing id parameter")

    data, target, source = _load_scripts(base)
    if script_id not in data:
        raise web.HTTPNotFound(text=f"Script not found: {script_id}")

    del data[script_id]
    surgical = save_entry(base, target, data, source, Edit("delete", script_id), yaml)
    reload = await core_api.reload_domain("script")
    return web.json_response({"status": "deleted", **write_fields(surgical), **core_api.reload_fields(reload)})


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/config/scripts", get_scripts),
    RouteDef("GET", "/v1/config/script", get_script),
    RouteDef("PUT", "/v1/config/script", put_script),
    RouteDef("POST", "/v1/config/script", post_script),
    RouteDef("DELETE", "/v1/config/script", delete_script),
]
