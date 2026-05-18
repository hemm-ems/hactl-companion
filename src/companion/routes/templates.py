"""Template sensor CRUD endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from typing import Any

from aiohttp import web
from ruamel.yaml import YAML

from companion.routes.config import _resolve_config_path

yaml = YAML()
yaml.preserve_quotes = True

TEMPLATE_FILE = "template.yaml"


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


def _load_templates(base: str) -> tuple[list[Any], Any]:
    """Load and parse template.yaml, returning (flat_sensors, raw_data)."""
    target = _resolve_config_path(base, TEMPLATE_FILE)
    if not target.is_file():
        raise web.HTTPNotFound(text=f"File not found: {TEMPLATE_FILE}")
    with open(target, encoding="utf-8") as f:
        data = yaml.load(f)
    if data is None:
        data = []
    if not isinstance(data, list):
        raise web.HTTPInternalServerError(text="template.yaml must be a top-level list")
    return data, target


def _extract_sensors(data: list[Any]) -> list[dict[str, Any]]:
    """Extract all sensor/binary_sensor defs with their domain and parent index."""
    sensors: list[dict[str, Any]] = []
    for group_idx, group in enumerate(data):
        if not isinstance(group, dict):
            continue
        for domain in ("sensor", "binary_sensor"):
            items = group.get(domain)
            if not isinstance(items, list):
                continue
            for item_idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                uid = item.get("unique_id", "")
                sensors.append(
                    {
                        "unique_id": str(uid),
                        "name": item.get("name", ""),
                        "domain": domain,
                        "state": str(item.get("state", "")),
                        "unit_of_measurement": item.get("unit_of_measurement", ""),
                        "device_class": item.get("device_class", ""),
                        "group_idx": group_idx,
                        "item_idx": item_idx,
                    }
                )
    return sensors


def _save_templates(target: Any, data: list[Any]) -> None:
    """Write template data back to file with backup."""
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


async def get_templates(request: web.Request) -> web.Response:
    """GET /v1/config/templates — list all template sensor definitions."""
    base = request.app["config_base_path"]
    data, _target = _load_templates(base)
    sensors = _extract_sensors(data)
    result = [
        {
            "unique_id": s["unique_id"],
            "name": s["name"],
            "domain": s["domain"],
            "state": s["state"],
            "unit_of_measurement": s["unit_of_measurement"],
            "device_class": s["device_class"],
        }
        for s in sensors
    ]
    return web.json_response({"templates": result})


async def get_template(request: web.Request) -> web.Response:
    """GET /v1/config/template?id=<unique_id> — get single template definition."""
    base = request.app["config_base_path"]
    uid = request.query.get("id", "")
    if not uid:
        raise web.HTTPBadRequest(text="Missing id parameter")

    data, _target = _load_templates(base)
    sensors = _extract_sensors(data)

    for s in sensors:
        if s["unique_id"] == uid:
            group = data[s["group_idx"]]
            item = group[s["domain"]][s["item_idx"]]
            stream = StringIO()
            yaml.dump(item, stream)
            return web.json_response({"unique_id": uid, "content": stream.getvalue()})

    raise web.HTTPNotFound(text=f"Template not found: {uid}")


async def put_template(request: web.Request) -> web.Response:
    """PUT /v1/config/template?id=<unique_id>&dry_run=true — update template definition."""
    base = request.app["config_base_path"]
    uid = request.query.get("id", "")
    dry_run = request.query.get("dry_run", "true").lower() != "false"

    if not uid:
        raise web.HTTPBadRequest(text="Missing id parameter")

    body = await request.text()
    if not body.strip():
        raise web.HTTPBadRequest(text="Request body must not be empty")

    try:
        new_item = yaml.load(StringIO(body))
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"Invalid YAML: {exc}") from exc

    if not isinstance(new_item, dict):
        raise web.HTTPBadRequest(text="Template must be a YAML mapping")

    data, target = _load_templates(base)
    sensors = _extract_sensors(data)

    for s in sensors:
        if s["unique_id"] == uid:
            if dry_run:
                import difflib

                old_stream = StringIO()
                yaml.dump(data[s["group_idx"]][s["domain"]][s["item_idx"]], old_stream)
                new_stream = StringIO()
                yaml.dump(new_item, new_stream)
                diff = "".join(
                    difflib.unified_diff(
                        old_stream.getvalue().splitlines(keepends=True),
                        new_stream.getvalue().splitlines(keepends=True),
                        fromfile=f"a/{uid}",
                        tofile=f"b/{uid}",
                    )
                )
                return web.json_response({"status": "dry_run", "diff": diff})

            data[s["group_idx"]][s["domain"]][s["item_idx"]] = new_item
            _save_templates(target, data)
            return web.json_response({"status": "applied"})

    raise web.HTTPNotFound(text=f"Template not found: {uid}")


async def post_template(request: web.Request) -> web.Response:
    """POST /v1/config/template — create new template sensor."""
    base = request.app["config_base_path"]
    body = await request.text()
    if not body.strip():
        raise web.HTTPBadRequest(text="Request body must not be empty")

    try:
        new_item = yaml.load(StringIO(body))
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"Invalid YAML: {exc}") from exc

    if not isinstance(new_item, dict):
        raise web.HTTPBadRequest(text="Template must be a YAML mapping")

    if "unique_id" not in new_item:
        raise web.HTTPBadRequest(text="Template must have a unique_id")

    domain = request.query.get("domain", "sensor")
    if domain not in ("sensor", "binary_sensor"):
        raise web.HTTPBadRequest(text="domain must be 'sensor' or 'binary_sensor'")

    data, target = _load_templates(base)

    # Check for duplicate unique_id
    sensors = _extract_sensors(data)
    for s in sensors:
        if s["unique_id"] == new_item["unique_id"]:
            raise web.HTTPConflict(text=f"Template with unique_id already exists: {new_item['unique_id']}")

    # Find or create a group for this domain
    for group in data:
        if isinstance(group, dict) and domain in group:
            group[domain].append(new_item)
            _save_templates(target, data)
            return web.json_response({"status": "created", "unique_id": new_item["unique_id"]}, status=201)

    # No existing group for this domain — create one
    data.append({domain: [new_item]})
    _save_templates(target, data)
    return web.json_response({"status": "created", "unique_id": new_item["unique_id"]}, status=201)


async def delete_template(request: web.Request) -> web.Response:
    """DELETE /v1/config/template?id=<unique_id> — delete template sensor."""
    base = request.app["config_base_path"]
    uid = request.query.get("id", "")
    if not uid:
        raise web.HTTPBadRequest(text="Missing id parameter")

    data, target = _load_templates(base)
    sensors = _extract_sensors(data)

    for s in sensors:
        if s["unique_id"] == uid:
            del data[s["group_idx"]][s["domain"]][s["item_idx"]]
            # Clean up empty groups
            group = data[s["group_idx"]]
            if isinstance(group, dict) and not group.get(s["domain"]):
                del group[s["domain"]]
                if not group:
                    data.pop(s["group_idx"])
            _save_templates(target, data)
            return web.json_response({"status": "deleted"})

    raise web.HTTPNotFound(text=f"Template not found: {uid}")


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/config/templates", get_templates),
    RouteDef("GET", "/v1/config/template", get_template),
    RouteDef("PUT", "/v1/config/template", put_template),
    RouteDef("POST", "/v1/config/template", post_template),
    RouteDef("DELETE", "/v1/config/template", delete_template),
]
