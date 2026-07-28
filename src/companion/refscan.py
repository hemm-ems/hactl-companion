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

The walk is deliberately tolerant — a file it cannot read is stepped over rather
than aborting the whole scan, because the commonest such file is ``secrets.yaml``
on a perfectly healthy instance. Tolerance without a record is the bug though:
the answer then covers less config than the caller thinks, and a caller
certifying something about the *whole* config (``ref validate``: "no dangling
references"; ``ref replace``: "renamed everywhere") certifies over a half it
never saw. So every walk takes an optional :class:`SkipLog` and every place a
file drops out of the graph writes to it; :func:`skipped_fields` turns that into
the wire field. The walk itself is unchanged — what is skipped is exactly what
was skipped before, only now it is sayable.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ruamel.yaml.scalarstring import ScalarString

from companion.backups import make_backup
from companion.yaml_resolver import (
    INCLUDE_TAGS,
    CircularIncludeError,
    UnknownIncludeTagError,
    YamlResolver,
    claims_to_include,
)

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

# Derived from the resolver's enumeration rather than restated: this module and
# yaml_resolver.py must agree on which tags extend the config graph, and two
# hand-maintained lists of the same fact drift (TC-7). The resolver owns the
# fact; here we only subtract the single-file tag.
_INCLUDE_DIR_TAGS = INCLUDE_TAGS - {"!include"}
_YAML_SUFFIXES = (".yaml", ".yml")

# A backslash immediately before a line break: inside a double-quoted scalar YAML
# joins those lines with no separator, so a token can span the break.
_ESCAPED_NEWLINE_RE = re.compile(r"\\\r?\n")


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


# Why a file the config graph names was not read. A closed vocabulary, and
# deliberately no finer than the distinctions the walk already makes: each value
# below is one branch the code already had, given a name. A reason the walk
# cannot actually tell apart would be invented rather than observed.
SKIP_MISSING = "missing"  # the file or include directory is not there
SKIP_UNREADABLE = "unreadable"  # refused: path guard (secrets.yaml), OS permissions, outside the config dir
SKIP_UNPARSEABLE = "unparseable"  # the file could not be turned into a tree
SKIP_CIRCULAR = "circular"  # an include cycle


@dataclass(frozen=True)
class SkippedFile:
    location: str  # config-relative path of the file or directory, e.g. "packages/energy.yaml"
    reason: str  # one of SKIP_* above


class SkipLog:
    """What a config walk did not read, collected while it walks.

    One log passed into the walk rather than a second return value, because both
    walkers here are shaped differently — :func:`iter_config_trees` is a
    generator (whose ``return`` value only surfaces through ``StopIteration``)
    and :func:`replace_yaml_literal` already returns its change list. An
    accumulator both hand the same object to is the one mechanism that cannot
    drift into two half-agreeing ones, which is how ``ref scan`` and ``ent
    related`` once disagreed about the same config.

    Records are deduplicated and sorted: two files can ``!include`` the same
    missing target, and the walk does not mark a file it never read as seen, so
    the raw stream repeats. A caller reads a set of facts about the config, not
    a trace of the traversal.
    """

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: set[tuple[str, str]] = set()

    def record(self, location: str, reason: str) -> None:
        self._records.add((location, reason))

    def files(self) -> list[SkippedFile]:
        return [SkippedFile(location, reason) for location, reason in sorted(self._records)]

    def __bool__(self) -> bool:
        return bool(self._records)

    def __len__(self) -> int:
        return len(self._records)


def skipped_fields(skipped: SkipLog | None) -> dict[str, Any]:
    """The wire fields a config-walking route adds for what it could not read.

    ``skipped`` is **absent** — not empty, not null — when the walk read the
    whole graph, so a complete scan's response is byte-identical to the one this
    service sent before the field existed and no existing consumer can see a
    difference. Present, it means the answer covers less than the config does.

    One helper rather than one literal per route, for the same reason
    :func:`companion.core_api.reload_fields` is one: "absent unless abnormal" is
    the kind of rule that gets re-derived correctly n-1 times (#94).
    """
    if not skipped:
        return {}
    return {"skipped": [{"location": f.location, "reason": f.reason} for f in skipped.files()]}


def _record_skip(skipped: SkipLog | None, location: str, reason: str) -> None:
    """Note one unread file, if the caller asked to be told. Never alters the walk."""
    if skipped is not None:
        skipped.record(location, reason)


class _Unread:
    """Sentinel: this file was not read, and the reason has been recorded.

    Not ``None`` — an empty YAML file legitimately parses to ``None``, and
    conflating "nothing in it" with "never opened" is the confusion this whole
    change exists to remove.
    """


_UNREAD = _Unread()


def _load_or_skip(resolver: YamlResolver, rel: str, location: str, skipped: SkipLog | None) -> Any:
    """Parse one file, or return :data:`_UNREAD` having recorded why it was not read.

    The single classification site: both walkers call it, so the read path and
    the write path cannot label the same failure differently. The exception set
    is exactly the one both loops already caught — this only gives each branch a
    name.

    Reachability is uneven and worth stating rather than implying. ``missing``
    and ``unreadable`` are ordinary (a renamed ``!include`` target; ``!include
    secrets.yaml``, which the path guard refuses on a perfectly healthy
    instance). ``unparseable`` and ``circular`` are near-unreachable *here*:
    ``resolve=False`` follows no include, so no cycle can form, and ruamel
    signals a YAML syntax error with ``YAMLError``, which is **not** a
    ``ValueError`` and so is not caught — a malformed file aborts the scan
    loudly today. They are classified anyway because the handler already catches
    them, and a branch that is caught but unnamed is how a reason goes missing
    later.
    """
    try:
        return resolver.load(rel, resolve=False)
    except FileNotFoundError:
        _record_skip(skipped, location, SKIP_MISSING)
    except PermissionError:
        _record_skip(skipped, location, SKIP_UNREADABLE)
    except CircularIncludeError:
        _record_skip(skipped, location, SKIP_CIRCULAR)
    except ValueError:
        _record_skip(skipped, location, SKIP_UNPARSEABLE)
    return _UNREAD


def _enqueue_includes(
    data: Any,
    abs_path: Path,
    base_path: Path,
    queue: deque[str],
    skipped: SkipLog | None,
) -> None:
    """Queue every file this tree includes, and record the ones the walk cannot follow.

    Two kinds of include leave the graph shorter than the config says it is, and
    both were dropped without a trace: an ``!include_dir_*`` naming a directory
    that is not there (:func:`include_dir_files` answers with an empty list,
    which reads exactly like an empty directory — the confusion C-11 exists to
    stop, one level down from the tag), and an include target outside the config
    directory (refused by containment, C-3). Neither behaviour changes here.
    """
    for inc in _include_targets(data, abs_path.parent, base_path, skipped):
        rel_inc = _rel_within(inc, base_path)
        if rel_inc is None:
            _record_skip(skipped, _rel_to(inc, base_path), SKIP_UNREADABLE)
        else:
            queue.append(rel_inc)


def iter_config_trees(
    base: str | Path,
    entry_file: str = "configuration.yaml",
    *,
    contains: str | None = None,
    skipped: SkipLog | None = None,
) -> Iterator[tuple[str, Path, Any]]:
    """Yield ``(location, absolute path, parsed tree)`` for every reachable config file.

    One breadth-first walk of the ``!include`` / ``!include_dir_*`` graph starting
    at ``entry_file``. Each file is parsed on its own (``resolve=False``) so a
    caller can attribute what it finds to the concrete file it lives in. A file
    the walk cannot read is stepped over rather than aborting the walk — and
    written to ``skipped`` when one is supplied, so the caller can tell a config
    that holds nothing from a config this walk only partly read.

    A YAML *syntax* error is the one failure that is not stepped over: ruamel
    raises ``YAMLError``, which is not among the exceptions caught here, so it
    propagates. That is deliberate for now — see :func:`_load_or_skip`.

    A file is only enqueued after its includer has been yielded, so a consumer
    that needs parent context (e.g. "``automation:`` pointed at this file") can
    record it while walking. This is the single walk implementation shared by
    every read-only scan, so callers never disagree about which files are part
    of the config.

    ``contains`` is a pure cost optimisation for callers hunting one literal:
    a file whose raw text cannot possibly yield a match and cannot extend the
    graph is skipped without paying for a YAML parse. Round-trip parsing a
    few-hundred-KiB config costs ~0.5s, and this endpoint runs it per request,
    so the pre-filter is what keeps the scan affordable. See :func:`_may_contain`
    for exactly when a file is deemed skippable — it is deliberately
    conservative: when in doubt, parse. Such a file is *not* recorded in
    ``skipped``: it was read, proven incapable of holding the target or of
    extending the graph, and dropped on the evidence — nothing about the answer
    is partial because of it.
    """
    base_path = Path(base).resolve()
    resolver = YamlResolver(base_path)
    seen: set[str] = set()
    queue: deque[str] = deque([entry_file])

    while queue:
        rel = queue.popleft()
        abs_path = (base_path / rel).resolve()
        if str(abs_path) in seen:
            continue
        location = _rel_to(abs_path, base_path)
        if not abs_path.is_file():
            # An `!include` naming a file that was renamed or deleted. The walk
            # goes on — one broken include must not blind the rest of the scan —
            # but the caller is told, because everything downstream of here
            # reports on a config graph one file shorter than the config says.
            _record_skip(skipped, location, SKIP_MISSING)
            continue
        seen.add(str(abs_path))

        if contains is not None and not _may_contain(abs_path, contains):
            continue

        data = _load_or_skip(resolver, rel, location, skipped)
        if data is _UNREAD:
            continue

        yield location, abs_path, data

        _enqueue_includes(data, abs_path, base_path, queue, skipped)


def _may_contain(path: Path, needle: str) -> bool:
    """Whether ``path`` is worth parsing when hunting for the literal ``needle``.

    True unless all three hold:

    * the raw text does not contain ``needle`` — a string leaf can only *hold*
      the token if the bytes are there;
    * the raw text has no ``!include`` — otherwise skipping it would prune part
      of the config graph, not just this file;
    * the raw text has no escaped line continuation (``\\`` at end of line inside
      a double-quoted scalar) — that is the one YAML construct that joins two
      lines with *no* separator, so it could rejoin a token the raw text splits.
      Folded/literal blocks always insert a space or newline, which no entity_id
      token can survive, so they need no special case.

    An unreadable file returns True so the parse attempt (and its existing
    error handling) still decides.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return True
    return needle in text or "!include" in text or _ESCAPED_NEWLINE_RE.search(text) is not None


def scan_tree_for_literal(tree: Any, target: str) -> list[str]:
    """Rendered paths of every string leaf in one parsed tree containing ``target``.

    The per-file half of :func:`scan_yaml_for_literal`, exposed so a caller that
    already holds a parsed tree (from :func:`iter_config_trees`) can match with
    *this* matcher instead of writing its own — two matchers that disagree is how
    ``ref scan`` and ``ent related`` came to give contradictory answers about the
    same entity.
    """
    pattern = _target_pattern(target)
    return [render_path(match_path) for match_path in _scan_tree(tree, pattern)]


def scan_yaml_for_literal(
    base: str | Path,
    target: str,
    entry_file: str = "configuration.yaml",
    *,
    skipped: SkipLog | None = None,
) -> list[ScanHit]:
    """Find every string leaf containing ``target`` as a whole token, across the config file graph.

    A leaf matches whether it *is* ``target`` (``entity_id: sensor.gone``) or
    merely *embeds* it as a boundary-delimited token, e.g. inside a Jinja
    template string (``"{{ states('sensor.gone') }}"``). See :data:`_ENTITY_ID_RE`
    for why ``\\b`` boundaries are the right notion of "whole token" here.

    Starts from ``entry_file`` and follows ``!include`` / ``!include_dir_*``
    directives, scanning each reachable file's own parsed tree. A file that
    cannot be read is skipped rather than aborting the whole scan, and named in
    ``skipped`` when a :class:`SkipLog` is supplied — an empty hit list over a
    config with an unreadable file means "not found *here*", not "not there".
    """
    hits = [
        ScanHit(location, path, target)
        for location, _abs_path, data in iter_config_trees(base, entry_file, contains=target, skipped=skipped)
        for path in scan_tree_for_literal(data, target)
    ]
    hits.sort(key=lambda h: (h.location, h.path))
    return hits


def scan_yaml_for_entities(
    base: str | Path,
    entry_file: str = "configuration.yaml",
    *,
    skipped: SkipLog | None = None,
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

    That use is exactly why ``skipped`` matters most here: the diff is a claim
    about the whole config, and a file this walk never opened cannot contribute
    a reference to it. Supply a :class:`SkipLog` and the caller learns whether
    its "no dangling references" verdict covers everything.
    """
    refs = [
        EntityRef(location, render_path(match_path), _terminal_key(match_path), value)
        for location, _abs_path, data in iter_config_trees(base, entry_file, skipped=skipped)
        for match_path, value in _entity_leaves(data)
    ]
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
    skipped: SkipLog | None = None,
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

    A file this walk cannot read is never written — and, with a :class:`SkipLog`
    supplied, is named in it. This is the sharp end of the whole mechanism: a
    rename reported as done while an unread file quietly keeps the old id leaves
    a dangling pointer behind a success message. The change list says what *was*
    rewritten; ``skipped`` is the only thing that can say the rename was partial.
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
        if str(abs_path) in seen:
            continue
        location = _rel_to(abs_path, base_path)
        if not abs_path.is_file():
            _record_skip(skipped, location, SKIP_MISSING)
            continue
        seen.add(str(abs_path))

        # Missing / denied (e.g. secrets.yaml) / circular — skip, never write.
        data = _load_or_skip(resolver, rel, location, skipped)
        if data is _UNREAD:
            continue

        match_paths = _replace_tree(data, pattern, replacement)
        if match_paths:
            for match_path in match_paths:
                changes.append(
                    {
                        "location": location,
                        "path": render_path(match_path),
                        "before": target,
                        "after": replacement,
                    }
                )
            if not dry_run:
                # Back up the prior content before overwriting (C-5: every applied
                # write gets a backup, same as the PUT/POST config routes).
                make_backup(abs_path)
                resolver.save(rel, data)

        _enqueue_includes(data, abs_path, base_path, queue, skipped)

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


def include_tag(node: Any) -> tuple[str, str] | None:
    """``(tag, raw target)`` if ``node`` is an ``!include``-family tagged scalar, else None.

    Only meaningful on an *unresolved* tree (``resolve=False``), where ruamel
    leaves an unknown tag as a tagged scalar. Exposed because which include tag
    was used is semantic, not cosmetic: under an ``automation:`` key,
    ``!include_dir_merge_list`` means "each file holds a *list* of automations"
    while ``!include_dir_list`` means "each file *is* one automation".

    An include-family tag this module does not know raises
    :class:`UnknownIncludeTagError` rather than returning None. Returning None
    would drop the files it names out of the walked graph, and everything built
    on that walk — ``ref scan``'s hit list, ``ref validate``'s dangling-reference
    verdict, ``ref replace``'s rewrite set — would answer confidently about a
    config it had only partly read. ``ref replace`` is the sharp end: a file
    pruned from the walk keeps the old reference while the response reports
    success.
    """
    if hasattr(node, "tag") and hasattr(node, "value"):
        tag = node.tag.value if hasattr(node.tag, "value") else str(node.tag)
        raw = str(node.value).strip()
        if raw and tag in INCLUDE_TAGS:
            return tag, raw
        if raw and claims_to_include(tag):
            msg = (
                f"Unsupported include directive {tag!r} (at {raw!r}): this scan knows "
                f"{', '.join(sorted(INCLUDE_TAGS))}. The files {tag} names would be missing from the "
                f"config graph, so no answer is given rather than a partial one. Please report the tag."
            )
            raise UnknownIncludeTagError(msg)
    return None


def include_dir_files(directory: Path) -> list[Path]:
    """The YAML files an ``!include_dir_*`` tag expands to, in the resolver's order."""
    if not directory.is_dir():
        return []
    return sorted(f.resolve() for f in directory.iterdir() if f.is_file() and f.suffix in _YAML_SUFFIXES)


def _include_targets(node: Any, context_dir: Path, base_path: Path, skipped: SkipLog | None) -> list[Path]:
    """Absolute paths of files reachable via !include* tags in an unresolved tree.

    A single ``!include`` naming a file that is not there stays a target: the
    walk enqueues it and records it as missing when it fails to open, so the
    location it reports is the file the config actually names. An
    ``!include_dir_*`` naming a directory that is not there never becomes a
    target at all — :func:`include_dir_files` answers with an empty list, which
    is indistinguishable from an empty directory — so it is recorded here, at the
    only point where the difference is still visible.
    """
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
        tagged = include_tag(value)
        if tagged is None:
            return
        tag, raw = tagged
        dest = (context_dir / raw).resolve()
        if tag == "!include":
            targets.append(dest)
        elif tag in _INCLUDE_DIR_TAGS:
            if not dest.is_dir():
                _record_skip(skipped, _rel_to(dest, base_path), SKIP_MISSING)
            targets.extend(include_dir_files(dest))

    walk(node)
    return targets


def render_path(path: list[Any] | tuple[Any, ...]) -> str:
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
