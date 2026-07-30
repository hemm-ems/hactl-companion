"""Template sensor CRUD endpoints.

Home Assistant's modern ``template:`` integration is a top-level YAML *list of
blocks*. A block may be **state-based** (just entity domains like ``sensor:`` /
``binary_sensor:``) or **trigger-based** (``triggers:``/``actions:``/
``conditions:`` at the block level, siblings of the entity domains — never
inside an entity). A single block can also declare multiple entity domains that
share one trigger set.

This module keeps that block structure intact. In particular ``post_template``
must not (a) nest a trigger inside an entity item, nor (b) merge a plain
state-based entity into an existing trigger-based block — both corrupt the file.
"""

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

TEMPLATE_DOMAIN = "template"
TEMPLATE_FILE = "template.yaml"

# Entity-domain keys a template block can declare. The presence of any of these
# at the top level of a mapping is what makes it a "block" rather than a bare
# entity item.
_ENTITY_DOMAINS = (
    "sensor",
    "binary_sensor",
    "number",
    "select",
    "button",
    "image",
    "weather",
    "light",
    "switch",
    "lock",
    "cover",
    "fan",
    "device_tracker",
    "event",
    "alarm_control_panel",
    "update",
    "vacuum",
)

# Block-level keys (both the modern plural and legacy singular spellings) that
# make a block trigger-based. A plain state-based entity must never be merged
# into a block carrying any of these.
_BLOCK_TRIGGER_KEYS = (
    "trigger",
    "triggers",
    "action",
    "actions",
    "condition",
    "conditions",
)


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


def _load_templates(base: str) -> tuple[list[Any], Path, str]:
    """Load and parse the template file, returning (raw_data, target_path, source_text).

    The file is whichever one ``configuration.yaml`` wires ``template:`` to,
    falling back to the conventional name when that cannot be established.
    """
    return _load_templates_from(base, wired_target_or_default(base, TEMPLATE_DOMAIN, TEMPLATE_FILE))


def _load_templates_from(base: str, target: Path) -> tuple[list[Any], Path, str]:
    """Load and parse an explicit template file, returning (raw_data, path, source_text).

    The source text travels with the parsed data so a write can rewrite only the
    block it was asked to change (see :mod:`companion.surgical`). The unit here is
    the top-level *block*, not the entity inside it: editing one entity in a
    multi-entity block re-serializes that block and nothing else.
    """
    if not target.is_file():
        raise web.HTTPNotFound(text=f"File not found: {target.name}")
    source = read_source(base, target)
    data = yaml.load(StringIO(source))
    if data is None:
        data = []
    if not isinstance(data, list):
        raise web.HTTPInternalServerError(text="template.yaml must be a top-level list")
    return data, target, source


def _block_is_trigger_based(block: dict[str, Any]) -> bool:
    """True if the block carries a block-level trigger/action/condition."""
    return any(k in block for k in _BLOCK_TRIGGER_KEYS)


def _is_block(item: dict[str, Any]) -> bool:
    """True if ``item`` is a full template block (declares an entity domain)."""
    return any(d in item for d in _ENTITY_DOMAINS)


def _block_unique_ids(block: dict[str, Any]) -> list[str]:
    """All ``unique_id``s declared by the entities in a block."""
    ids: list[str] = []
    for domain in _ENTITY_DOMAINS:
        items = block.get(domain)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and "unique_id" in item:
                ids.append(str(item["unique_id"]))
    return ids


def _all_unique_ids(data: list[Any]) -> set[str]:
    """Every ``unique_id`` across every block/domain in the file."""
    ids: set[str] = set()
    for block in data:
        if isinstance(block, dict):
            ids.update(_block_unique_ids(block))
    return ids


def _extract_entities(data: list[Any]) -> list[dict[str, Any]]:
    """Extract every template entity def with its domain and parent index.

    Walks all ``_ENTITY_DOMAINS`` so entities in any template domain (not just
    sensor/binary_sensor) are visible to get/list/update/delete. The
    ``state``/``unit_of_measurement``/``device_class`` fields are sensor-oriented
    and left empty for domains that don't declare them.
    """
    entities: list[dict[str, Any]] = []
    for group_idx, group in enumerate(data):
        if not isinstance(group, dict):
            continue
        trigger_based = _block_is_trigger_based(group)
        for domain in _ENTITY_DOMAINS:
            items = group.get(domain)
            if not isinstance(items, list):
                continue
            for item_idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                uid = item.get("unique_id", "")
                entities.append(
                    {
                        "unique_id": str(uid),
                        "name": item.get("name", ""),
                        "domain": domain,
                        "state": str(item.get("state", "")),
                        "unit_of_measurement": item.get("unit_of_measurement", ""),
                        "device_class": item.get("device_class", ""),
                        "trigger": trigger_based,
                        "group_idx": group_idx,
                        "item_idx": item_idx,
                    }
                )
    return entities


