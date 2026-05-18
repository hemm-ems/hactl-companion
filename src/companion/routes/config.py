"""YAML config read/write endpoints."""

from __future__ import annotations

import difflib
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web
from ruamel.yaml import YAML

from companion.yaml_resolver import YamlResolver

# Files that must never be exposed
DENIED_FILES: set[str] = {"secrets.yaml"}

yaml = YAML()
yaml.preserve_quotes = True


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


def _resolve_config_path(base: str, relative: str) -> Path:
    """Resolve and validate a config path, preventing traversal attacks."""
    if not relative:
        raise web.HTTPBadRequest(text="Missing path parameter")

    base_path = Path(base).resolve()
    target = (base_path / relative).resolve()

    if not str(target).startswith(str(base_path)):
        raise web.HTTPBadRequest(text="Path traversal is not allowed")

    filename = target.name.lower()
    if filename in DENIED_FILES:
        raise web.HTTPForbidden(text=f"Access to {filename} is denied")

    return target


async def get_config_files(request: web.Request) -> web.Response:
    """GET /v1/config/files — list all YAML files in /config."""
    base = request.app["config_base_path"]
    base_path = Path(base)

    if not base_path.is_dir():
        raise web.HTTPNotFound(text="Config directory not found")

    files: list[str] = []
    for root, dirs, filenames in os.walk(base_path, followlinks=True):
        # Skip hidden/internal directories
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in sorted(filenames):
            if fname.endswith((".yaml", ".yml")) and fname.lower() not in DENIED_FILES:
                rel = os.path.relpath(os.path.join(root, fname), base_path)
                files.append(rel.replace("\\", "/"))

    return web.json_response({"files": sorted(files)})


async def get_config_file(request: web.Request) -> web.Response:
    """GET /v1/config/file?path=...&resolve=true|false — read a whole YAML file."""
    base = request.app["config_base_path"]
    rel_path = request.query.get("path", "")
    resolve = request.query.get("resolve", "true").lower() != "false"
    target = _resolve_config_path(base, rel_path)

    if not target.is_file():
        raise web.HTTPNotFound(text=f"File not found: {rel_path}")

    if resolve:
        resolver = YamlResolver(base)
        try:
            data = resolver.load(rel_path, resolve=True)
            content = target.read_text(encoding="utf-8") if data is None else resolver.dump_to_string(data)
        except (PermissionError, ValueError) as exc:
            raise web.HTTPForbidden(text=str(exc)) from exc
        except FileNotFoundError as exc:
            raise web.HTTPNotFound(text=str(exc)) from exc
    else:
        content = target.read_text(encoding="utf-8")

    return web.json_response({"path": rel_path, "content": content})


async def get_config_block(request: web.Request) -> web.Response:
    """GET /v1/config/block?path=...&id=... — read a specific block from a YAML file."""
    base = request.app["config_base_path"]
    rel_path = request.query.get("path", "")
    block_id = request.query.get("id", "")

    if not block_id:
        raise web.HTTPBadRequest(text="Missing id parameter")

    target = _resolve_config_path(base, rel_path)

    if not target.is_file():
        raise web.HTTPNotFound(text=f"File not found: {rel_path}")

    with open(target, encoding="utf-8") as f:
        data = yaml.load(f)

    if data is None:
        raise web.HTTPNotFound(text=f"Block not found: {block_id}")

    # Search for block by id or alias in list-type configs
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                item_id = item.get("id") or item.get("alias")
                if item_id == block_id:
                    from io import StringIO

                    stream = StringIO()
                    yaml.dump(item, stream)
                    return web.json_response({"path": rel_path, "id": block_id, "content": stream.getvalue()})

    # Search in dict-type configs
    if isinstance(data, dict) and block_id in data:
        from io import StringIO

        stream = StringIO()
        yaml.dump({block_id: data[block_id]}, stream)
        return web.json_response({"path": rel_path, "id": block_id, "content": stream.getvalue()})

    raise web.HTTPNotFound(text=f"Block not found: {block_id}")


async def put_config_file(request: web.Request) -> web.Response:
    """PUT /v1/config/file?path=...&dry_run=true|false — write a YAML config file."""
    base = request.app["config_base_path"]
    rel_path = request.query.get("path", "")
    dry_run = request.query.get("dry_run", "true").lower() != "false"

    target = _resolve_config_path(base, rel_path)
    new_content = await request.text()

    if not new_content.strip():
        raise web.HTTPBadRequest(text="Request body must not be empty")

    # Validate that the content is valid YAML
    try:
        from io import StringIO

        yaml.load(StringIO(new_content))
    except Exception as exc:
        raise web.HTTPBadRequest(text=f"Invalid YAML: {exc}") from exc

    # Read existing content for diff
    old_content = ""
    if target.is_file():
        old_content = target.read_text(encoding="utf-8")

    if dry_run:
        diff = difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        )
        diff_text = "".join(diff)
        return web.json_response({"status": "dry_run", "diff": diff_text})

    # Create backup
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    backup_name = f"{target.name}.bak.{timestamp}"
    backup_path = target.parent / backup_name

    if target.is_file():
        shutil.copy2(target, backup_path)

    # Write new content
    target.write_text(new_content, encoding="utf-8")

    # Validate via ha core check-config (if available)
    validation_result = await _validate_config()

    if validation_result is not None and not validation_result["valid"]:
        # Restore backup on validation failure
        if backup_path.is_file():
            shutil.copy2(backup_path, target)
        raise web.HTTPBadRequest(text=f"Config validation failed: {validation_result['error']}. Backup restored.")

    return web.json_response(
        {
            "status": "applied",
            "backup": backup_name,
        }
    )


async def _validate_config() -> dict[str, object] | None:
    """Run ha core check-config if available. Returns None if ha CLI not available."""
    import asyncio

    try:
        proc = await asyncio.create_subprocess_exec(
            "ha",
            "core",
            "check-config",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            return {"valid": True, "error": None}
        return {"valid": False, "error": stderr.decode("utf-8", errors="replace").strip()}
    except (FileNotFoundError, TimeoutError):
        # ha CLI not available or timed out — skip validation
        return None


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/config/files", get_config_files),
    RouteDef("GET", "/v1/config/file", get_config_file),
    RouteDef("GET", "/v1/config/block", get_config_block),
    RouteDef("PUT", "/v1/config/file", put_config_file),
]
