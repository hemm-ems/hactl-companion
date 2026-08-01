"""Helper entity CRUD endpoints.

Manages HA helpers (input_boolean, input_number, input_select, input_text,
input_datetime, counter, timer, schedule) via their per-domain YAML files, and
*reads* the storage-backed helpers HA's own UI creates (see
:func:`storage_matches`).
"""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from aiohttp import web
from ruamel.yaml import YAML

from companion import core_api, registry
from companion.surgical import Edit, read_source, save_entry, write_fields
from companion.wiring import DomainFile, readable_domain_files, require_wired_target, wired_target_or_default

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
STORAGE_DIR = registry.STORAGE_DIR

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


def _load_helpers_from(base: str, target: Path, key_path: tuple[str, ...] = ()) -> tuple[dict[str, Any], str]:
    """Load a helper YAML file's dict content from an explicit path.

    Returns ``(data, source_text)``; an absent file is an empty mapping and empty
    text. The source travels with the parsed data so a write can rewrite only the
    entry it was asked to change (see :mod:`companion.surgical`).

    ``key_path`` names where inside the document the helper mapping sits — empty
    for a file whose root is the domain's config, one key deep for an inline
    domain or a package.
    """
    if not target.is_file():
        return {}, ""
    source = read_source(base, target)
    data = yaml.load(StringIO(source))
    if data is None:
        data = {}
    if key_path:
        return _in_file(data if isinstance(data, dict) else None, key_path), source
    if not isinstance(data, dict):
        raise web.HTTPInternalServerError(text=f"{target.name} must be a top-level mapping")
    return data, source


def _load_helpers(base: str, domain: str) -> tuple[dict[str, Any], Path, str]:
    """Load a helper YAML file, returning (data_dict, file_path, source_text).

    The single-file view, kept for the create path and for callers that need the
    source text to splice. Reads go through :func:`_load_helpers_everywhere`,
    which sees all four wirings; this one sees the file a new entry may be
    appended to.
    """
    target = wired_target_or_default(base, domain, yaml_file_for_domain(domain))
    data, source = _load_helpers_from(base, target)
    return data, target, source


def _in_file(data: dict[str, Any] | None, key_path: tuple[str, ...]) -> dict[str, Any]:
    """Descend ``key_path`` into a parsed document, or return an empty mapping.

    An inline domain and a package put the helper mapping one key down; an
    include target has it at the root. Anything that is not a mapping where a
    mapping was expected reads as absent rather than raising: this runs over
    files the caller did not name, and one odd document must not turn a listing
    into a 500.
    """
    node: Any = data
    for key in key_path:
        if not isinstance(node, dict):
            return {}
        node = node.get(key)
    return node if isinstance(node, dict) else {}


def _load_helpers_everywhere(base: str, domain: str) -> list[tuple[dict[str, Any], DomainFile, str]]:
    """Every file holding helpers for ``domain``, with its entries and source text.

    Live-fire #105: this used to be one file, resolved through the create path's
    question, so a domain written out in ``configuration.yaml`` — which is how
    the reference instance configures three of them — listed as empty and every
    lookup in it 404'd. Three of the four wirings HA supports were invisible.
    """
    loaded: list[tuple[dict[str, Any], DomainFile, str]] = []
    for entry in readable_domain_files(base, domain, yaml_file_for_domain(domain)):
        data, source = _load_helpers_from(base, entry.path, key_path=entry.key_path)
        loaded.append((data, entry, source))
    return loaded


def _helper_owner(base: str, domain: str, helper_id: str) -> tuple[dict[str, Any], DomainFile, str] | None:
    """The file that defines ``helper_id`` in ``domain``, or None."""
    for data, entry, source in _load_helpers_everywhere(base, domain):
        if helper_id in data:
            return data, entry, source
    return None


def _validate_domain(domain: str) -> None:
    """Raise 400 if domain is not an allowed helper domain."""
    if domain not in ALLOWED_DOMAINS:
        raise web.HTTPBadRequest(text=f"Invalid helper domain: {domain}. Allowed: {', '.join(sorted(ALLOWED_DOMAINS))}")