async def get_templates(request: web.Request) -> web.Response:
    """GET /v1/config/templates — list all template sensor definitions."""
    base = request.app["config_base_path"]
    data, _target, _source = _load_templates(base)
    entities = _extract_entities(data)
    result = [
        {
            "unique_id": s["unique_id"],
            "name": s["name"],
            "domain": s["domain"],
            "state": s["state"],
            "unit_of_measurement": s["unit_of_measurement"],
            "device_class": s["device_class"],
            "trigger": s["trigger"],
        }
        for s in entities
    ]
    return web.json_response({"templates": result})


async def get_template(request: web.Request) -> web.Response:
    """GET /v1/config/template?id=<unique_id> — get single template definition.

    For a trigger-based entry the returned ``content`` is the whole block
    (trigger + entity) so the trigger is visible, not just the entity item.
    """
    base = request.app["config_base_path"]
    uid = request.query.get("id", "")
    if not uid:
        raise web.HTTPBadRequest(text="Missing id parameter")

    data, _target, _source = _load_templates(base)
    entities = _extract_entities(data)

    for s in entities:
        if s["unique_id"] == uid:
            group = data[s["group_idx"]]
            payload = group if s["trigger"] else group[s["domain"]][s["item_idx"]]
            stream = StringIO()
            yaml.dump(payload, stream)
            return web.json_response({"unique_id": uid, "content": stream.getvalue(), "trigger": s["trigger"]})

    raise web.HTTPNotFound(text=f"Template not found: {uid}")


async def put_template(request: web.Request) -> web.Response:
    """PUT /v1/config/template?id=<unique_id>&dry_run=true — update template definition."""
    base = request.app["config_base_path"]
    uid = request.query.get("id", "")
    dry_run = parse_bool_param(request, "dry_run", default=True)

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

    # PUT replaces a single entity item in place. A full block or a stray
    # block-level key here would overwrite the entity with the wrong shape.
    if _is_block(new_item):
        raise web.HTTPBadRequest(text="Update replaces a single entity; pass the entity mapping, not a full block")
    stray = next((k for k in _BLOCK_TRIGGER_KEYS if k in new_item), None)
    if stray is not None:
        raise web.HTTPBadRequest(text=f"'{stray}' belongs at the block level, not inside an entity item")

    data, target, source = _load_templates(base)
    entities = _extract_entities(data)

    for s in entities:
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
            surgical = save_entry(base, target, data, source, Edit("replace", s["group_idx"]), yaml)
            reload = await core_api.reload_domain("template")
            return web.json_response({"status": "applied", **write_fields(surgical), **core_api.reload_fields(reload)})

    raise web.HTTPNotFound(text=f"Template not found: {uid}")


async def post_template(request: web.Request) -> web.Response:
    """POST /v1/config/template — create a new template entry.

    Accepts two input shapes:

    * a **bare entity item** (``unique_id`` + ``state`` + …), placed into a
      state-based block for ``?domain=`` (the legacy shape); or
    * a **full block** (declares any template entity domain — ``sensor:``,
      ``number:``, ``select:``, ``button:``, ``weather:``, … — optionally with
      block-level ``triggers:``/``actions:``/``conditions:``), appended verbatim
      as its own new top-level list item — this is how trigger-based and
      multi-domain entries are authored.
    """
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

    # C-10: a template.yaml no `template:` key !include's is written happily and
    # never read — the entity simply never appears. Prove HA reads this file
    # before claiming to have created anything in it.
    data, target, source = _load_templates_from(base, require_wired_target(base, TEMPLATE_DOMAIN, TEMPLATE_FILE))
    existing_ids = _all_unique_ids(data)

    if _is_block(new_item):
        first_uid, edit = _create_block(data, new_item, existing_ids)
    else:
        first_uid, edit = _create_bare_item(request, data, new_item, existing_ids)

    surgical = save_entry(base, target, data, source, edit, yaml)
    reload = await core_api.reload_domain("template")
    return web.json_response(
        {"status": "created", "unique_id": first_uid, **write_fields(surgical), **core_api.reload_fields(reload)},
        status=201,
    )


