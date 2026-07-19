"""Literal entity-reference scanning across Home Assistant config files.

Unlike ``related.py``'s co-occurrence graph, which only pairs strings already in
the live entity registry, this scans for a *literal* value regardless of whether
it is still a known entity. That is what makes it usable for stale/renamed
entities: it finds every place a now-deleted entity_id is still referenced —
including a mention embedded inside a larger string, such as an entity_id
wrapped in a Jinja template (``"{{ states('sensor.foo') }}"``). Matching is
boundary-aware (see :func:`_target_pattern`), not substring: ``sensor.foo``
matches inside that template but not inside ``sensor.foo_bar``.

Each file is parsed on its own (``resolve=False``) so a hit reports the concrete
file it lives in (e.g. ``automations.yaml``) rather than a fully-inlined blob.
The ``{location, path, matched_value}`` shape mirrors the Go ``jsonwalk`` output
so a caller can merge YAML and dashboard hits into one uniform result set.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ruamel.yaml.scalarstring import ScalarString

from companion.backups import make_backup
from companion.yaml_resolver import CircularIncludeError, YamlResolver

# An entity_id is domain.object_id: a lowercase/underscore domain, a dot, then a
# lowercase/digit/underscore object id. Deliberately shape-only — a service name
# (e.g. light.turn_on) matches too; separating services from entities is the
# caller's job (it can key off the path terminal, e.g. `.service`).
#
# Deliberately de-anchored (no ^...$): a leaf doesn't have to *be* an entity_id,
# it only has to *contain* one as a whole token — e.g. a Jinja template string
# like "{{ states('sensor.foo') }}" embeds sensor.foo. The \b boundaries are
# what make this safe: since every entity_id starts and ends on a word
# character ([a-z_] / [a-z0-9_]), \b rejects a match that is merely a prefix of
# a longer token (sensor.foo inside sensor.foo_bar) or glued onto other text
# (asensor.foo), while still matching across non-word delimiters like quotes,
# parens and spaces.
_ENTITY_ID_RE = re.compile(r"\b[a-z_]+\.[a-z0-9_]+\b")

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


@dataclass(frozen=True)
class EntityRef:
    location: str  # config-relative file, e.g. "automations.yaml"
    path: str  # dotted/bracketed path within that file
    key: str  # nearest enclosing mapping key (e.g. "entity_id" vs "service") — lets the
    # caller tell a true entity position from a same-shaped service name
    matched_value: str  # the entity_id-shaped value found at this leaf


def scan_yaml_for_literal(
    base: str | Path,
    target: str,
    entry_file: str = "configuration.yaml",
) -> list[ScanHit]:
    """Find every string leaf containing ``target`` as a whole token, across the config file graph.

    A leaf matches whether it *is* ``target`` (``entity_id: sensor.gone``) or
    merely *embeds* it as a boundary-delimited token, e.g. inside a Jinja
    template string (``"{{ states('sensor.gone') }}"``). See :data:`_ENTITY_ID_RE`
    for why ``\\b`` boundaries are the right notion of "whole token" here.

    Starts from ``entry_file`` and follows ``!include`` / ``!include_dir_*``
    directives, scanning each reachable file's own parsed tree. Files that cannot
    be read or parsed are skipped rather than aborting the whole scan.
    """
    base_path = Path(base).resolve()
    resolver = YamlResolver(base_path)
    pattern = _target_pattern(target)
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
        for match_path in _scan_tree(data, pattern):
            hits.append(ScanHit(location, _path_str(match_path), target))

        for inc in _include_targets(data, abs_path.parent):
            rel_inc = _rel_within(inc, base_path)
            if rel_inc is not None:
                queue.append(rel_inc)

    hits.sort(key=lambda h: (h.location, h.path))
    return hits


def scan_yaml_for_entities(
    base: str | Path,
    entry_file: str = "configuration.yaml",
) -> list[EntityRef]:
    """Find every entity_id-shaped string leaf across the config file graph.

    Same per-file ``!include`` walk as :func:`scan_yaml_for_literal`, but instead
    of an exact target it collects every leaf matching the entity_id *shape*
    (``domain.object_id``). Each ref carries its ``key`` (nearest enclosing
    mapping key) so a caller can tell a true entity position (``entity_id``,
    ``entity``) from a same-shaped service name (``service: light.turn_on``).

    This is the bulk-enumeration primitive a caller uses to validate references:
    diff the returned values against the live entity set to find dangling ones.
    It is intentionally unfiltered — the caller decides which keys are entities.
    """
    base_path = Path(base).resolve()
    resolver = YamlResolver(base_path)
    refs: list[EntityRef] = []
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
        for match_path, value in _entity_leaves(data):
            refs.append(EntityRef(location, _path_str(match_path), _terminal_key(match_path), value))

        for inc in _include_targets(data, abs_path.parent):
            rel_inc = _rel_within(inc, base_path)
            if rel_inc is not None:
                queue.append(rel_inc)

    refs.sort(key=lambda r: (r.location, r.path))
    return refs


def _terminal_key(path: list[Any]) -> str:
    """The nearest enclosing mapping key: the last string segment of the path.

    For ``[0].trigger[0].entity_id`` this is ``entity_id``; for a list item like
    ``entity_id[0]`` it is still ``entity_id`` (trailing indices are skipped).
    """
    for seg in reversed(path):
        if isinstance(seg, str):
            return seg
    return ""


def _entity_leaves(node: Any, path: tuple[Any, ...] = ()) -> list[tuple[list[Any], str]]:
    """Return (path, value) for every entity_id-shaped token found in a str leaf.

    A leaf that *is* an entity id (``entity_id: light.kitchen``) yields one
    ref; a leaf that only *embeds* one or more entity-id-shaped tokens (e.g. a
    Jinja template string like ``"{{ states('sensor.foo') }}"``) yields one ref
    per embedded token, all sharing the leaf's path. Mirrors :func:`_scan_tree`'s
    traversal; ``!include``-tagged scalars are not str/dict/list so they fall
    through untouched.
    """
    if isinstance(node, str):
        return [(list(path), m.group()) for m in _ENTITY_ID_RE.finditer(node)]
    leaves: list[tuple[list[Any], str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            leaves.extend(_entity_leaves(value, (*path, key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            leaves.extend(_entity_leaves(value, (*path, index)))
    return leaves


def replace_yaml_literal(
    base: str | Path,
    target: str,
    replacement: str,
    entry_file: str = "configuration.yaml",
    *,
    dry_run: bool,
) -> list[dict[str, str]]:
    """Rewrite every whole-token occurrence of ``target`` to ``replacement``, per file.

    Matches the same boundary-aware notion of "occurrence" as
    :func:`scan_yaml_for_literal`: a leaf that *is* ``target`` is replaced
    outright, and a leaf that only *embeds* ``target`` as a token (e.g. inside a
    Jinja template string) has just that token swapped, leaving the rest of the
    string — and the surrounding YAML — untouched.

    Walks the exact same ``!include`` graph as :func:`scan_yaml_for_literal` and
    rewrites the literal *in the file it actually lives in*, so a reference in
    ``automations.yaml`` is changed there and not in a resolved blob. Each file is
    loaded round-trip (``resolve=False``) and dumped back through the resolver, so
    comments, formatting and quote style of untouched nodes survive.

    Returns ``[{location, path, before, after}]`` for every rewritten leaf. When
    ``dry_run`` is true the report is identical but no file is written. Each
    rewritten file is backed up (see :mod:`companion.backups`) before it is saved.
    """
    base_path = Path(base).resolve()
    resolver = YamlResolver(base_path)
    pattern = _target_pattern(target)
    changes: list[dict[str, str]] = []
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
            # Missing / denied (e.g. secrets.yaml) / circular — skip, never write.
            continue

        location = _rel_to(abs_path, base_path)
        match_paths = _replace_tree(data, pattern, replacement)
        if match_paths:
            for match_path in match_paths:
                changes.append(
                    {
                        "location": location,
                        "path": _path_str(match_path),
                        "before": target,
                        "after": replacement,
                    }
                )
            if not dry_run:
                # Back up the prior content before overwriting (C-5: every applied
                # write gets a backup, same as the PUT/POST config routes).
                make_backup(abs_path)
                resolver.save(rel, data)

        for inc in _include_targets(data, abs_path.parent):
            rel_inc = _rel_within(inc, base_path)
            if rel_inc is not None:
                queue.append(rel_inc)

    changes.sort(key=lambda c: (c["location"], c["path"]))
    return changes


def _replace_tree(node: Any, pattern: re.Pattern[str], replacement: str, path: tuple[Any, ...] = ()) -> list[list[Any]]:
    """Mutate ``node`` in place: rewrite every whole-token match of ``pattern`` to replacement.

    A leaf that is *only* the target is replaced outright; a leaf that merely
    *embeds* the target (e.g. ``"{{ states('sensor.foo') }}"``) has just the
    matched token(s) substituted, via :func:`re.Pattern.sub` on the whole leaf —
    the surrounding text is left exactly as it was. Mirrors :func:`_scan_tree`
    but assigns into the parent container, so it needs the parent+key/index to
    write. Quote style is preserved by reconstructing the original scalar's
    subclass (ruamel quoted scalars are ``str`` subclasses). ``!include``-tagged
    scalars are not str/dict/list, so they fall through untouched and are never
    rewritten. Returns the path of every rewritten leaf.
    """
    # A callable repl (rather than a plain string) makes re.sub treat
    # ``replacement`` as a literal — a string repl would otherwise interpret
    # backslash sequences (e.g. "\1") in an arbitrary entity_id as a group
    # reference and raise.
    matches: list[list[Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                new_value = pattern.sub(lambda _m: replacement, value)
                if new_value != value:
                    node[key] = _styled_like(value, new_value)
                    matches.append([*path, key])
            elif isinstance(value, (dict, list)):
                matches.extend(_replace_tree(value, pattern, replacement, (*path, key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, str):
                new_value = pattern.sub(lambda _m: replacement, value)
                if new_value != value:
                    node[index] = _styled_like(value, new_value)
                    matches.append([*path, index])
            elif isinstance(value, (dict, list)):
                matches.extend(_replace_tree(value, pattern, replacement, (*path, index)))
    return matches


def _styled_like(original: str, replacement: str) -> str:
    """Return ``replacement`` wrapped in the original scalar's quote style, if any."""
    if isinstance(original, ScalarString):
        # ScalarString subclasses str; the constructor's inferred type is Any.
        return cast(str, type(original)(replacement))
    return replacement


