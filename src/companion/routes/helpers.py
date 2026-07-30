"""Helper entity CRUD endpoints.

Manages HA helpers (input_boolean, input_number, input_select, input_text,
input_datetime, counter, timer, schedule) via their per-domain YAML files, and
*reads* the storage-backed helpers HA's own UI creates (see
:func:`storage_matches`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from aiohttp import web
from ruamel.yaml import YAML

from companion import core_api
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

#: Helper domains that exist only as UI/storage collections — `input_button` has
#: no YAML schema at all, so it can never appear in `ALLOWED_DOMAINS` (which is
#: the *writable* set) and would otherwise be unreadable here while `helper ls`
#: lists it from the live states.
STORAGE_ONLY_DOMAINS: set[str] = {"input_button"}

#: Every domain whose definitions this service can *read*. Wider than
#: `ALLOWED_DOMAINS` on purpose: read and write have different reach, and the
#: read surface must cover everything the listing shows.
STORAGE_DOMAINS: set[str] = ALLOWED_DOMAINS | STORAGE_ONLY_DOMAINS

#: Where HA keeps a collection's items on disk. Verified against a live 2026.7
#: instance for all nine helper domains: `.storage/<domain>` holds
#: `{"data": {"items": [{"id": ..., <config>}]}}`, and the item id is the
#: entity's object id unless the entity was renamed in the registry.
STORAGE_DIR = ".storage"
ENTITY_REGISTRY_KEY = "core.entity_registry"

#: Prepended to a storage helper's rendered YAML. `helper cat` prints this
#: content verbatim, so for a definition that cannot be edited through this
#: service the marker has to travel *inside* the document — a comment is valid
#: YAML and survives a pipe into a file. The machine-readable half is the
#: `source` field on the response.
STORAGE_CONTENT_HEADER = (
    "# source: storage — created in the Home Assistant UI, not in a YAML file.\n"
    "# Read-only here: helper create/set/delete manage YAML helpers only.\n"
)


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


@dataclass(frozen=True)
class StorageHelper:
    """One helper HA stores in `.storage/<domain>` rather than in a YAML file."""

    domain: str
    item_id: str
    entity_id: str
    config: dict[str, Any]

    def content(self) -> str:
        """The definition rendered the way a YAML helper's would be, plus the marker.

        Same shape as the YAML branch — top-level key is the id, body is the
        rest — so the two sources read alike and the output can be pasted into
        `<domain>.yaml` if the user wants to take the helper over.
        """
        stream = StringIO()
        stream.write(STORAGE_CONTENT_HEADER)
        yaml.dump({self.item_id: {k: v for k, v in self.config.items() if k != "id"}}, stream)
        return stream.getvalue()


def yaml_file_for_domain(domain: str) -> str:
    """Return the YAML config file name for a helper domain."""
    return f"{domain}.yaml"


def _load_helpers_from(target: Path) -> dict[str, Any]:
    """Load a helper YAML file's dict content from an explicit path.

    If the file doesn't exist, returns an empty dict.
    """
    if not target.is_file():
        return {}
    with open(target, encoding="utf-8") as f:
        data = yaml.load(f)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise web.HTTPInternalServerError(text=f"{target.name} must be a top-level mapping")
    return data


def _load_helpers(base: str, domain: str) -> tuple[dict[str, Any], Any]:
    """Load a helper YAML file, returning (data_dict, file_path).

    Helper files are top-level mappings keyed by entity slug. The file is the
    one ``configuration.yaml`` wires the domain to, falling back to the
    conventional ``<domain>.yaml`` when the wiring cannot be established — used
    by the read/update/delete paths, which operate on helpers that (by
    definition) already exist. Resolving through the same function the create
    path uses is what stops a helper created in ``my_booleans.yaml`` from being
    invisible to the ``helper ls`` that follows it.
    """
    target = wired_target_or_default(base, domain, yaml_file_for_domain(domain))
    return _load_helpers_from(target), target


async def _poll_entity_created(entity_id: str, attempts: int = 5, delay: float = 0.4) -> bool:
    """Poll /api/states until entity_id appears, or give up after `attempts` tries."""
    import asyncio

    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(delay)
        if await core_api.get_state(entity_id) is not None:
            return True
    return False


def _save_helpers(target: Any, data: dict[str, Any]) -> None:
    """Write helper data back to file with backup."""
    from pathlib import Path

    from companion.backups import make_backup

    path = Path(target)
    make_backup(path)

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f)


def _validate_domain(domain: str) -> None:
    """Raise 400 if domain is not an allowed helper domain."""
    if domain not in ALLOWED_DOMAINS:
        raise web.HTTPBadRequest(text=f"Invalid helper domain: {domain}. Allowed: {', '.join(sorted(ALLOWED_DOMAINS))}")


def _storage_json(base: str, key: str) -> dict[str, Any]:
    """The ``data`` object of ``.storage/<key>``, or ``{}`` if unreadable.

    Best-effort on purpose: `.storage` belongs to Home Assistant, and a file
    that is mid-write, absent, or from a future schema must degrade this lookup
    to "no storage helpers", never turn a read route into a 500.
    """
    path = Path(base) / STORAGE_DIR / key
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    data = raw.get("data") if isinstance(raw, dict) else None
    return data if isinstance(data, dict) else {}


def _entity_ids_by_unique_id(base: str) -> dict[tuple[str, str], str]:
    """``(platform, unique_id) -> entity_id`` from HA's entity registry.

    A UI helper registers under a unique_id equal to its collection item id, so
    this is what turns an item back into the entity_id the user sees. It is not
    cosmetic: renaming the entity changes the entity_id and leaves the item id
    alone, so ``input_boolean.<item id>`` is a *guess* and the registry is the
    fact (verified live — the rename was performed and observed).
    """
    entities = _storage_json(base, ENTITY_REGISTRY_KEY).get("entities")
    if not isinstance(entities, list):
        return {}
    index: dict[tuple[str, str], str] = {}
    for entry in entities:
        if not isinstance(entry, dict):
            continue
        platform, unique_id, entity_id = entry.get("platform"), entry.get("unique_id"), entry.get("entity_id")
        if isinstance(platform, str) and isinstance(unique_id, str) and isinstance(entity_id, str):
            index[(platform, unique_id)] = entity_id
    return index


def storage_helpers(base: str) -> list[StorageHelper]:
    """Every helper HA keeps in a `.storage` collection, across all read domains."""
    entity_ids = _entity_ids_by_unique_id(base)
    found: list[StorageHelper] = []
    for domain in sorted(STORAGE_DOMAINS):
        items = _storage_json(base, domain).get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            item_id = item["id"]
            found.append(
                StorageHelper(
                    domain=domain,
                    item_id=item_id,
                    entity_id=entity_ids.get((domain, item_id), f"{domain}.{item_id}"),
                    config=item,
                )
            )
    return found


def storage_matches(base: str, helper_id: str, domain: str | None = None) -> list[StorageHelper]:
    """Every storage-backed helper ``helper_id`` can name.

    Three forms are accepted, because all three are identifiers this service or
    hactl prints for such a helper (H-17): the live ``entity_id`` (what
    ``helper ls`` shows for a storage row), the bare collection id (what the
    equivalent YAML helper's top-level key would be), and ``<domain>.<item id>``
    (the entity_id before anyone renamed it).
    """
    matches = [
        helper
        for helper in storage_helpers(base)
        if helper_id in {helper.entity_id, helper.item_id, f"{helper.domain}.{helper.item_id}"}
    ]
    if domain:
        matches = [helper for helper in matches if helper.domain == domain]
    return matches


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


def _storage_ambiguous(helper_id: str, matches: list[StorageHelper]) -> web.HTTPConflict:
    domains = ", ".join(sorted(helper.domain for helper in matches))
    return web.HTTPConflict(
        text=(
            f"Helper id '{helper_id}' is ambiguous across storage-backed domains: {domains}. "
            f"Retry with the full entity_id (e.g. '{matches[0].entity_id}')."
        )
    )


def _not_found(helper_id: str) -> web.HTTPNotFound:
    """404 that says where we looked — 'not found' alone reads as 'does not exist'.

    On a UI-managed instance every helper is storage-backed, so a bare 'not
    found' was the answer to *every* lookup while `helper ls` listed the same
    helpers happily.
    """
    return web.HTTPNotFound(
        text=(
            f"Helper not found: {helper_id} — searched the YAML helper files "
            f"({', '.join(sorted(yaml_file_for_domain(d) for d in ALLOWED_DOMAINS))}) and HA's "
            f"{STORAGE_DIR} collections ({', '.join(sorted(STORAGE_DOMAINS))}). A helper created in the UI "
            f"seconds ago may not be in {STORAGE_DIR} yet — Home Assistant writes those files on a delay."
        )
    )


async def get_helper(request: web.Request) -> web.Response:
    """GET /v1/config/helper?id=<id> — get single helper definition.

    Answers for both sources a helper can have. YAML helpers (this service's
    own files) are addressed by their top-level key; storage helpers — the ones
    HA's UI creates, which on a normally-configured instance are *all* of them —
    are addressed by entity_id or by their collection id, and come back with
    ``source: storage`` and a rendered, read-only definition. Listing a helper
    and then refusing to show it was a contradiction inside one command family.
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
            return web.json_response(
                {"id": helper_id, "domain": domain, "content": stream.getvalue(), "source": "yaml"}
            )

    matches = storage_matches(base, helper_id)
    if len(matches) > 1:
        raise _storage_ambiguous(helper_id, matches)
    if matches:
        helper = matches[0]
        return web.json_response(
            {
                "id": helper.entity_id,
                "domain": helper.domain,
                "content": helper.content(),
                "source": "storage",
            }
        )

    raise _not_found(helper_id)


async def post_helper(request: web.Request) -> web.Response:
    """POST /v1/config/helper?domain=<domain> — create new helper."""
    base = request.app["config_base_path"]
    domain = request.query.get("domain", "")
    if not domain:
        raise web.HTTPBadRequest(text="Missing domain parameter")
    _validate_domain(domain)
    # C-10: prove HA reads this file before writing a new helper into it.
    target = require_wired_target(base, domain, yaml_file_for_domain(domain))

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

    data = _load_helpers_from(target)
    if helper_id in data:
        raise web.HTTPConflict(text=f"Helper already exists: {helper_id}")

    data[helper_id] = helper_body
    _save_helpers(target, data)
    reload = await core_api.reload_domain(domain)
    entity_id = f"{domain}.{helper_id}"
    entity_created = await _poll_entity_created(entity_id) if reload.ok else False
    return web.json_response(
        {
            "status": "created",
            "id": helper_id,
            "entity_id": entity_id,
            **core_api.reload_fields(reload),
            "entity_created": entity_created,
        },
        status=201,
    )


def _refuse_storage_write(base: str, helper_id: str, domain: str | None) -> None:
    """Raise 409 if ``helper_id`` names a storage-backed helper; return otherwise."""
    matches = storage_matches(base, helper_id, domain)
    if not matches:
        return
    helper = matches[0]
    raise web.HTTPConflict(
        text=(
            f"Helper '{helper_id}' is storage-backed: {helper.entity_id} was created in the Home Assistant UI "
            f"and lives in {STORAGE_DIR}/{helper.domain}, so there is no YAML definition to edit or delete. "
            f"Change it in the UI (or via HA's {helper.domain}/update / {helper.domain}/delete WebSocket "
            f"command). GET /v1/config/helper reads it."
        )
    )


def _locate_helper(base: str, helper_id: str, domain: str | None) -> tuple[str, dict[str, Any], Any]:
    """Locate the domain file that owns ``helper_id`` for update/delete.

    With an explicit ``domain`` only that domain is considered. Without one, all
    domains are scanned. The same slug can legitimately exist in two domains
    (e.g. ``counter.kitchen`` and ``input_boolean.kitchen``), so scanning and
    silently acting on the alphabetically-first match would touch the wrong
    entity — instead the ambiguity is surfaced as a 409 listing the candidates.

    A storage-backed helper reaching here is refused with 409 naming the
    mechanism, not 404: GET now resolves it, so a bare "not found" from the
    write half of the same family would be the read/write contradiction the
    other way round. There is nothing in a YAML file to edit or delete.

    Returns ``(domain, data_dict, file_path)``.
    """
    if domain:
        _validate_domain(domain)
        data, target = _load_helpers(base, domain)
        if helper_id in data:
            return domain, data, target
        _refuse_storage_write(base, helper_id, domain)
        raise web.HTTPNotFound(text=f"Helper not found: {helper_id} (domain {domain})")

    matches: list[tuple[str, dict[str, Any], Any]] = []
    for candidate in sorted(ALLOWED_DOMAINS):
        data, target = _load_helpers(base, candidate)
        if helper_id in data:
            matches.append((candidate, data, target))

    if not matches:
        _refuse_storage_write(base, helper_id, None)
        raise _not_found(helper_id)
    if len(matches) > 1:
        domains = ", ".join(candidate for candidate, _, _ in matches)
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

    domain, data, target = _locate_helper(base, helper_id, domain_param)
    data[helper_id] = new_body
    _save_helpers(target, data)
    reload = await core_api.reload_domain(domain)
    return web.json_response({"status": "applied", **core_api.reload_fields(reload)})


async def delete_helper(request: web.Request) -> web.Response:
    """DELETE /v1/config/helper?id=<id>[&domain=<domain>] — delete helper."""
    base = request.app["config_base_path"]
    helper_id = request.query.get("id", "")
    if not helper_id:
        raise web.HTTPBadRequest(text="Missing id parameter")
    domain_param = request.query.get("domain") or None

    domain, data, target = _locate_helper(base, helper_id, domain_param)
    del data[helper_id]
    _save_helpers(target, data)
    reload = await core_api.reload_domain(domain)
    return web.json_response({"status": "deleted", **core_api.reload_fields(reload)})


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/config/helpers", get_helpers),
    RouteDef("GET", "/v1/config/helper", get_helper),
    RouteDef("PUT", "/v1/config/helper", put_helper),
    RouteDef("POST", "/v1/config/helper", post_helper),
    RouteDef("DELETE", "/v1/config/helper", delete_helper),
]
