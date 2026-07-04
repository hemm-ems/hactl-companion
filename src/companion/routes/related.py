"""Generic related-entity graph endpoints."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from companion.yaml_resolver import CircularIncludeError, YamlResolver


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

        return sorted(related, key=lambda r: (r.entity_id, r.relationship, r.detail))

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
    include_stale = request.query.get("stale", "").lower() in ("1", "true", "yes")

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