def _target_pattern(target: str) -> re.Pattern[str]:
    """Boundary-aware pattern matching ``target`` as a whole token anywhere in a string.

    Entity ids are made only of ``[a-z_]``, ``[a-z0-9_]`` and a separating dot,
    so every entity_id-shaped ``target`` starts and ends on a word character —
    which makes a plain ``\\b`` at each end exactly the right notion of "whole
    token": it matches a bare leaf (``sensor.foo``) and an embedded mention
    inside a larger string (``"{{ states('sensor.foo') }}"``), while rejecting
    a match that is only a prefix of a longer token (``sensor.foo_bar``) or
    glued onto other text (``asensor.foo``).
    """
    return re.compile(r"\b" + re.escape(target) + r"\b")


def _scan_tree(node: Any, pattern: re.Pattern[str], path: tuple[Any, ...] = ()) -> list[list[Any]]:
    """Return the path of every str leaf containing a whole-token match of pattern.

    ``!include``-tagged scalars are neither str, dict nor list, so they fall
    through untouched — their contents are scanned when the target file is
    reached via the include graph.
    """
    if isinstance(node, str):
        return [list(path)] if pattern.search(node) else []
    matches: list[list[Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            matches.extend(_scan_tree(value, pattern, (*path, key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            matches.extend(_scan_tree(value, pattern, (*path, index)))
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


def _rel_within(path: Path, base: Path) -> str | None:
    """Base-relative path, or None if it escapes base.

    Used when enqueueing include targets: an `!include ../outside.yaml` must be
    skipped, not retargeted to a same-named file *inside* base (which `_rel_to`'s
    `path.name` fallback would wrongly do).
    """
    try:
        return path.resolve().relative_to(base).as_posix()
    except ValueError:
        return None
