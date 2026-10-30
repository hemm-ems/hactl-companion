"""Substring / regex search across the config file graph (``GET /v1/ref/grep``).

``ref scan`` answers "where is this *id* referenced?" with a whole-token match.
This answers "where does this *term* appear?": a substring (or a pattern) in any
string leaf **or mapping key** of any file reachable from ``configuration.yaml``.
It exists because every search hactl had matched a whole value or an exact id,
so ``balkony`` found nothing while ``sensor.balkony_p_ac`` sat in two places
(hactl #115).

It is a separate module, and the route a separate route, on purpose (hactl
D-48): the substring matcher lives nowhere near ``replace_yaml_literal``, so no
substring is ever one parameter away from a write. What it shares with
``ref scan`` is the *walk* — :func:`companion.refscan.iter_config_trees` and its
:class:`~companion.refscan.SkipLog` — because two walks that disagree about
which files are config is how ``ref scan`` and ``ent related`` once contradicted
each other.

Three choices here are measured, not assumed (workspace ``SPEC-global-grep.md``,
lane B oracle results):

* **No raw-text prefilter.** ``iter_config_trees(contains=...)`` skips a file
  whose bytes lack the needle. That is sound for an entity id and unsound for a
  case-insensitive substring: it is case-sensitive, and even lowercased it misses
  a term spanning a folded plain scalar (``Küche⏎    Licht`` parses to
  ``Küche Licht``) and a non-ASCII term written as a YAML escape. A regex cannot
  use it at all. A full walk measured 2.8 to 3.5 s on a real house against hactl's
  30 s timeout, so the walk always reads everything.
* **Matching rule (hactl D-49).** The *simple* Unicode lowercase mapping on
  both sides, one code point to one code point (:func:`simple_lower`) — the
  mapping Go's ``strings.ToLower`` applies on the dashboard half of the same
  search. Never ``casefold()``, which maps ``ß`` to ``ss`` and has no Go twin.
  Not whole-string ``str.lower()`` either: that applies SpecialCasing
  (``İ`` → ``i`` + U+0307) and the final-sigma context rule (``Σ`` → ``ς`` at a
  word end), so ``istanbul`` missed ``İstanbul`` and ``οδοσ`` missed ``ΟΔΟΣ``
  while Go and ``re.IGNORECASE`` found both. Measured over every code point:
  per-code-point ``str.lower()`` with :data:`SIMPLE_LOWER_OVERRIDES` equals Go
  1.26's ``unicode.ToLower`` exactly on Python 3.12 (both Unicode 15.0.0, the
  production image). A newer interpreter's Unicode adds mappings only for
  characters added after 15.0 (Python 3.14, Unicode 16.0: 30 code points,
  e.g. Garay). ``re.IGNORECASE`` agrees on every non-ASCII case the oracle
  probed (``Küche``/``KÜCHE``, ``ß``/``ẞ``, no ``ß``→``SS``).
* **A hit cap with exact counts.** :data:`HIT_CAP` rows at most, ``truncated``
  and ``total`` always stated, per-file counts computed before the cap.

A regex is the one input that can make this route *hang*: Python's ``re``
backtracks, and a pattern Go's RE2 compiles happily (``([a-z]+ ?)+$``) takes
minutes on a 30-character leaf — measured, 2.6 s at 24 characters and doubling
per character. The companion serves every route from one event loop, so that
would stall all of it. Patterns are therefore evaluated in a child interpreter
with a deadline (:data:`REGEX_DEADLINE_S`) and killed when they overrun; the
literal path is linear and needs none of this.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
import sys
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from companion.refscan import SkipLog, iter_config_trees, render_path

#: Most hit rows one response carries (oracle resolution 1).
HIT_CAP = 1000

#: Shortest literal term, in code points (oracle resolution 3). A 1-character
#: term matched 62 % (``a``) and 75 % (``e``) of every string leaf on the
#: measured house. From 2 up length does not predict a flood (``ab`` 277 leaves,
#: ``licht`` 627, ``on`` 1,769) so there is no further rule; the cap handles it.
#: A regex is not length-gated: ``\d{4}`` is short and legitimate.
MIN_LITERAL_LEN = 2

#: Seconds a pattern may take over the whole candidate set before it is killed.
#: Matching 7.4k leaves took 1 to 5 ms on the measured house; the deadline exists
#: only for catastrophic backtracking, and stays well inside hactl's 30 s budget
#: together with the walk itself.
REGEX_DEADLINE_S = 5.0

KIND_KEY = "key"
KIND_VALUE = "value"


#: Code points whose ``str.lower()`` is longer than one code point, mapped to
#: their simple (UnicodeData.txt) lowercase instead. U+0130 is the only one in
#: Unicode 15.0 and 16.0; ``tests/test_refgrep.py`` fails if that changes.
SIMPLE_LOWER_OVERRIDES: dict[int, str] = {0x0130: "i"}


@functools.cache
def _simple_lower_table() -> dict[int, str]:
    table: dict[int, str] = {}
    for cp in range(sys.maxunicode + 1):
        lowered = chr(cp).lower()
        if lowered != chr(cp):
            table[cp] = lowered
    table.update(SIMPLE_LOWER_OVERRIDES)
    return table


def simple_lower(text: str) -> str:
    """Lowercase ``text`` one code point at a time by the simple mapping (hactl D-49).

    A per-character ``str.translate``, so no context rule (final sigma) applies
    and no character grows (``İ``), unlike whole-string ``str.lower()``.
    """
    return text.translate(_simple_lower_table())


class GrepRequestError(ValueError):
    """The request cannot be answered as asked; the message is the 400 text."""


@dataclass(frozen=True)
class GrepHit:
    location: str  # config-relative file, e.g. "automations.yaml"
    path: str  # rendered path within that file; for a key hit it ends at the key
    key: str  # last mapping key on the path ("" when only list indices lead here)
    kind: str  # KIND_KEY or KIND_VALUE
    matched_value: str  # the whole string that matched — the leaf, or the key


@dataclass(frozen=True)
class SourceCount:
    location: str
    key_hits: int
    value_hits: int


@dataclass(frozen=True)
class GrepResult:
    hits: list[GrepHit]  # at most HIT_CAP, sorted
    total: int  # every hit, before the cap
    sources: list[SourceCount]  # every file with a hit, before the cap

    @property
    def truncated(self) -> bool:
        return len(self.hits) < self.total


@dataclass(frozen=True)
class _Candidate:
    location: str
    path: tuple[Any, ...]
    key: str
    kind: str
    text: str


def validate_term(term: str, *, regex: bool, case_sensitive: bool) -> re.Pattern[str] | None:
    """Refuse a term this route will not search for; compile it when it is a pattern.

    Returns the compiled pattern for ``regex=True``, else ``None``. Raises
    :class:`GrepRequestError` with the text the caller should see.

    A pattern Python *warns* about at compile time is refused too. The one such
    warning ``re`` emits is ``FutureWarning: Possible nested set`` for
    ``[[:alpha:]]`` — which RE2 reads as a POSIX class and Python as something
    else (oracle resolution 4). Answering it would be answering a question the
    caller did not ask.
    """
    if not term.strip():
        raise GrepRequestError("Missing term parameter: give a non-empty, non-whitespace term")
    if not regex:
        if len(term) < MIN_LITERAL_LEN:
            raise GrepRequestError(
                f"term {term!r} is shorter than {MIN_LITERAL_LEN} characters: a 1-character term matches most "
                "of any real config. Use a longer term, or regex=true for a deliberate short pattern."
            )
        return None
    flags = 0 if case_sensitive else re.IGNORECASE
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        try:
            return re.compile(term, flags)
        # `re` reports two kinds of uncompilable pattern outside `re.error`:
        # OverflowError for a repeat count past its C limit (`a{4294967296}`)
        # and RecursionError for deep group nesting (500 levels, which RE2
        # accepts, so hactl sends it). Uncaught they are a 500, and hactl
        # retries a GET on any 5xx.
        except (re.error, OverflowError, RecursionError) as exc:
            raise GrepRequestError(f"Invalid regex: {exc}") from exc
        except Warning as exc:
            raise GrepRequestError(f"Ambiguous regex: {exc}") from exc


def _candidates(node: Any, location: str, path: tuple[Any, ...], key: str) -> Iterator[_Candidate]:
    """Every searchable string in one parsed tree: each str mapping key, each str leaf.

    ``!include``-family and other tagged scalars (``!secret``, ``!input``) are
    neither ``str``, ``dict`` nor ``list`` and fall through: an include's content
    is searched when the walk reaches that file, and a secret's name is not
    config text. Non-string keys and leaves (numbers, booleans) are not searched,
    the same rule ``ref scan`` applies.
    """
    if isinstance(node, str):
        yield _Candidate(location, path, key, KIND_VALUE, str(node))
    elif isinstance(node, dict):
        for child_key, value in node.items():
            child_path = (*path, child_key)
            child_name = key
            if isinstance(child_key, str):
                child_name = str(child_key)
                yield _Candidate(location, child_path, child_name, KIND_KEY, child_name)
            yield from _candidates(value, location, child_path, child_name)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _candidates(value, location, (*path, index), key)


def collect_candidates(
    base: str | Path,
    entry_file: str = "configuration.yaml",
    *,
    skipped: SkipLog | None = None,
) -> list[_Candidate]:
    """Walk the whole config graph (no prefilter — see the module docstring)."""
    return [
        candidate
        for location, _abs_path, data in iter_config_trees(base, entry_file, skipped=skipped)
        for candidate in _candidates(data, location, (), "")
    ]


def literal_matches(candidates: list[_Candidate], term: str, *, case_sensitive: bool) -> list[_Candidate]:
    """Substring match under hactl D-49: :func:`simple_lower` on both sides unless case-sensitive."""
    if case_sensitive:
        return [c for c in candidates if term in c.text]
    needle = simple_lower(term)
    return [c for c in candidates if needle in simple_lower(c.text)]


# Runs in a separate, isolated (-I) interpreter: standard library only, nothing
# from this package, and no import from the working directory.
_REGEX_WORKER = """
import json, re, sys
request = json.load(sys.stdin)
pattern = re.compile(request["pattern"], request["flags"])
json.dump([i for i, text in enumerate(request["texts"]) if pattern.search(text)], sys.stdout)
"""


async def regex_matches(candidates: list[_Candidate], pattern: re.Pattern[str]) -> list[_Candidate]:
    """``pattern.search`` over every candidate, in a child interpreter killed at the deadline.

    The pattern was already compiled (and so validated) here by
    :func:`validate_term`; the child recompiles the same source with the same
    flags. An overrun is a :class:`GrepRequestError` — a 4xx, delivered once:
    hactl retries a GET on any 5xx, and the same pattern would overrun again.
    """
    if not candidates:
        return []
    limit = REGEX_DEADLINE_S
    payload = json.dumps({"pattern": pattern.pattern, "flags": pattern.flags, "texts": [c.text for c in candidates]})
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        _REGEX_WORKER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(payload.encode("utf-8")), timeout=limit)
    except TimeoutError:
        msg = (
            f"Regex did not finish within {limit:g} s over {len(candidates)} config strings, most likely "
            "catastrophic backtracking (a nested repeat such as (a+)+). Simplify the pattern."
        )
        raise GrepRequestError(msg) from None
    finally:
        # Also on cancellation (the client hung up): a child left running a
        # runaway pattern would spin for as long as the pattern takes.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        msg = f"regex worker exited with status {proc.returncode}: {stderr.decode('utf-8', 'replace').strip()}"
        raise RuntimeError(msg)
    indices: list[int] = json.loads(stdout)
    return [candidates[i] for i in indices]


def _sort_key(candidate: _Candidate) -> tuple[str, str, str]:
    # The same (location, path) order ref scan sorts by; `key` before `value`
    # when a key and its own string value both match at one path.
    return (candidate.location, render_path(candidate.path), candidate.kind)


def summarise(matches: list[_Candidate], *, cap: int = HIT_CAP) -> GrepResult:
    """Sort, count per file, then cap — counts first, so a truncated answer still says how much there was."""
    ordered = sorted(matches, key=_sort_key)
    per_file: dict[str, list[int]] = {}
    for candidate in ordered:
        counts = per_file.setdefault(candidate.location, [0, 0])
        counts[0 if candidate.kind == KIND_KEY else 1] += 1
    hits = [GrepHit(c.location, render_path(c.path), c.key, c.kind, c.text) for c in ordered[:cap]]
    sources = [SourceCount(location, keys, values) for location, (keys, values) in sorted(per_file.items())]
    return GrepResult(hits=hits, total=len(ordered), sources=sources)


async def grep_config(
    base: str | Path,
    term: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
    entry_file: str = "configuration.yaml",
    skipped: SkipLog | None = None,
    cap: int = HIT_CAP,
) -> GrepResult:
    """Every key and string leaf in the config graph containing ``term`` (or matching it, with ``regex``).

    Raises :class:`GrepRequestError` for a term the route refuses (see
    :func:`validate_term`) and for a pattern that overruns its deadline. A file
    the walk could not read is named in ``skipped`` — an empty result over such a
    config means "not found in what was read".
    """
    pattern = validate_term(term, regex=regex, case_sensitive=case_sensitive)
    candidates = collect_candidates(base, entry_file, skipped=skipped)
    if pattern is None:
        matches = literal_matches(candidates, term, case_sensitive=case_sensitive)
    else:
        matches = await regex_matches(candidates, pattern)
    return summarise(matches, cap=cap)
