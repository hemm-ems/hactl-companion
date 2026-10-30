"""``GET /v1/ref/grep`` — term in, every config place it appears out (hactl #115).

The cases follow the acceptance criteria in the workspace ``SPEC-global-grep.md``
and the lane B oracle resolutions it records:

* substring, not whole token, and inside template bodies (AC-1);
* case-insensitive by the simple per-code-point lowercase mapping (Go's
  ``strings.ToLower``) — a non-ASCII pin, the two places ``casefold`` would
  answer differently, and the two where whole-string ``str.lower()`` would
  (D-49 / AC-2);
* found even where the raw file text does not hold the term (the reason the
  ``ref scan`` prefilter is not used here);
* mapping keys searched as their own ``kind`` (resolution 1);
* refusals: empty, whitespace, shorter than 2 code points, invalid or ambiguous
  regex, a pattern that overruns its deadline (AC-5, AC-6, resolution 3);
* a stated cap with exact counts (AC-3);
* the partial-walk record and the path guard, same as every ref route (AC-4);
* read-only, and ``ref scan`` / ``ref replace`` unchanged byte for byte (AC-7).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient

from companion import refgrep
from companion.refgrep import HIT_CAP, GrepRequestError, validate_term
from companion.refscan import SKIP_MISSING, SKIP_UNREADABLE


def _write(config_dir: Path, name: str, text: str) -> Path:
    path = config_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _only_config(config_dir: Path, text: str) -> None:
    """Make ``configuration.yaml`` the whole graph (other fixtures become unreachable)."""
    _write(config_dir, "configuration.yaml", text)


async def _grep(client: TestClient, auth_headers: dict[str, str], **params: str) -> dict[str, Any]:
    resp = await client.get("/v1/ref/grep", params=params, headers=auth_headers)
    assert resp.status == 200, await resp.text()
    body: dict[str, Any] = await resp.json()
    return body


async def _refused(client: TestClient, auth_headers: dict[str, str], **params: str) -> str:
    resp = await client.get("/v1/ref/grep", params=params, headers=auth_headers)
    assert resp.status == 400, f"{params}: answered {resp.status}: {await resp.text()}"
    body = await resp.json()
    message: str = body["error"]["message"]
    return message


def _values(body: dict[str, Any]) -> list[str]:
    return [h["matched_value"] for h in body["hits"]]


# ---------------------------------------------------------------------------
# Substring, inside template bodies (AC-1)
# ---------------------------------------------------------------------------


async def test_substring_hit_inside_a_template_body(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """The field report: ``balkony`` must find the id embedded in a template, which ``ref scan`` cannot."""
    template = "{{ states('sensor.balkony_p_ac') | float(0) > 50 }}"
    _only_config(config_dir, f'template:\n  - binary_sensor:\n      - name: Sun\n        state: "{template}"\n')

    body = await _grep(client, auth_headers, term="balkony")

    assert body["hits"] == [
        {
            "location": "configuration.yaml",
            "path": "template[0].binary_sensor[0].state",
            "key": "state",
            "kind": "value",
            "matched_value": template,
        }
    ]
    assert body["total"] == 1
    assert body["truncated"] is False
    assert body["sources"] == [{"location": "configuration.yaml", "key_hits": 0, "value_hits": 1}]
    assert "skipped" not in body

    scan = await client.get("/v1/ref/scan", params={"target": "balkony"}, headers=auth_headers)
    assert (await scan.json())["hits"] == [], "ref scan is whole-token: the contrast this route exists for"


async def test_hits_follow_the_include_graph_and_name_the_file(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    _only_config(config_dir, "automation: !include automations.yaml\nsensor:\n  - platform: x\n    name: Balkony\n")
    _write(
        config_dir,
        "automations.yaml",
        "- id: a1\n  alias: Balkon\n  triggers:\n    - trigger: state\n      entity_id: sensor.balkony_p_ac\n",
    )

    body = await _grep(client, auth_headers, term="balkony")

    assert [(h["location"], h["path"]) for h in body["hits"]] == [
        ("automations.yaml", "[0].triggers[0].entity_id"),
        ("configuration.yaml", "sensor[0].name"),
    ]
    assert [s["location"] for s in body["sources"]] == ["automations.yaml", "configuration.yaml"]


# ---------------------------------------------------------------------------
# Case: simple lowercase, not casefold, not full str.lower() (D-49 / AC-2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("term", ["küche", "KÜCHE", "Küche", "kÜchE"])
async def test_case_insensitive_including_non_ascii(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, term: str
) -> None:
    _only_config(
        config_dir, "script:\n  a:\n    alias: Küche Licht\n  b:\n    alias: KÜCHE AUS\n  c:\n    alias: Bad\n"
    )

    body = await _grep(client, auth_headers, term=term)

    assert _values(body) == ["Küche Licht", "KÜCHE AUS"]


async def test_case_sensitive_compares_as_written(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    _only_config(config_dir, "script:\n  a:\n    alias: Küche Licht\n  b:\n    alias: KÜCHE AUS\n")

    body = await _grep(client, auth_headers, term="KÜCHE", case_sensitive="true")

    assert _values(body) == ["KÜCHE AUS"]
    assert body["case_sensitive"] is True


async def test_lowercase_not_casefold(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """The two answers that tell ``str.lower()`` from ``casefold()``.

    ``casefold`` maps ``ß`` to ``ss``, so it would find ``STRASSE`` for
    ``straße``; Go's ``strings.ToLower`` (the dashboard half of the same search)
    would not, and the two halves would disagree. Capital ``ẞ`` lowercases to
    ``ß`` under both rules, so it is found.
    """
    _only_config(config_dir, "a: STRASSE 1\nb: STRAẞE 2\nc: Straße 3\n")

    body = await _grep(client, auth_headers, term="straße")

    assert _values(body) == ["STRAẞE 2", "Straße 3"]
    assert "straße".casefold() in "STRASSE 1".casefold(), "premise: casefold WOULD have matched"


@pytest.mark.parametrize(
    ("text", "term"),
    [
        # str.lower() maps U+0130 to "i" + U+0307 (SpecialCasing); the simple
        # mapping, Go's unicode.ToLower and re.IGNORECASE all give plain "i".
        ("İstanbul", "istanbul"),
        ("İstanbul", "ISTANBUL"),
        # str.lower() turns a word-final capital sigma into final "ς"; the simple
        # mapping is U+03C3 (non-final sigma) in every position.
        ("ΟΔΟΣ", "οδοσ"),
        ("ΟΔΟΣ ΑΘΗΝΑΣ", "οδοσ αθηνασ"),
    ],
    ids=["dotted-I", "dotted-I-ascii-upper-term", "final-sigma", "final-sigma-mid-string"],
)
async def test_literal_uses_the_simple_mapping_and_agrees_with_regex(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, text: str, term: str
) -> None:
    """The literal matcher lowercases per code point, by the simple mapping — Go's ``strings.ToLower``.

    Whole-string ``str.lower()`` applies SpecialCasing (``İ`` → ``i̇``) and the
    final-sigma context rule (``Σ`` → ``ς`` at a word end), so it missed both
    of these while ``regex=true`` and the dashboard half found them.
    """
    _only_config(config_dir, f"a: {text}\nb: other\n")
    assert term.lower() not in text.lower(), "premise: whole-string str.lower() misses this"

    literal = await _grep(client, auth_headers, term=term)
    pattern = await _grep(client, auth_headers, term=term, regex="true")

    assert _values(literal) == [text]
    assert _values(pattern) == [text]


def test_simple_lower_is_per_code_point_and_one_to_one() -> None:
    """Every code point lowers to exactly one code point, ``str.lower()``'s own wherever that is one.

    ``U+0130`` is the one code point whose ``str.lower()`` is longer than one
    (Unicode 15.0 and 16.0 both). Should a Unicode update add another, this
    fails instead of the override table silently going stale.
    """
    multi = {cp for cp in range(sys.maxunicode + 1) if len(chr(cp).lower()) != 1}
    assert multi == set(refgrep.SIMPLE_LOWER_OVERRIDES)
    for cp in range(sys.maxunicode + 1):
        lowered = refgrep.simple_lower(chr(cp))
        assert len(lowered) == 1, hex(cp)
        if cp not in multi:
            assert lowered == chr(cp).lower(), hex(cp)
    assert refgrep.simple_lower("İ") == "i"
    assert refgrep.simple_lower("ΟΔΟΣ") == "οδοσ"


async def test_found_where_the_raw_text_does_not_hold_the_term(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """The cases that make ``ref scan``'s raw-text prefilter unsound for this route (oracle, resolution 2).

    A plain scalar folded over two lines parses to one space-joined string, and
    a YAML escape spells a character the file never contains. A prefilter on the
    file's bytes would skip this file and answer "nothing".
    """
    _only_config(config_dir, 'a:\n  alias: Küche\n    Licht an\nb: "K\\u00fcche"\n')
    raw = (config_dir / "configuration.yaml").read_text(encoding="utf-8")
    assert "küche licht" not in raw.lower()
    assert "Küche" in raw  # only once, in `a`; `b` holds the escape

    body = await _grep(client, auth_headers, term="küche licht")
    assert _values(body) == ["Küche Licht an"]

    escaped = await _grep(client, auth_headers, term="küche")
    assert _values(escaped) == ["Küche Licht an", "Küche"]


# ---------------------------------------------------------------------------
# Keys (resolution 1)
# ---------------------------------------------------------------------------


async def test_mapping_keys_are_hits_of_their_own_kind(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """A script's object id lives only in a key — the definition site no leaf holds."""
    _only_config(
        config_dir,
        "script:\n  licht_an_sinje:\n    alias: Sinje\n    sequence:\n      - action: light.turn_on\n"
        "input_number:\n  licht: Licht\n",
    )

    body = await _grep(client, auth_headers, term="licht")

    assert body["hits"] == [
        {
            "location": "configuration.yaml",
            "path": "input_number.licht",
            "key": "licht",
            "kind": "key",
            "matched_value": "licht",
        },
        {
            "location": "configuration.yaml",
            "path": "input_number.licht",
            "key": "licht",
            "kind": "value",
            "matched_value": "Licht",
        },
        {
            "location": "configuration.yaml",
            "path": "script.licht_an_sinje",
            "key": "licht_an_sinje",
            "kind": "key",
            "matched_value": "licht_an_sinje",
        },
    ]
    assert body["sources"] == [{"location": "configuration.yaml", "key_hits": 2, "value_hits": 1}]


