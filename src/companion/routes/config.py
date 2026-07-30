"""YAML config read/write endpoints."""

from __future__ import annotations

import difflib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from aiohttp import web
from ruamel.yaml import YAML

from companion import core_api
from companion.backups import backup_dir, make_backup
from companion.params import parse_bool_param
from companion.pathguard import is_denied, is_within
from companion.yaml_resolver import YamlResolver

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

    if not is_within(target, base_path):
        raise web.HTTPBadRequest(text="Path traversal is not allowed")

    if is_denied(target.name):
        raise web.HTTPForbidden(text=f"Access to {target.name} is denied")

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
            if fname.endswith((".yaml", ".yml")) and not is_denied(fname):
                rel = os.path.relpath(os.path.join(root, fname), base_path)
                files.append(rel.replace("\\", "/"))

    return web.json_response({"files": sorted(files)})


async def get_config_file(request: web.Request) -> web.Response:
    """GET /v1/config/file?path=...&resolve=true|false — read a whole YAML file."""
    base = request.app["config_base_path"]
    rel_path = request.query.get("path", "")
    resolve = parse_bool_param(request, "resolve", default=True)
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

    # Search for block by id or alias in list-type configs. This scan runs
    # BEFORE the index fallback below on purpose: HA's UI mints purely
    # numeric automation ids (millisecond timestamps), so a bare number must
    # keep resolving as the id it always was — a printed id that stopped
    # resolving would be the H-17 failure. Index addressing loses that tie;
    # the bracketed form (`[3]`) never collides with an id.
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                item_id = item.get("id") or item.get("alias")
                if item_id == block_id:
                    from io import StringIO

                    stream = StringIO()
                    yaml.dump(item, stream)
                    return web.json_response({"path": rel_path, "id": block_id, "content": stream.getvalue()})

    # A list-rooted file is also addressable by position, bare (`3`) or
    # bracketed (`[3]`) — the exact prefix `ref scan` prints in its paths, so
    # a printed address pastes back (hemm-ems/hactl#107 / hactl D-13). For
    # template.yaml this is the FIRST working address: its blocks carry
    # neither id nor alias. Deliberately the only new scheme — nested
    # unique_ids stay `tpl cat`'s job, not a fourth addressing form here.
    index_match = re.fullmatch(r"\[?(\d+)\]?", block_id)
    if index_match and isinstance(data, list):
        index = int(index_match.group(1))
        if index >= len(data):
            raise web.HTTPNotFound(
                text=(
                    f"Block not found: {block_id} (index out of range; "
                    f"{rel_path} has {len(data)} blocks, 0..{len(data) - 1})"
                )
            )
        from io import StringIO

        stream = StringIO()
        yaml.dump(data[index], stream)
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
    dry_run = parse_bool_param(request, "dry_run", default=True)

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

    # Back up the existing file (a brand-new file has nothing to back up — this
    # distinction drives the rollback path below).
    backup_name = make_backup(target)
    existed = backup_name is not None
    backup_path = backup_dir(target) / backup_name if backup_name is not None else None

    # Write new content
    target.write_text(new_content, encoding="utf-8")

    # Validate the now-on-disk config via the HA core API — the same path as
    # POST /v1/ha/check-config, so the two validation routes cannot disagree.
    outcome = await _validate_written_config()

    if outcome.status == "invalid":
        _rollback(target, backup_path, existed)
        raise web.HTTPBadRequest(text=f"Config validation failed: {outcome.detail}. {_rollback_note(existed)}")
    if outcome.status == "unavailable":
        # A present-but-unreachable/slow validator must NOT silently un-gate the
        # write. Roll back and surface it as a distinct, non-skipping outcome.
        _rollback(target, backup_path, existed)
        raise web.HTTPServiceUnavailable(
            text=f"Config validation could not run: {outcome.detail}. {_rollback_note(existed)}"
        )

    response: dict[str, object] = {"status": "applied", "validated": outcome.status == "ok"}
    if backup_name is not None:
        response["backup"] = backup_name
    return web.json_response(response)


def _rollback(target: Path, backup_path: Path | None, existed: bool) -> None:
    """Undo a just-written file: restore the backup, or remove a brand-new file."""
    if existed and backup_path is not None and backup_path.is_file():
        shutil.copy2(backup_path, target)
    elif not existed:
        target.unlink(missing_ok=True)


def _rollback_note(existed: bool) -> str:
    return "Backup restored." if existed else "New file removed."


class _ValidationOutcome(NamedTuple):
    status: str  # "ok" | "invalid" | "skipped" | "unavailable"
    detail: str


async def _validate_written_config() -> _ValidationOutcome:
    """Validate the on-disk config via the HA core API.

    Outcomes:
      - ``ok``          — config is valid.
      - ``invalid``     — config is invalid (``detail`` carries the errors).
      - ``skipped``     — no validator available (SUPERVISOR_TOKEN unset, e.g. a
                          dev/no-supervisor stack); the write is allowed through.
      - ``unavailable`` — a validator should exist but the check could not run
                          (core API unreachable, HTTP error, or timeout). This is
                          deliberately distinct from ``skipped`` so a slow/failing
                          check cannot silently un-gate the write.
    """
    if not os.environ.get("SUPERVISOR_TOKEN"):
        return _ValidationOutcome("skipped", "")
    try:
        valid, errors = await core_api.check_config()
    except core_api.CoreAPIUnavailableError as exc:
        return _ValidationOutcome("unavailable", str(exc))
    return _ValidationOutcome("ok" if valid else "invalid", "" if valid else errors)


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/config/files", get_config_files),
    RouteDef("GET", "/v1/config/file", get_config_file),
    RouteDef("GET", "/v1/config/block", get_config_block),
    RouteDef("PUT", "/v1/config/file", put_config_file),
]
