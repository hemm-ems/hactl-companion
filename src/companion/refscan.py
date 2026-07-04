"""Literal entity-reference scanning across Home Assistant config files.

Unlike ``related.py``'s co-occurrence graph, which only pairs strings already in
the live entity registry, this scans for a *literal* value regardless of whether
it is still a known entity. That is what makes it usable for stale/renamed
entities: it finds every place a now-deleted entity_id is still referenced.

Each file is parsed on its own (``resolve=False``) so a hit reports the concrete
file it lives in (e.g. ``automations.yaml``) rather than a fully-inlined blob.
The ``{location, path, matched_value}`` shape mirrors the Go ``jsonwalk`` output
so a caller can merge YAML and dashboard hits into one uniform result set.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from companion.yaml_resolver import CircularIncludeError, YamlResolver

_INCLUDE_DIR_TAGS = {
    "!include_dir_named",
    "!include_dir_list",
    "!include_dir_merge_named",
    "!include_dir_merge_list",
}
_YAML_SUFFIXES = (".yaml", ".yml")


@dataclass(frozen=True)
class ScanHit:
    location: str  # config-relative file, e.g. "automations.yaml"
    path: str  # dotted/bracketed path within that file, e.g. "[3].trigger[0].entity_id"
    matched_value: str


def scan_yaml_for_literal(
    base: str | Path,
    target: str,
    entry_file: str = "configuration.yaml",
) -> list[ScanHit]:
    """Find every string leaf equal to ``target`` across the config file graph.

    Starts from ``entry_file`` and follows ``!include`` / ``!include_dir_*``
    directives, scanning each reachable file's own parsed tree. Files that cannot
    be read or parsed are skipped rather than aborting the whole scan.
    """
    base_path = Path(base).resolve()
    resolver = YamlResolver(base_path)
    hits: list[ScanHit] = []
    seen: set[str] = set()
    queue: deque[str] = deque([entry_file])

    while queue:
        rel = queue.popleft()
        abs_path = (base_path / rel).resolve()
        if str(abs_path) in seen or not abs_path.is_file():
            continue
        seen.add(str(abs_path))

        try:
            data = resolver.load(rel, resolve=False)
        except (FileNotFoundError, PermissionError, ValueError, CircularIncludeError):
            continue

        location = _rel_to(abs_path, base_path)
        for match_path in _scan_tree(data, target):
            hits.append(ScanHit(location, _path_str(match_path), target))

        for inc in _include_targets(data, abs_path.parent):
            queue.append(_rel_to(inc, base_path))

    hits.sort(key=lambda h: (h.location, h.path))
    return hits


def _scan_tree(node: Any, target: str, path: tuple[Any, ...] = ()) -> list[list[Any]]:
    """Return the path of every str leaf equal to target in a parsed YAML tree.

    ``!include``-tagged scalars are neither str, dict nor list, so they fall
    through untouched — their contents are scanned when the target file is
    reached via the include graph.
    """
    if isinstance(node, str):
        return [list(path)] if node == target else []
    matches: list[list[Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            matches.extend(_scan_tree(value, target, (*path, key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            matches.extend(_scan_tree(value, target, (*path, index)))
    return matches


def _include_targets(node: Any, context_dir: Path) -> list[Path]:
    """Absolute paths of files reachable via !include* tags in an unresolved tree."""
    targets: list[Path] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
            return
        if isinstance(value, list):
            for child in value:
                walk(child)
            return
        if hasattr(value, "tag") and hasattr(value, "value"):
            tag = value.tag.value if hasattr(value.tag, "value") else str(value.tag)
            raw = str(value.value).strip()
            if not raw:
                return
            dest = (context_dir / raw).resolve()
            if tag == "!include":
                targets.append(dest)
            elif tag in _INCLUDE_DIR_TAGS and dest.is_dir():
                targets.extend(
                    sorted(f.resolve() for f in dest.iterdir() if f.is_file() and f.suffix in _YAML_SUFFIXES)
                )

    walk(node)
    return targets


def _path_str(path: list[Any]) -> str:
    """Render a path like ``views[0].cards[2].entity`` (mirrors Go jsonwalk)."""
    parts: list[str] = []
    for i, seg in enumerate(path):
        if isinstance(seg, int):
            parts.append(f"[{seg}]")
        else:
            parts.append(str(seg) if i == 0 else f".{seg}")
    return "".join(parts)


def _rel_to(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base).as_posix()
    except ValueError:
        return path.name
