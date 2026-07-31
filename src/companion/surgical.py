"""Single-entry config writes that change only the entry's own bytes.

Every CRUD route in this service loads a whole config file, mutates one entry
and writes the file back with ``yaml.dump(data, f)``. That dump is a *full
re-serialization*: ruamel round-trip keeps comments and quote style, but it
re-folds every long scalar at its own best width and re-indents every
collection to its own defaults. On a hand-maintained ``automations.yaml`` the
result is that creating one automation reformats dozens of unrelated ones —
semantically lossless, and still a defect: it clobbers hand-maintained
formatting and makes ``git diff`` on a config repo useless. (Live-fire 2026-07-30,
P1 #4: one confirmed write rewrote ~34 unrelated real automations.)

This module replaces that with a **splice**: locate the entry's own lines in
the file text, swap just those bytes, leave every other byte alone.

Why a line splice and not a smarter emitter
-------------------------------------------
ruamel's own node marks cannot draw the boundary. Composing

    - id: one
      alias: One

    # comment that documents entry two
    - id: two

reports item ``one``'s ``end_mark`` *after* the blank line and the comment —
that comment is inside ``one``'s span by the parser's accounting even though it
plainly belongs to ``two``. Symmetrically, ``yaml.dump([data[0]])`` emits that
same comment as trailing output. Using either directly would move a comment or
duplicate it. So the span is computed from the source lines: an entry owns from
its own first line down to its last line that is neither blank nor a full-line
comment. Blank lines and comments between two entries stay where they are, and
belong (as YAML convention and human intent agree) to the entry that follows.

Why the result is verified before it is written
-----------------------------------------------
Span arithmetic on text is exactly the kind of code that is right for every
file anyone thought of. So it is not trusted: the spliced text is re-parsed and
compared against the tree the route intended to write. If they differ at all —
or the splice does not parse, or the file uses a construct the splice cannot
carry, such as an anchor whose definition lives in the entry being replaced —
the whole-file dump runs instead. The surgical path is therefore never a
correctness risk over the status quo; the worst case is the behaviour this
module exists to replace, and :func:`write_fields` says so on the wire.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any, Literal

from aiohttp import web
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from companion.backups import make_backup
from companion.pathguard import is_denied, is_within

#: What the route did to the container it loaded.
#:
#: ``where`` addresses the entry **in the file as it was read** — a list index
#: or a mapping key. For ``append`` it is ignored: a new entry goes at the end
#: of the file, after any trailing comment, because that is the only position
#: that disturbs nothing.
EditKind = Literal["replace", "delete", "append"]


@dataclass(frozen=True)
class Edit:
    """One single-entry change to a top-level YAML list or mapping."""

    kind: EditKind
    where: int | str = -1


def contained(base: str | Path, path: str | Path) -> Path:
    """``path``, proven to be a config file this service may touch (C-3).

    The routes reach their target through :mod:`companion.wiring`, which already
    resolves the ``!include`` inside the config base. This re-establishes the
    precondition at the point of use rather than inheriting it: an unchecked
    write is the one mistake here that cannot be taken back, and a future caller
    that reaches this module without going through the wiring resolver would
    otherwise get no check at all. The rules are ``pathguard``'s, the same ones
    ``PUT /v1/config/file`` applies.
    """
    root = Path(base).resolve()
    target = Path(path).resolve()
    if not is_within(target, root):
        raise web.HTTPBadRequest(text="Path traversal is not allowed")
    if is_denied(target.name):
        raise web.HTTPForbidden(text=f"Access to {target.name} is denied")
    return target


def read_source(base: str | Path, path: str | Path) -> str:
    """The file's text with its line endings intact.

    ``Path.read_text`` runs universal-newline translation, so a CRLF file arrives
    as LF and every splice would write it back LF — the whole file changed, under
    a promise that only one entry did. Reading with ``newline=""`` keeps the
    bytes honest; :func:`_splice` then emits its replacement in the file's own
    convention.
    """
    with open(contained(base, path), encoding="utf-8", newline="") as stream:
        return stream.read()


def write_fields(surgical: bool) -> dict[str, Any]:
    """The formatting field a write route puts on the wire.

    ``reformatted`` is **absent** when the write was surgical, present and true
    when the file had to be re-serialized whole — the same "present only when it
    matters" shape :func:`companion.core_api.reload_fields` uses for
    ``reload_error``, so a normal success response is byte-identical to the one
    this service sent before the field existed.

    Silence would be the bug here. A caller that hands its config directory to
    git needs to know the difference between "your entry changed" and "the file
    was rewritten"; a fallback nobody is told about is indistinguishable from
    the defect.
    """
    return {} if surgical else {"reformatted": True}


def save_entry(base: str | Path, path: str | Path, data: Any, source: str, edit: Edit, yaml: YAML) -> bool:
    """Write ``data`` back to ``path``, rewriting only the bytes ``edit`` names.

    ``source`` is the file text the route parsed ``data`` from, before it
    mutated it. ``data`` is the mutated tree — the whole-file fallback dumps it,
    and the verification step compares against it.

    The prior content is backed up first (C-5) either way. Returns True when the
    surgical path produced the file, False when the whole-file dump did.
    """
    target = contained(base, path)
    spliced = _splice(source, data, edit, yaml)
    if spliced is not None and not _parses_back_to(spliced, data, yaml):
        spliced = None

    make_backup(target)
    with target.open("w", encoding="utf-8", newline="") as stream:
        if spliced is None:
            yaml.dump(data, stream)
        else:
            stream.write(spliced)
    return spliced is not None


# ---------------------------------------------------------------------------
# splice
# ---------------------------------------------------------------------------


def _splice(source: str, data: Any, edit: Edit, yaml: YAML) -> str | None:
    """The file text with only ``edit``'s entry rewritten, or None if unsupported.

    Returning None is not a failure — it hands the write to the whole-file dump,
    which is what this service did for every write until now.
    """
    try:
        root = yaml.compose(StringIO(source))
    except YAMLError:
        return None

    if root is None:
        # An empty file, or one holding nothing but comments. There are no
        # entries to preserve, but a header comment may be there — and the first
        # entry appended after it disturbs nothing, so this is still surgical.
        return _append(source, data, edit, yaml, "") if edit.kind == "append" else None

    if getattr(root, "flow_style", None):
        # A flow-style top level (`[]`, `{a: 1}`) has no per-entry block to
        # splice — and no hand-maintained line structure to protect either.
        return None

    starts = _entry_line_starts(source, root)
    if starts is None:
        return None

    if edit.kind == "append":
        return _append(source, data, edit, yaml, " " * root.start_mark.column)

    if edit.where not in starts:
        return None
    ordered = list(starts)
    position = ordered.index(edit.where)
    start = starts[edit.where]
    limit = starts[ordered[position + 1]] if position + 1 < len(ordered) else len(source)
    end = _content_end(source, start, limit)

    if edit.kind == "delete":
        return source[:start] + source[end:]

    block = _entry_block(data, edit, yaml)
    if block is None:
        return None
    indent = source[start : start + root.start_mark.column]
    if indent.strip():
        return None
    return source[:start] + _reindent(block, indent, _line_ending(source)) + source[end:]


def _append(source: str, data: Any, edit: Edit, yaml: YAML, indent: str) -> str | None:
    """``source`` with the new entry added at the end, after any trailing comment.

    End-of-file is the one insertion point that needs no span at all, so an
    append never disturbs an existing byte. A trailing comment stays above the
    new entry rather than being pushed down — it was written about the file, or
    about the entry it already followed, and either way moving it would be a
    guess.
    """
    block = _entry_block(data, edit, yaml)
    if block is None:
        return None
    eol = _line_ending(source)
    tail = source if source.endswith("\n") or not source else source + eol
    return tail + _reindent(block, indent, eol)


def _entry_line_starts(source: str, root: Any) -> dict[Any, int] | None:
    """Character offset of the first line of every top-level entry, keyed by index/key.

    For a sequence the entry starts at its ``-`` indicator, which the composer
    does not mark; it is recovered by walking back from the item's first content
    character over what must be nothing but the indicator and whitespace. Any
    layout where that does not hold (a flow item, a same-line neighbour) returns
    None so the caller falls back rather than guessing.
    """
    starts: dict[Any, int] = {}
    if root.id == "sequence":
        anchors: list[tuple[Any, Any]] = list(enumerate(root.value))
    elif root.id == "mapping":
        anchors = [(key.value, key) for key, _value in root.value]
    else:
        return None

    for where, node in anchors:
        content = node.start_mark.index
        if root.id == "sequence":
            dash = source.rfind("-", 0, content)
            if dash < 0 or source[dash + 1 : content].strip():
                return None
            content = dash
        line_start = source.rfind("\n", 0, content) + 1
        if source[line_start:content].strip():
            # Something other than indentation shares the entry's first line —
            # `- a: 1` inside a flow context, or a compact nested sequence.
            return None
        starts[where] = line_start
    return starts


def _content_end(source: str, start: int, limit: int) -> int:
    """End of the entry that begins at ``start``, excluding what follows it.

    ``limit`` is where the next entry's first line begins (or EOF). Blank lines
    and full-line comments immediately before it are *not* part of this entry:
    they introduce the next one, or trail the file. Excluding them is what keeps
    a comment from being carried off by the entry above it.
    """
    end = limit
    while end > start:
        line_start = max(start, source.rfind("\n", start, end - 1) + 1)
        line = source[line_start:end]
        if line.strip() and not line.lstrip().startswith("#"):
            return end
        end = line_start
    return end


def _entry_block(data: Any, edit: Edit, yaml: YAML) -> str | None:
    """The entry, serialized alone, as the lines that replace its old ones.

    Trailing blank lines and comments are dropped from the emitted block for the
    same reason :func:`_content_end` excludes them from the span: ruamel attaches
    the comment *between* two entries to the first one, so a standalone dump ends
    with text that belongs to the entry below. It is only dropped from the
    replacement — the file's own copy is outside the span and never moves.
    """
    try:
        if isinstance(data, list):
            index = -1 if edit.kind == "append" else int(edit.where)
            payload: Any = [data[index]]
        else:
            # Reuse the key object the file was parsed with, so a quoted key stays
            # quoted; a brand-new key has no stored object and is used as given.
            key = next((k for k in data if k == edit.where), edit.where)
            payload = {key: data[edit.where]}
    except (KeyError, IndexError, TypeError, ValueError):
        return None

    buffer = StringIO()
    try:
        yaml.dump(payload, buffer)
    except YAMLError:
        return None
    block = buffer.getvalue()
    return block[: _content_end(block, 0, len(block))]


def _line_ending(source: str) -> str:
    """The file's own line ending, so a spliced block matches the lines around it."""
    return "\r\n" if "\r\n" in source else "\n"


