"""Read access to Home Assistant's `.storage` files.

`.storage` is HA's own state, not config a human authors — it is read directly
on disk, never through :mod:`companion.pathguard` (that guard governs
caller-supplied config paths; nothing here takes one, the key is always a
fixed, internally-chosen constant). Best-effort throughout: a `.storage` file
that is mid-write, absent, or from a future schema degrades a lookup to "not
found", never a 500 — `.storage` belongs to Home Assistant, and this service
only ever looks, never writes to it.

Was a private copy of the read primitive in ``routes/helpers.py``
(``_storage_json``), for exactly one purpose (listing storage-backed helper
collections). Generalized here so a second caller reads the same file the same
way rather than growing its own copy — but note the entity registry
specifically (`core.entity_registry`) is a *debounced* file: measured live, a
just-created entity's registry row landed on disk roughly ten seconds after
the entity was already answering `/api/states`. That is far outside any
request-scoped poll, which is why the post-create entity verification in
``routes/templates.py`` deliberately does **not** use this module — it matches
against live states instead (see ``_poll_template_entity``). This module
remains the right tool for `helper ls`'s storage-backed listing, which reads
on its own schedule, not inside another route's just-finished write.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STORAGE_DIR = ".storage"
ENTITY_REGISTRY_KEY = "core.entity_registry"


def storage_json(base: str, key: str) -> dict[str, Any]:
    """The ``data`` object of ``.storage/<key>``, or ``{}`` if unreadable."""
    path = Path(base) / STORAGE_DIR / key
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    data = raw.get("data") if isinstance(raw, dict) else None
    return data if isinstance(data, dict) else {}


def entity_ids_by_unique_id(base: str) -> dict[tuple[str, str], str]:
    """``(platform, unique_id) -> entity_id`` from the entity registry.

    A UI helper registers under a unique_id equal to its collection item id, so
    this is what turns an item back into the entity_id the user sees. It is not
    cosmetic: renaming the entity changes the entity_id and leaves the item id
    alone, so ``input_boolean.<item id>`` is a *guess* and the registry is the
    fact (verified live — the rename was performed and observed). Fine to read
    here because a helper listing is not racing its own just-finished write the
    way a post-create verification would be (see the module docstring).
    """
    entities = storage_json(base, ENTITY_REGISTRY_KEY).get("entities")
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
