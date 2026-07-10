"""Helper entity CRUD endpoints.

Manages HA helpers (input_boolean, input_number, input_select, input_text,
input_datetime, counter, timer, schedule) via their per-domain YAML files.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from aiohttp import web
from ruamel.yaml import YAML

from companion import core_api
from companion.routes.config import _resolve_config_path

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


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


def _yaml_file_for_domain(domain: str) -> str:
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

    Helper files are top-level mappings keyed by entity slug. Resolves the
    file purely by naming convention (<domain>.yaml) — used by the
    read/update/delete paths, which operate on helpers that (by definition)
    already exist and were therefore already loaded successfully by HA.
    """
    target = _resolve_config_path(base, _yaml_file_for_domain(domain))
    return _load_helpers_from(target), target


def _tag_of(node: Any) -> str | None:
    """Return the ruamel YAML tag (e.g. '!include') of a loaded node, or None."""
    tag = getattr(node, "tag", None)
    return getattr(tag, "value", None) if tag is not None else None


def _resolve_domain_target(base: str, domain: str) -> Path:
    """Resolve the file HA actually loads for a helper domain's top-level key.

    Reads configuration.yaml's top-level `<domain>:` key. Raises 400 if the
    key is absent, holds an inline mapping/list instead of an `!include`, or
    uses an `!include_dir_*` directive — in all three cases, writing to
    `<domain>.yaml` by convention would produce a helper HA never loads.
    """
    config_path = _resolve_config_path(base, "configuration.yaml")
    if not config_path.is_file():
        raise web.HTTPBadRequest(text="configuration.yaml not found")

    with open(config_path, encoding="utf-8") as f:
        config_data = yaml.load(f)

    if not isinstance(config_data, dict) or domain not in config_data:
        raise web.HTTPBadRequest(
            text=(
                f"configuration.yaml has no top-level '{domain}:' key. "
                f"Add '{domain}: !include {domain}.yaml' before creating {domain} helpers."
            )
        )

    value = config_data[domain]
    tag = _tag_of(value)

    if tag == "!include":
        rel_path = str(value.value).strip()
        return _resolve_config_path(base, rel_path)

    if tag and tag.startswith("!include_dir_"):
        raise web.HTTPBadRequest(
            text=f"'{domain}:' uses {tag} in configuration.yaml, which hactl cannot target for a single new helper."
        )

    raise web.HTTPBadRequest(
        text=(
            f"'{domain}:' is defined inline in configuration.yaml, not via !include. "
            f"hactl cannot safely append to an inline mapping."
        )
    )


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


async def get_helper(request: web.Request) -> web.Response:
    """GET /v1/config/helper?id=<id> — get single helper definition.

    The id should be the slug (e.g. 'my_toggle'). We search all domains.
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
            return web.json_response({"id": helper_id, "domain": domain, "content": stream.getvalue()})

    raise web.HTTPNotFound(text=f"Helper not found: {helper_id}")


async def post_helper(request: web.Request) -> web.Response:
    """POST /v1/config/helper?domain=<domain> — create new helper."""
    base = request.app["config_base_path"]
    domain = request.query.get("domain", "")
    if not domain:
        raise web.HTTPBadRequest(text="Missing domain parameter")
    _validate_domain(domain)
    target = _resolve_domain_target(base, domain)

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
    reloaded = await core_api.reload_domain(domain)
    entity_id = f"{domain}.{helper_id}"
    entity_created = await _poll_entity_created(entity_id) if reloaded else False
    return web.json_response(
        {
            "status": "created",
            "id": helper_id,
            "entity_id": entity_id,
            "reloaded": reloaded,
            "entity_created": entity_created,
        },
        status=201,
    )


def _locate_helper(base: str, helper_id: str, domain: str | None) -> tuple[str, dict[str, Any], Any]:
    """Locate the domain file that owns ``helper_id`` for update/delete.

    With an explicit ``domain`` only that domain is considered. Without one, all
    domains are scanned. The same slug can legitimately exist in two domains
    (e.g. ``counter.kitchen`` and ``input_boolean.kitchen``), so scanning and
    silently acting on the alphabetically-first match would touch the wrong
    entity — instead the ambiguity is surfaced as a 409 listing the candidates.

    Returns ``(domain, data_dict, file_path)``.
    """
    if domain:
        _validate_domain(domain)
        data, target = _load_helpers(base, domain)
        if helper_id in data:
            return domain, data, target
        raise web.HTTPNotFound(text=f"Helper not found: {helper_id} (domain {domain})")

    matches: list[tuple[str, dict[str, Any], Any]] = []
    for candidate in sorted(ALLOWED_DOMAINS):
        data, target = _load_helpers(base, candidate)
        if helper_id in data:
            matches.append((candidate, data, target))

    if not matches:
        raise web.HTTPNotFound(text=f"Helper not found: {helper_id}")
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
    reloaded = await core_api.reload_domain(domain)
    return web.json_response({"status": "applied", "reloaded": reloaded})


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
    reloaded = await core_api.reload_domain(domain)
    return web.json_response({"status": "deleted", "reloaded": reloaded})


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/config/helpers", get_helpers),
    RouteDef("GET", "/v1/config/helper", get_helper),
    RouteDef("PUT", "/v1/config/helper", put_helper),
    RouteDef("POST", "/v1/config/helper", post_helper),
    RouteDef("DELETE", "/v1/config/helper", delete_helper),
]