def _reindent(block: str, indent: str, eol: str) -> str:
    """Shift a block emitted at column 0 to the container's column and line ending."""
    lines = block.splitlines(keepends=True)
    out = [(indent + line if indent and line.strip() else line) for line in lines]
    if eol != "\n":
        out = [line[:-1] + eol if line.endswith("\n") else line for line in out]
    return "".join(out)


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def _parses_back_to(spliced: str, data: Any, yaml: YAML) -> bool:
    """True if ``spliced`` parses to exactly the tree the route meant to write.

    This is the whole safety argument. Text arithmetic that is subtly wrong for
    some file nobody thought of produces a tree that differs from ``data``, and
    the caller writes the whole-file dump instead — the pre-existing behaviour.
    So the splice can only ever change *formatting*, never content.
    """
    try:
        reparsed = yaml.load(StringIO(spliced))
    except YAMLError:
        return False
    return bool(_plain(reparsed) == _plain(data))


def _plain(node: Any) -> Any:
    """Project a ruamel tree onto plain Python, so two parses compare by value.

    ruamel's scalars are ``str``/``int`` subclasses and compare fine, but a
    tagged scalar (``!secret home_lat``) is an opaque object that never equals
    another parse of itself. Rendering it as its tag and text makes an unchanged
    ``!secret`` compare equal and a moved one compare different, which is what
    the check is for.
    """
    if isinstance(node, dict):
        return {_plain(k): _plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_plain(v) for v in node]
    if isinstance(node, str):
        return str(node)
    if hasattr(node, "tag") and hasattr(node, "value"):
        tag = node.tag.value if hasattr(node.tag, "value") else str(node.tag)
        return f"{tag} {node.value}"
    return node
