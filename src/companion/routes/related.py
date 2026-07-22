"""Generic related-entity graph endpoints."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from companion.params import parse_bool_param
from companion.refscan import include_dir_files, include_tag, iter_config_trees, render_path, scan_tree_for_literal
from companion.yaml_resolver import CircularIncludeError, YamlResolver

# Relationship name for "this automation's config mentions the entity". Distinct
# from `yaml-reference` (an entity that merely co-occurs in the same YAML node),
# and from the config-entry/device relationships, because it answers a different
# question: `ent related` is the delete-safety check, and the useful answer to
# "what breaks if I delete sensor.x" is the *automation* that would break.
AUTOMATION_RELATIONSHIP = "automation-reference"

# HA accepts a bare `automation:` key and label-suffixed variants (`automation ui:`,
# `automation manual:`) so a config can declare the domain more than once.
_AUTOMATION_KEY_RE = re.compile(r"^automation(\s+\S+)?$")

_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


@dataclass(frozen=True)
class RelatedItem:
    entity_id: str
    relationship: str
    detail: str


@dataclass(frozen=True)
class AutomationBlock:
    """One automation definition located in a concrete config file.

    ``prefix`` is the rendered path of the automation *within* that file
    (``"[3]"`` for the fourth entry of ``automations.yaml``, ``"automation[0]"``
    for the first entry of a package's ``automation:`` key, ``""`` when the whole
    file is one automation). A scan hit belongs to this automation when its path
    starts at that prefix.
    """

    location: str
    prefix: str
    automation_id: str
    alias: str

    @property
    def detail(self) -> str:
        where = f"{self.location}:{self.prefix}" if self.prefix else self.location
        return f"{where} ({self.alias})" if self.alias else where

    def entity_id(self, registry: dict[str, str]) -> str:
        """The automation's entity_id: registry-resolved, else derived from its alias.

        HA registers a YAML automation under a unique_id equal to its ``id:``, so
        the registry snapshot gives the true entity_id whenever the automation has
        an id and has been loaded. Automations without an ``id:`` are never in the
        registry, so we fall back to how HA itself first names one — the slugified
        alias. That is a best-effort name; ``detail`` always carries the exact
        file and index, which is what makes the row actionable either way.
        """
        resolved = registry.get(self.automation_id)
        if resolved:
            return resolved
        return f"automation.{_slugify(self.alias or self.automation_id)}"


class RelatedGraph:
    def __init__(self, base_path: str | Path) -> None:
        self._base = Path(base_path)
        self.entity_ids: set[str] = set()
        self.device_ids: set[str] = set()
        self.config_entry_ids: set[str] = set()
        self.area_ids: set[str] = set()
        self.entity_to_config_entries: dict[str, set[str]] = defaultdict(set)
        self.config_entry_to_entities: dict[str, set[str]] = defaultdict(set)
        self.entity_to_device: dict[str, str] = {}
        self.device_to_entities: dict[str, set[str]] = defaultdict(set)
        self.config_entry_entity_refs: dict[str, set[str]] = defaultdict(set)
        self.config_entry_device_refs: dict[str, set[str]] = defaultdict(set)
        self.yaml_entity_refs: set[frozenset[str]] = set()
        # unique_id -> entity_id for registry entries owned by the automation
        # integration; a YAML automation's unique_id is its `id:`.
        self.automation_entity_by_unique_id: dict[str, str] = {}

    def load(self) -> None:
        config_entries = _dict_list(self._storage_data("core.config_entries").get("entries"))
        entity_entries = _dict_list(self._storage_data("core.entity_registry").get("entities"))
        device_entries = _dict_list(self._storage_data("core.device_registry").get("devices"))

        for entry in config_entries:
            if isinstance(entry, dict) and isinstance(entry.get("entry_id"), str):
                self.config_entry_ids.add(entry["entry_id"])

        for entry in entity_entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("entity_id"), str):
                continue
            entity_id = entry["entity_id"]
            self.entity_ids.add(entity_id)
            if isinstance(entry.get("device_id"), str) and entry["device_id"]:
                device_id = entry["device_id"]
                self.device_ids.add(device_id)
                self.entity_to_device[entity_id] = device_id
                self.device_to_entities[device_id].add(entity_id)
            if isinstance(entry.get("area_id"), str) and entry["area_id"]:
                self.area_ids.add(entry["area_id"])
            if entry.get("platform") == "automation" and entry.get("unique_id") is not None:
                self.automation_entity_by_unique_id[str(entry["unique_id"])] = entity_id
            for config_entry_id in _entity_config_entries(entry):
                self.config_entry_ids.add(config_entry_id)
                self.entity_to_config_entries[entity_id].add(config_entry_id)
                self.config_entry_to_entities[config_entry_id].add(entity_id)

        for entry in device_entries:
            if not isinstance(entry, dict):
                continue
            device_id = entry.get("id")
            if isinstance(device_id, str) and device_id:
                self.device_ids.add(device_id)
                if isinstance(entry.get("area_id"), str) and entry["area_id"]:
                    self.area_ids.add(entry["area_id"])
                for config_entry_id in _string_list(entry.get("config_entries")):
                    self.config_entry_ids.add(config_entry_id)

        known = {
            "entity": self.entity_ids,
            "device": self.device_ids,
            "config_entry": self.config_entry_ids,
            "area": self.area_ids,
        }
        for entry in config_entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("entry_id"), str):
                continue
            entry_id = entry["entry_id"]
            refs = _collect_known_ids(entry, known)
            self.config_entry_entity_refs[entry_id].update(refs["entity"] - self.config_entry_to_entities[entry_id])
            self.config_entry_device_refs[entry_id].update(refs["device"])

        self._load_yaml_refs()

    def related_to_entity(self, entity_id: str) -> list[RelatedItem]:
        if entity_id not in self.entity_ids:
            raise KeyError(entity_id)

        related: set[RelatedItem] = set()
        for config_entry_id, refs in self.config_entry_entity_refs.items():
            if entity_id in refs:
                for generated in self.config_entry_to_entities.get(config_entry_id, set()):
                    if generated != entity_id:
                        related.add(RelatedItem(generated, "config-entry-reference", f"config_entry={config_entry_id}"))

        for config_entry_id in self.entity_to_config_entries.get(entity_id, set()):
            for ref in self.config_entry_entity_refs.get(config_entry_id, set()):
                if ref != entity_id:
                    related.add(RelatedItem(ref, "referenced-entity", f"config_entry={config_entry_id}"))
            for device_id in self.config_entry_device_refs.get(config_entry_id, set()):
                for ref in self.device_to_entities.get(device_id, set()):
                    if ref != entity_id:
                        related.add(RelatedItem(ref, "device-reference", f"config_entry={config_entry_id}"))

        for pair in self.yaml_entity_refs:
            if entity_id not in pair:
                continue
            for ref in pair:
                if ref != entity_id:
                    related.add(RelatedItem(ref, "yaml-reference", "configuration.yaml"))

        related.update(self.automation_references(entity_id))

        return sorted(related, key=lambda r: (r.entity_id, r.relationship, r.detail))

    def automation_references(self, entity_id: str) -> set[RelatedItem]:
        """Automations whose config mentions ``entity_id``, named individually.

        The co-occurrence graph above cannot answer this: it compares whole
        strings (so a Jinja-embedded reference never matches) and it only ever
        emits entity<->entity edges (so even an exact match can say "these two
        entities appear near each other", never "*this automation* uses it").
        Here we instead run the boundary-aware matcher from :mod:`companion.refscan`
        — the same one ``ref scan`` uses — over each config file's own tree, and
        attribute every hit to the automation whose path prefix contains it.
        """
        found: set[RelatedItem] = set()
        # location -> how that file's automations are laid out, recorded from the
        # `automation:` include tag that pointed at it. Its includer is always
        # walked first, so the kind is known by the time the file is reached.
        file_kinds: dict[str, str] = {}
        # Must match what iter_config_trees resolves internally, or every include
        # target looks like it escapes the config dir (e.g. a symlinked /config).
        base = self._base.resolve()

        # `contains` skips parsing files that can hold neither the entity nor an
        # include; without it this walk roughly doubles the cost of the endpoint.
        for location, abs_path, tree in iter_config_trees(base, contains=entity_id):
            for child, kind in _automation_include_kinds(tree, abs_path.parent, base):
                file_kinds.setdefault(child, kind)

            blocks = _automation_blocks(tree, file_kinds.get(location), location)
            if not blocks:
                continue
            for hit_path in scan_tree_for_literal(tree, entity_id):
                block = _owning_block(blocks, hit_path)
                if block is None:
                    continue
                automation_entity_id = block.entity_id(self.automation_entity_by_unique_id)
                if automation_entity_id == entity_id:
                    continue  # an automation's own definition is not a relation
                found.add(RelatedItem(automation_entity_id, AUTOMATION_RELATIONSHIP, block.detail))

        return found

    def _storage_data(self, key: str) -> dict[str, Any]:
        path = self._base / ".storage" / key
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        data = raw.get("data")
        return data if isinstance(data, dict) else {}

    def _load_yaml_refs(self) -> None:
        config = self._base / "configuration.yaml"
        if not config.is_file():
            return
        try:
            data = YamlResolver(self._base).load("configuration.yaml", resolve=True)
        except (FileNotFoundError, PermissionError, ValueError, CircularIncludeError):
            return
        self.yaml_entity_refs.update(_yaml_entity_pairs(data, self.entity_ids))


def _entity_config_entries(entry: dict[str, Any]) -> Iterable[str]:
    config_entry_id = entry.get("config_entry_id")
    if isinstance(config_entry_id, str) and config_entry_id:
        yield config_entry_id
    yield from _string_list(entry.get("config_entry_ids"))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _collect_known_ids(node: Any, known: dict[str, set[str]]) -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {kind: set() for kind in known}

    def walk(value: Any) -> None:
        if isinstance(value, str):
            for kind, ids in known.items():
                if value in ids:
                    refs[kind].add(value)
            return
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
            return
        if isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return refs


def _is_automation_key(key: Any) -> bool:
    return isinstance(key, str) and bool(_AUTOMATION_KEY_RE.match(key))


def _slugify(text: str) -> str:
    """HA-style slug: accents folded, non-alphanumerics collapsed to underscores."""
    folded = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return _NON_SLUG_RE.sub("_", ascii_only.lower()).strip("_") or "unnamed"


def _keyed_automation_nodes(node: Any, path: tuple[Any, ...] = ()) -> list[tuple[tuple[Any, ...], Any]]:
    """Every ``(path, value)`` sitting under an ``automation``-ish mapping key.

    Walks to any depth, so it finds both a top-level ``automation:`` (a package
    file, or configuration.yaml itself) and one nested inside an inline
    ``homeassistant: packages:`` block. Does not descend into the value it
    reports — an automation's own body is never a container of automations.
    """
    matches: list[tuple[tuple[Any, ...], Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if _is_automation_key(key):
                matches.append(((*path, key), value))
            else:
                matches.extend(_keyed_automation_nodes(value, (*path, key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            matches.extend(_keyed_automation_nodes(value, (*path, index)))
    return matches


def _automation_include_kinds(tree: Any, context_dir: Path, base: Path) -> list[tuple[str, str]]:
    """``(location, kind)`` for every file an ``automation:`` key includes.

    ``kind`` is ``"list"`` when the target file holds a list of automations
    (``!include automations.yaml``, ``!include_dir_merge_list automations/``) and
    ``"single"`` when the whole file is one automation (``!include_dir_list``).
    Named-dir tags are not valid for the automation domain and are ignored.
    """
    kinds: list[tuple[str, str]] = []
    for _path, value in _keyed_automation_nodes(tree):
        tagged = include_tag(value)
        if tagged is None:
            continue
        tag, raw = tagged
        dest = (context_dir / raw).resolve()
        if tag == "!include":
            targets, kind = [dest], "list"
        elif tag == "!include_dir_merge_list":
            targets, kind = include_dir_files(dest), "list"
        elif tag == "!include_dir_list":
            targets, kind = include_dir_files(dest), "single"
        else:
            continue
        for target in targets:
            try:
                kinds.append((target.relative_to(base).as_posix(), kind))
            except ValueError:
                continue  # escapes the config dir; the walk skips it too
    return kinds


def _automation_blocks(tree: Any, file_kind: str | None, location: str) -> list[AutomationBlock]:
    """Every automation defined in one parsed file, with its path prefix.

    Covers all the layouts HA allows: a file that *is* the automation list
    (``!include``/``!include_dir_merge_list``), a file that is one automation
    (``!include_dir_list``), and an inline/package ``automation:`` key.
    """
    found: list[tuple[tuple[Any, ...], Any]] = []
    if file_kind == "list" and isinstance(tree, list):
        found.extend(((index,), item) for index, item in enumerate(tree))
    elif file_kind in {"list", "single"} and isinstance(tree, dict):
        found.append(((), tree))

    for path, value in _keyed_automation_nodes(tree):
        if isinstance(value, list):
            found.extend(((*path, index), item) for index, item in enumerate(value))
        elif isinstance(value, dict):
            found.append((path, value))

    blocks: list[AutomationBlock] = []
    for path, item in found:
        if not isinstance(item, dict):
            continue
        automation_id = item.get("id")
        alias = item.get("alias")
        blocks.append(
            AutomationBlock(
                location=location,
                prefix=render_path(path),
                automation_id=str(automation_id) if automation_id is not None else "",
                alias=str(alias) if alias is not None else "",
            )
        )
    return blocks


def _owning_block(blocks: list[AutomationBlock], hit_path: str) -> AutomationBlock | None:
    """The automation containing ``hit_path``, i.e. the longest matching prefix.

    A path prefix must end on a segment boundary: ``[1]`` must not claim a hit in
    ``[10]``, and ``automation`` must not claim one in ``automations``.
    """
    best: AutomationBlock | None = None
    for block in blocks:
        if not _path_starts_with(hit_path, block.prefix):
            continue
        if best is None or len(block.prefix) > len(best.prefix):
            best = block
    return best


def _path_starts_with(path: str, prefix: str) -> bool:
    if not prefix:
        return True
    if not path.startswith(prefix):
        return False
    return len(path) == len(prefix) or path[len(prefix)] in ".["


def _yaml_entity_pairs(node: Any, entity_ids: set[str]) -> set[frozenset[str]]:
    pairs: set[frozenset[str]] = set()

    def walk(value: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(value, str):
            if value in entity_ids:
                found.add(value)
            return found
        if isinstance(value, dict):
            for child in value.values():
                found.update(walk(child))
        elif isinstance(value, list):
            for child in value:
                found.update(walk(child))
        if 1 < len(found) <= 20:
            items = sorted(found)
            for i, left in enumerate(items):
                for right in items[i + 1 :]:
                    pairs.add(frozenset((left, right)))
        return found

    walk(node)
    return pairs


async def get_related_entity(request: web.Request) -> web.Response:
    entity_id = request.query.get("entity_id", "")
    if not entity_id:
        raise web.HTTPBadRequest(text="Missing entity_id parameter")
    include_stale = parse_bool_param(request, "stale", default=False)

    base = request.app["config_base_path"]
    graph = RelatedGraph(base)
    graph.load()

    # A stale/renamed/deleted entity is not in the on-disk registry snapshot. By
    # default that is a 404 (unchanged). With ?stale=true we instead scan the
    # config files for the literal entity_id and report where it is still
    # referenced — the co-occurrence graph can't, since it only pairs known ids.
    is_stale = entity_id not in graph.entity_ids
    if is_stale and not include_stale:
        raise web.HTTPNotFound(text=f"Entity not found: {entity_id}")

    related = [] if is_stale else graph.related_to_entity(entity_id)

    stale_refs: list[dict[str, str]] = []
    if is_stale:
        from companion.refscan import scan_yaml_for_literal

        stale_refs = [
            {"location": hit.location, "path": hit.path, "matched_value": hit.matched_value}
            for hit in scan_yaml_for_literal(base, entity_id)
        ]

    return web.json_response(
        {
            "entity_id": entity_id,
            "stale": is_stale,
            "related": [
                {"entity_id": item.entity_id, "relationship": item.relationship, "detail": item.detail}
                for item in related
            ],
            "stale_refs": stale_refs,
        }
    )


routes: list[RouteDef] = [
    RouteDef("GET", "/v1/related/entity", get_related_entity),
]
