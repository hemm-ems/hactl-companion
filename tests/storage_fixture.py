"""The `.storage` collections Home Assistant keeps UI-created helpers in.

Every item below is the payload a live HA 2026.7 instance wrote to
`.storage/<domain>` after the corresponding `<domain>/create` WebSocket command
— copied off disk, not invented. A fixture and the code can be wrong together
(the `related_fixture` failure class), so this shape is *also* re-derived from
HA at test time by
`tests/integration/test_live.py::TestStorageHelpers::test_ui_created_helpers_are_readable`,
which creates the helpers through HA's own API and then asks this service for
them. This module exists so the unit tier can cover the same branch without
Docker, never as the authority on what HA writes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: domain -> the collection item HA persists. Nine domains: the eight this
#: service can also write as YAML, plus `input_button`, which has no YAML form
#: at all and is therefore *only* ever storage-backed.
STORAGE_ITEMS: dict[str, dict[str, Any]] = {
    "input_boolean": {"id": "probe_bool", "name": "Probe Bool", "icon": "mdi:toggle-switch"},
    "input_number": {
        "id": "probe_number",
        "name": "Probe Number",
        "min": 0.0,
        "max": 100.0,
        "step": 1.0,
        "mode": "slider",
    },
    "input_select": {"id": "probe_select", "name": "Probe Select", "options": ["a", "b"]},
    "input_text": {"id": "probe_text", "name": "Probe Text", "min": 0, "max": 100, "mode": "text"},
    "input_datetime": {"id": "probe_datetime", "name": "Probe Datetime", "has_date": True, "has_time": True},
    "input_button": {"id": "probe_button", "name": "Probe Button"},
    "counter": {
        "id": "probe_counter",
        "name": "Probe Counter",
        "initial": 0,
        "step": 1,
        "restore": True,
        "minimum": None,
        "maximum": None,
    },
    "timer": {"id": "probe_timer", "name": "Probe Timer", "duration": "0:05:00", "restore": False},
    "schedule": {
        "id": "probe_schedule",
        "name": "Probe Schedule",
        "monday": [{"from": "08:00:00", "to": "17:00:00"}],
        "tuesday": [],
        "wednesday": [],
        "thursday": [],
        "friday": [],
        "saturday": [],
        "sunday": [],
    },
}


def _write(path: Path, key: str, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "minor_version": 1, "key": key, "data": data}, indent=2),
        encoding="utf-8",
    )


def seed_storage_helpers(config_dir: Path, domains: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Write `.storage/<domain>` for each domain, plus a matching entity registry.

    Returns the items written, keyed by domain. The registry is part of the
    fixture because the entity_id is a registry fact, not a naming rule.
    """
    chosen = {domain: STORAGE_ITEMS[domain] for domain in (domains or list(STORAGE_ITEMS))}
    for domain, item in chosen.items():
        _write(config_dir / ".storage" / domain, domain, {"items": [item]})
    _write(
        config_dir / ".storage" / "core.entity_registry",
        "core.entity_registry",
        {
            "entities": [
                {
                    "entity_id": f"{domain}.{item['id']}",
                    "platform": domain,
                    "unique_id": item["id"],
                    "original_name": item.get("name"),
                }
                for domain, item in chosen.items()
            ]
        },
    )
    return chosen


def rename_in_registry(config_dir: Path, domain: str, item_id: str, new_entity_id: str) -> None:
    """Point the registry entry for a collection item at a different entity_id.

    HA's `config/entity_registry/update` does exactly this and leaves the
    collection item's `id` untouched, so `<domain>.<item id>` stops being the
    entity's real name — verified live.
    """
    path = config_dir / ".storage" / "core.entity_registry"
    raw = json.loads(path.read_text(encoding="utf-8"))
    for entry in raw["data"]["entities"]:
        if entry["platform"] == domain and entry["unique_id"] == item_id:
            entry["entity_id"] = new_entity_id
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