def _create_block(data: list[Any], block: dict[str, Any], existing_ids: set[str]) -> tuple[str, Edit]:
    """Append a full block verbatim as its own list item.

    Returns ``(first unique_id, the edit it made)`` — the write path needs to be
    told which top-level block changed so it can rewrite only that one.
    """
    new_ids = _block_unique_ids(block)
    if not new_ids:
        raise web.HTTPBadRequest(text="Template block must define at least one entity with a unique_id")

    dup = next((u for u in new_ids if u in existing_ids), None)
    if dup is not None:
        raise web.HTTPConflict(text=f"Template with unique_id already exists: {dup}")

    data.append(block)
    return new_ids[0], Edit("append")


def _create_bare_item(
    request: web.Request, data: list[Any], item: dict[str, Any], existing_ids: set[str]
) -> tuple[str, Edit]:
    """Place a bare entity item into a state-based block.

    Returns ``(unique_id, the edit it made)``: merging into an existing block
    rewrites that block, opening a new one appends.
    """
    if "unique_id" not in item:
        raise web.HTTPBadRequest(text="Template must have a unique_id")

    # A bare item carrying a block-level key is the classic corruption trap:
    # HA rejects a trigger nested inside an entity. Reject with guidance.
    stray = next((k for k in _BLOCK_TRIGGER_KEYS if k in item), None)
    if stray is not None:
        raise web.HTTPBadRequest(
            text=f"'{stray}' belongs at the block level, not inside an entity item. "
            f"Supply a full block instead, e.g. 'triggers: [...]' alongside 'sensor: [ {{...}} ]'."
        )

    domain = request.query.get("domain", "sensor")
    if domain not in _ENTITY_DOMAINS:
        raise web.HTTPBadRequest(text=f"'{domain}' is not a template entity domain")

    uid = str(item["unique_id"])
    if uid in existing_ids:
        raise web.HTTPConflict(text=f"Template with unique_id already exists: {uid}")

    # Merge into the first STATE-BASED block that already has this domain; skip
    # trigger-based blocks so a plain entity never gets bound to a trigger.
    for group_idx, group in enumerate(data):
        if isinstance(group, dict) and domain in group and not _block_is_trigger_based(group):
            group[domain].append(item)
            return uid, Edit("replace", group_idx)
    data.append({domain: [item]})
    return uid, Edit("append")


async def delete_template(request: web.Request) -> web.Response:
    """DELETE /v1/config/template?id=<unique_id> — delete template sensor."""
    base = request.app["config_base_path"]
    uid = request.query.get("id", "")
    if not uid:
        raise web.HTTPBadRequest(text="Missing id parameter")

    data, target, source = _load_templates(base)
    entities = _extract_entities(data)

    for s in entities:
        if s["unique_id"] == uid:
            group = data[s["group_idx"]]
            del group[s["domain"]][s["item_idx"]]
            # Drop the now-empty domain key.
            if not group[s["domain"]]:
                del group[s["domain"]]
            # If no entity domains remain, drop the whole block — including any
            # orphaned trigger/action, which HA would otherwise reject as an
            # incomplete template configuration.
            edit = Edit("replace", s["group_idx"])
            if isinstance(group, dict) and not any(d in group for d in _ENTITY_DOMAINS):
                data.pop(s["group_idx"])
                edit = Edit("delete", s["group_idx"])
            surgical = save_entry(base, target, data, source, edit, yaml)
            reload = await core_api.reload_domain("template")
            return web.json_response({"status": "deleted", **write_fields(surgical), **core_api.reload_fields(reload)})

    raise web.HTTPNotFound(text=f"Template not found: {uid}")


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/config/templates", get_templates),
    RouteDef("GET", "/v1/config/template", get_template),
    RouteDef("PUT", "/v1/config/template", put_template),
    RouteDef("POST", "/v1/config/template", post_template),
    RouteDef("DELETE", "/v1/config/template", delete_template),
]