async def test_non_string_and_tagged_scalars_are_not_searched(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    _only_config(config_dir, "a: 1234\nb: true\nc: !secret probe_1234\nd: !input probe_1234\ne: x1234\n")

    body = await _grep(client, auth_headers, term="1234")

    assert _values(body) == ["x1234"]


# ---------------------------------------------------------------------------
# Regex (AC-2, AC-6)
# ---------------------------------------------------------------------------


async def test_regex_alternation(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    _only_config(config_dir, "a: sensor.wp_vl_temp\nb: sensor.WP_RL_temp\nc: sensor.wp_xx\n")

    body = await _grep(client, auth_headers, term=r"wp_(vl|rl)", regex="true")
    assert _values(body) == ["sensor.wp_vl_temp", "sensor.WP_RL_temp"]
    assert body["regex"] is True

    exact = await _grep(client, auth_headers, term=r"wp_(vl|rl)", regex="true", case_sensitive="true")
    assert _values(exact) == ["sensor.wp_vl_temp"]


async def test_regex_case_insensitive_on_non_ascii(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    _only_config(config_dir, "a: KÜCHE\nb: Küche\nc: Kuche\n")

    body = await _grep(client, auth_headers, term="k(ü|ue)che", regex="true")

    assert _values(body) == ["KÜCHE", "Küche"]


async def test_regex_is_not_length_gated(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """A short pattern is a deliberate question (``\\d``), unlike a 1-character literal (resolution 3)."""
    _only_config(config_dir, "a: '2026'\nb: none\n")

    body = await _grep(client, auth_headers, term=r"\d", regex="true")

    assert _values(body) == ["2026"]


@pytest.mark.parametrize(
    ("pattern", "parser_says"),
    [
        ("(unclosed", "missing ), unterminated subpattern"),
        ("a**", "multiple repeat"),
        ("wp_(?=vl", "missing ), unterminated subpattern"),
        # `re` raises OverflowError, not re.error, for a repeat count past its
        # C limit, and RecursionError for deep nesting. RE2 (nesting limit 1000)
        # compiles 500 groups, so hactl would send this one; a 500 here would be
        # retried three times by hactl's GET retry.
        ("a{4294967296}", "the repetition number is too large"),
        ("a{0,4294967295}", "the repetition number is too large"),
        ("(" * 500 + ")" * 500, "maximum recursion depth exceeded"),
    ],
    ids=["unclosed", "multiple-repeat", "lookahead-unclosed", "repeat-overflow", "range-overflow", "deep-nesting"],
)
async def test_invalid_regex_is_400_with_the_parser_message(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, pattern: str, parser_says: str
) -> None:
    message = await _refused(client, auth_headers, term=pattern, regex="true")

    assert message.startswith("Invalid regex: ")
    assert parser_says in message


async def test_a_pattern_python_warns_about_is_refused(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """``[[:alpha:]]`` is a POSIX class to RE2 and a nested-set warning to Python (oracle resolution 4, row 36)."""
    message = await _refused(client, auth_headers, term="[[:alpha:]]+", regex="true")

    assert message.startswith("Ambiguous regex: ")
    assert "nested set" in message


async def test_a_backtracking_pattern_is_cut_off_not_left_to_hang(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``(a+)+$`` against 40 ``a`` and a ``!`` takes Python's ``re`` hours; RE2 compiles it happily.

    Measured before this guard existed: the same shape took 2.6 s at 24
    characters and doubles with each one, and the companion serves every route
    from one event loop. The deadline is shortened here only to keep the test
    fast; the assertion is that the answer is a 400 naming the cause, delivered
    in bounded time, and that the service answers the next request.
    """
    monkeypatch.setattr(refgrep, "REGEX_DEADLINE_S", 1.0)
    _only_config(config_dir, f"a: {'a' * 40}!\n")

    started = time.monotonic()
    message = await _refused(client, auth_headers, term="(a+)+$", regex="true")
    elapsed = time.monotonic() - started

    assert "did not finish within 1 s" in message
    assert elapsed < 10, f"the deadline did not bound the request: {elapsed:.1f}s"
    health = await client.get("/v1/health")
    assert health.status == 200


# ---------------------------------------------------------------------------
# Refused terms (AC-5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params", [{}, {"term": ""}, {"term": "   "}, {"term": "\t\n"}, {"term": " ", "regex": "true"}]
)
async def test_missing_empty_or_whitespace_term_is_400(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, params: dict[str, str]
) -> None:
    message = await _refused(client, auth_headers, **params)

    assert "term" in message


@pytest.mark.parametrize("term", ["a", "ü", "1"])
async def test_one_character_literal_is_400(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, term: str
) -> None:
    """Counted in code points, so ``ü`` (two UTF-8 bytes) is one character."""
    message = await _refused(client, auth_headers, term=term)

    assert "shorter than 2 characters" in message


async def test_two_character_literal_is_searched(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    _only_config(config_dir, "a: Grün\nb: Rot\n")

    body = await _grep(client, auth_headers, term="ün")

    assert _values(body) == ["Grün"]


def test_validation_runs_before_any_walk(tmp_path: Path) -> None:
    """A refused term never pays for a parse: nothing is read at all."""
    with pytest.raises(GrepRequestError):
        validate_term("a", regex=False, case_sensitive=False)
    with pytest.raises(GrepRequestError):
        validate_term("(", regex=True, case_sensitive=False)
    assert validate_term("ab", regex=False, case_sensitive=False) is None


# ---------------------------------------------------------------------------
# The cap (AC-3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("count", "truncated"), [(HIT_CAP, False), (HIT_CAP + 7, True)])
async def test_cap_is_stated_and_counts_stay_exact(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, count: int, truncated: bool
) -> None:
    _only_config(config_dir, "probe:\n" + "".join(f"  k{i:05d}: needle {i}\n" for i in range(count)))

    body = await _grep(client, auth_headers, term="needle")

    assert body["truncated"] is truncated
    assert body["total"] == count
    assert len(body["hits"]) == min(count, HIT_CAP)
    assert body["sources"] == [{"location": "configuration.yaml", "key_hits": 0, "value_hits": count}]
    assert body["hits"][0]["path"] == "probe.k00000"
    assert body["hits"][-1]["path"] == f"probe.k{min(count, HIT_CAP) - 1:05d}"


async def test_cap_keeps_every_source_counted_even_when_its_rows_are_cut(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """A file whose rows all fall past the cap is still named, with its exact count."""
    _only_config(config_dir, "a: !include a.yaml\nz: !include z.yaml\n")
    _write(config_dir, "a.yaml", "".join(f"k{i:05d}: needle\n" for i in range(HIT_CAP + 1)))
    _write(config_dir, "z.yaml", "late: needle\n")

    body = await _grep(client, auth_headers, term="needle")

    assert body["truncated"] is True
    assert {h["location"] for h in body["hits"]} == {"a.yaml"}
    assert body["sources"] == [
        {"location": "a.yaml", "key_hits": 0, "value_hits": HIT_CAP + 1},
        {"location": "z.yaml", "key_hits": 0, "value_hits": 1},
    ]
    assert body["total"] == HIT_CAP + 2


# ---------------------------------------------------------------------------
# Partial walk and path guard (AC-4, C-3)
# ---------------------------------------------------------------------------


async def test_skipped_files_are_named_and_the_walk_goes_on(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    _write(config_dir, "secrets.yaml", "wifi: needle_secret\n")
    _only_config(
        config_dir,
        "automation: !include packages/renamed.yaml\nhomeassistant:\n  x: !include secrets.yaml\nsensor:\n"
        "  value: needle_here\n",
    )

    body = await _grep(client, auth_headers, term="needle")

    assert _values(body) == ["needle_here"]
    assert body["skipped"] == [
        {"location": "packages/renamed.yaml", "reason": SKIP_MISSING},
        {"location": "secrets.yaml", "reason": SKIP_UNREADABLE},
    ]


async def test_storage_is_never_searched(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """An ``!include`` into ``.storage`` is refused by the path guard, like every config read."""
    _write(config_dir, ".storage/core.config_entries", '{"password": "needle_credential"}\n')
    _only_config(config_dir, "x: !include .storage/core.config_entries\n")

    body = await _grep(client, auth_headers, term="needle")

    assert body["hits"] == []
    assert body["skipped"] == [{"location": ".storage/core.config_entries", "reason": SKIP_UNREADABLE}]


async def test_grep_writes_nothing(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    before = {p: p.read_bytes() for p in config_dir.rglob("*") if p.is_file()}

    await _grep(client, auth_headers, term="light")
    await _grep(client, auth_headers, term="light.*", regex="true")

    after = {p: p.read_bytes() for p in config_dir.rglob("*") if p.is_file()}
    assert after == before


async def test_requires_auth(client: TestClient, config_dir: Path) -> None:
    resp = await client.get("/v1/ref/grep", params={"term": "light"})
    assert resp.status == 401


async def test_only_get_is_routed(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """Read-only by construction (D-48): there is no method on this path that could write."""
    for method in ("POST", "PUT", "DELETE"):
        resp = await client.request(method, "/v1/ref/grep?term=light", headers=auth_headers)
        assert resp.status == 405, f"{method} answered {resp.status}"


# ---------------------------------------------------------------------------
# ref scan / ref replace are unchanged (AC-7)
# ---------------------------------------------------------------------------

#: A config where the substring rule and the whole-token rule disagree: grep
#: finds ``sensor.gone`` five times, scan/replace only where it is a whole token.
_AC7_CONFIG = (
    "sensor:\n"
    "  value: sensor.gone\n"
    "  longer: sensor.gone_bar\n"
    "  glued: asensor.gone\n"
    "  upper: SENSOR.GONE\n"
    "  tpl: \"{{ states('sensor.gone') }}\"\n"
)

# Pinned on main @ c3b202e, before /v1/ref/grep existed (same bytes on this branch).
_AC7_SCAN_BYTES = (
    b'{"target": "sensor.gone", "hits": [{"location": "configuration.yaml", "path": "sensor.tpl", '
    b'"matched_value": "sensor.gone"}, {"location": "configuration.yaml", "path": "sensor.value", '
    b'"matched_value": "sensor.gone"}]}'
)
_AC7_REPLACE_BYTES = (
    b'{"status": "dry_run", "changes": [{"location": "configuration.yaml", "path": "sensor.tpl", '
    b'"before": "sensor.gone", "after": "sensor.new"}, {"location": "configuration.yaml", "path": '
    b'"sensor.value", "before": "sensor.gone", "after": "sensor.new"}]}'
)


async def test_ref_scan_and_replace_responses_are_byte_identical(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    _only_config(config_dir, _AC7_CONFIG)

    scan = await client.get("/v1/ref/scan", params={"target": "sensor.gone"}, headers=auth_headers)
    assert scan.status == 200
    assert await scan.read() == _AC7_SCAN_BYTES

    replace = await client.post(
        "/v1/ref/replace", json={"old": "sensor.gone", "new": "sensor.new", "dry_run": True}, headers=auth_headers
    )
    assert replace.status == 200
    assert await replace.read() == _AC7_REPLACE_BYTES

    grep = await _grep(client, auth_headers, term="sensor.gone")
    assert grep["total"] == 5, "premise: the substring rule really does see more than the whole-token rule"


async def test_a_cancelled_search_does_not_leave_its_worker_running(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that hangs up mid-search must not leave a runaway pattern spinning in a child process."""
    import asyncio
    import re

    spawned: list[asyncio.subprocess.Process] = []
    real_exec = asyncio.create_subprocess_exec

    async def _recording_exec(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await real_exec(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(refgrep.asyncio, "create_subprocess_exec", _recording_exec)
    candidates = refgrep.collect_candidates(_seeded(config_dir, f"a: {'a' * 40}!\n"))
    task = asyncio.ensure_future(refgrep.regex_matches(candidates, re.compile("(a+)+$")))
    for _ in range(100):
        if spawned:
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert spawned, "no worker was started"
    assert spawned[0].returncode is not None, "the worker outlived the cancelled request"


def _seeded(config_dir: Path, text: str) -> Path:
    _only_config(config_dir, text)
    return config_dir