def storage_helpers(base: str) -> list[StorageHelper]:
    """Every helper HA keeps in a `.storage` collection, across all read domains."""
    entity_ids = registry.entity_ids_by_unique_id(base)
    found: list[StorageHelper] = []
    for domain in sorted(STORAGE_DOMAINS):
        items = registry.storage_json(base, domain).get("items")
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
    seen: set[tuple[str, str]] = set()
    for domain in domains:
        _validate_domain(domain)
        for data, _entry, _source in _load_helpers_everywhere(base, domain):
            for helper_id, helper in data.items():
                if not isinstance(helper, dict) or (domain, helper_id) in seen:
                    continue
                seen.add((domain, helper_id))
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
        owner = _helper_owner(base, domain, helper_id)
        if owner is not None:
            data, _entry, _source = owner
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

    data, source = _load_helpers_from(base, target)
    if helper_id in data:
        raise web.HTTPConflict(text=f"Helper already exists: {helper_id}")

    data[helper_id] = helper_body
    surgical = save_entry(base, target, data, source, Edit("append", helper_id), yaml)
    reload = await core_api.reload_domain(domain)
    entity_id = f"{domain}.{helper_id}"
    entity_created = await core_api.poll_for_entity(entity_id) if reload.ok else False
    return web.json_response(
        {
            "status": "created",
            "id": helper_id,
            "entity_id": entity_id,
            **write_fields(surgical),
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


def _require_writable(entry: DomainFile, helper_id: str, domain: str) -> None:
    """Refuse a write to a file whose root is not the domain's own mapping.

    The read path deliberately reaches further than the write path can follow
    (live-fire #105): an inline domain and a package file both hold real helpers
    and both keep them UNDER a key. :mod:`companion.surgical` splices an entry
    into a document whose root IS the mapping, so pointing it at one of these
    would rewrite the wrong level of ``configuration.yaml``.

    409 rather than 404, and naming the file: the helper exists, this route
    cannot be the one to change it, and the caller needs to know which file to
    open. A bare "not found" from the write half of a family whose read half
    just returned the entry is the read/write contradiction #104 was.
    """
    if entry.writable:
        return
    where = ".".join(entry.key_path)
    raise web.HTTPConflict(
        text=(
            f"Helper '{helper_id}' is defined in {entry.path.name} under '{where}:', not in a file "
            f"whose top level is the {domain} mapping — this route edits a single entry by splicing "
            f"it, which is only safe at the document's root. Edit {entry.path.name} directly with "
            f"PUT /v1/config/file, or move the domain to '{domain}: !include "
            f"{yaml_file_for_domain(domain)}'."
        )
    )


def _locate_helper(base: str, helper_id: str, domain: str | None) -> tuple[str, dict[str, Any], Path, str]:
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

    Returns ``(domain, data_dict, file_path, source_text)``.
    """
    if domain:
        _validate_domain(domain)
        owner = _helper_owner(base, domain, helper_id)
        if owner is not None:
            data, entry, source = owner
            _require_writable(entry, helper_id, domain)
            return domain, data, entry.path, source
        _refuse_storage_write(base, helper_id, domain)
        raise web.HTTPNotFound(text=f"Helper not found: {helper_id} (domain {domain})")

    matches: list[tuple[str, dict[str, Any], Path, str]] = []
    for candidate in sorted(ALLOWED_DOMAINS):
        owner = _helper_owner(base, candidate, helper_id)
        if owner is not None:
            data, entry, source = owner
            _require_writable(entry, helper_id, candidate)
            matches.append((candidate, data, entry.path, source))

    if not matches:
        _refuse_storage_write(base, helper_id, None)
        raise _not_found(helper_id)
    if len(matches) > 1:
        domains = ", ".join(candidate for candidate, _, _, _ in matches)
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

    domain, data, target, source = _locate_helper(base, helper_id, domain_param)
    data[helper_id] = new_body
    surgical = save_entry(base, target, data, source, Edit("replace", helper_id), yaml)
    reload = await core_api.reload_domain(domain)
    return web.json_response({"status": "applied", **write_fields(surgical), **core_api.reload_fields(reload)})


async def delete_helper(request: web.Request) -> web.Response:
    """DELETE /v1/config/helper?id=<id>[&domain=<domain>] — delete helper."""
    base = request.app["config_base_path"]
    helper_id = request.query.get("id", "")
    if not helper_id:
        raise web.HTTPBadRequest(text="Missing id parameter")
    domain_param = request.query.get("domain") or None

    domain, data, target, source = _locate_helper(base, helper_id, domain_param)
    del data[helper_id]
    surgical = save_entry(base, target, data, source, Edit("delete", helper_id), yaml)
    reload = await core_api.reload_domain(domain)
    return web.json_response({"status": "deleted", **write_fields(surgical), **core_api.reload_fields(reload)})


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/config/helpers", get_helpers),
    RouteDef("GET", "/v1/config/helper", get_helper),
    RouteDef("PUT", "/v1/config/helper", put_helper),
    RouteDef("POST", "/v1/config/helper", post_helper),
    RouteDef("DELETE", "/v1/config/helper", delete_helper),
]
