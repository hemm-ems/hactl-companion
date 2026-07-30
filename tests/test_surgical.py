"""Byte-level behaviour of single-entry config writes (C-13).

Two things are pinned here, and they are the same statement from both sides:

* what the **whole-file dump** does to a hand-maintained file — the defect this
  module exists to remove, asserted directly so it cannot quietly come back as
  "that's just how YAML round-trips work";
* what the **splice** does instead — every byte outside the touched entry
  identical, proven by diffing bytes rather than by parsing and comparing values
  (a parsed comparison is exactly the check that called the original defect
  lossless and let it ship).

The fallback cases matter as much as the happy ones: a write that cannot be
spliced must still produce the right *content*, and must say so on the wire.
"""

from __future__ import annotations

import difflib
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from ruamel.yaml import YAML

from companion.backups import BACKUP_DIRNAME
from companion.surgical import Edit, contained, read_source, save_entry, write_fields

# A file with everything a hand-maintained config has and a tool tends to eat:
# a header comment, a comment that documents the entry below it, a long
# unwrapped Jinja line, non-default sequence indentation, a folded scalar, a
# literal block scalar, unicode, mixed quote styles and an inline flow mapping.
HOSTILE_LIST = """\
# Hand-maintained automations. Do not let a tool eat this comment.
- id: '1600000000001'
  alias: Jan position clock
  description: ""
  trigger:
  - platform: state
    entity_id: input_number.posclock_jan
  action:
  - service: input_text.set_value
    data:
      value: JansPos:{{ states.input_number.posclock_jan.state }}:{{states.input_number.posclock_speed.state|int}}
    target:
      entity_id: input_text.flur_clock_position
  mode: single

# Second block, deliberately indented UI style with 4-space sequences
- id: '1600000000002'
  alias: Bad Wetter
  trigger:
      - platform: numeric_state
        entity_id: sensor.balkon_illuminance_filtered
        above: 60
  condition:
      - condition: template
        value_template: >-
          {{ as_timestamp(state_attr('automation.kinderzimmer2_rollo_kuhlen', 'last_triggered'))
             | timestamp_custom('%-d') != as_timestamp(now()) | timestamp_custom('%-d') }}
  action:
      - service: notify.mobile_app
        data:
          message: "Rollo wird gekuehlt - bitte Fenster schliessen (Sued/Ost)"
          title: 'Klima'
  mode: restart

- id: '1600000000003'
  alias: Literal block scalar entry
  description: |-
    Alle Phasen mit water=high und fan=high;
    zweiter Absatz bleibt erhalten.
  trigger:
  - platform: time
    at: 07:00:00
  action:
  - service: script.turn_on
    target: {entity_id: script.morning}
  mode: single
# dangling comment at end of file
"""

HOSTILE_MAP = """\
# Hand-maintained scripts.
welcome_home:
  alias: Welcome Home
  sequence:
      - service: mqtt.publish
        data:
          topic: haus/flur
          payload: desired-temp {{states.input_number.gastezimmer_solltemp.state|float}}
          qos: "0"

# Räumt die ganze Wohnung; bitte Reihenfolge nicht ändern.
putzen_komplett:
  alias: Putzen komplett
  description: |-
    Alle Phasen mit water=high und fan=high;
    zweiter Absatz bleibt erhalten.
  sequence:
      - service: vacuum.start
        target: {entity_id: vacuum.roborock}
  mode: single

abendroutine:
  alias: Abendroutine
  sequence:
  - service: light.turn_off
    target:
      entity_id: light.all
"""


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    return y


def _load(text: str) -> Any:
    return _yaml().load(StringIO(text))


def _changed_regions(before: str, after: str) -> list[tuple[str, int, int, int, int]]:
    """Every non-equal opcode between the two texts, line-wise."""
    matcher = difflib.SequenceMatcher(
        None, before.splitlines(keepends=True), after.splitlines(keepends=True), autojunk=False
    )
    return [op for op in matcher.get_opcodes() if op[0] != "equal"]


def _write(tmp_path: Path, text: str, mutate: Any, edit: Edit) -> tuple[str, bool]:
    """Run one single-entry write against ``text``; return (new text, surgical)."""
    path = tmp_path / "target.yaml"
    with open(path, "w", encoding="utf-8", newline="") as stream:
        stream.write(text)
    yaml = _yaml()
    source = read_source(tmp_path, path)
    data = yaml.load(StringIO(source))
    if data is None:
        # What every route does with an empty file before it mutates it.
        data = [] if edit.kind != "replace" and not isinstance(edit.where, str) else {}
    mutate(data)
    surgical = save_entry(tmp_path, path, data, source, edit, yaml)
    return read_source(tmp_path, path), surgical


def _replace_second(data: Any) -> None:
    data[1] = _load("id: '1600000000002'\nalias: Bad Wetter RENAMED\nmode: single\n")


# ---------------------------------------------------------------------------
# the defect, pinned
# ---------------------------------------------------------------------------


def test_whole_file_dump_reformats_entries_nobody_touched() -> None:
    """The behaviour this module replaces: one edit, dozens of entries rewritten.

    Asserted on the *first* entry, which the edit never names. Its long Jinja
    line comes back folded and its neighbours re-indented — semantically
    identical, and a diff no operator asked for. This is the live-fire P1 #4
    reproduction in unit form, and it is here so a future "simplification" back
    to ``yaml.dump(data, f)`` fails loudly rather than passing every value-level
    test in the suite.
    """
    yaml = _yaml()
    data = yaml.load(StringIO(HOSTILE_LIST))
    _replace_second(data)
    buffer = StringIO()
    yaml.dump(data, buffer)

    untouched = "value: JansPos:{{ states.input_number.posclock_jan.state }}:{{states"
    assert untouched in HOSTILE_LIST, "fixture no longer carries the long unwrapped line this test is about"
    assert untouched not in buffer.getvalue(), (
        "a whole-file dump left the first entry's long line alone — the fixture no longer distinguishes "
        "the two writers, so every assertion below would pass vacuously"
    )


# ---------------------------------------------------------------------------
# the invariant: only the named entry's bytes move
# ---------------------------------------------------------------------------


def test_replace_changes_exactly_one_region_and_it_is_the_named_entry(tmp_path: Path) -> None:
    """One contiguous changed region, and it lies inside entry two's own lines."""
    after, surgical = _write(tmp_path, HOSTILE_LIST, _replace_second, Edit("replace", 1))
    assert surgical
    assert "Bad Wetter RENAMED" in after

    lines = HOSTILE_LIST.splitlines(keepends=True)
    first = lines.index("- id: '1600000000002'\n")
    last = lines.index("- id: '1600000000003'\n")

    regions = _changed_regions(HOSTILE_LIST, after)
    assert len(regions) == 1, f"expected one contiguous changed region, got {regions}"
    _tag, start, end, _newstart, _newend = regions[0]
    assert first <= start and end <= last, (
        f"changed lines {start}..{end} reach outside entry two's own lines {first}..{last}"
    )


@pytest.mark.parametrize(
    "fragment",
    [
        "value: JansPos:{{ states.input_number.posclock_jan.state }}:"
        "{{states.input_number.posclock_speed.state|int}}\n",
        "  description: |-\n    Alle Phasen mit water=high und fan=high;\n",
        "    target: {entity_id: script.morning}\n",
        "# Hand-maintained automations. Do not let a tool eat this comment.\n",
        "# dangling comment at end of file\n",
    ],
)
def test_untouched_constructs_survive_byte_for_byte(tmp_path: Path, fragment: str) -> None:
    """Long lines, block scalars, flow mappings and comments elsewhere are not re-emitted.

    Byte comparison on purpose: the original defect was *semantically* lossless,
    so a parse-and-compare check would have called it clean.
    """
    assert fragment in HOSTILE_LIST
    after, surgical = _write(tmp_path, HOSTILE_LIST, _replace_second, Edit("replace", 1))
    assert surgical
    assert fragment in after


def test_the_comment_above_the_next_entry_is_not_carried_off(tmp_path: Path) -> None:
    """Deleting entry two must not take entry three's introduction with it.

    ruamel attaches the comment *between* two entries to the one above, so the
    obvious implementations (span from the node's ``end_mark``, or dump the item
    standalone) either delete this comment or duplicate it.
    """
    after, surgical = _write(tmp_path, HOSTILE_LIST, lambda d: d.pop(1), Edit("delete", 1))
    assert surgical
    assert after.count("# Second block, deliberately indented UI style with 4-space sequences") == 1
    assert "1600000000002" not in after
    assert "- id: '1600000000003'" in after
    assert len(_changed_regions(HOSTILE_LIST, after)) == 1


def test_append_leaves_every_prior_byte_alone(tmp_path: Path) -> None:
    def mutate(data: Any) -> None:
        data.append(_load("id: '1600000000009'\nalias: Brand New\n"))

    after, surgical = _write(tmp_path, HOSTILE_LIST, mutate, Edit("append"))
    assert surgical
    assert after.startswith(HOSTILE_LIST), "an append rewrote bytes that were already in the file"
    assert "Brand New" in after


def test_first_and_last_entries_splice_like_any_other(tmp_path: Path) -> None:
    """The ends of the file are where span arithmetic goes wrong."""
    for index in (0, 2):

        def mutate(data: Any, index: int = index) -> None:
            data[index] = _load(f"id: '160000000000{index + 1}'\nalias: Touched\n")

        after, surgical = _write(tmp_path, HOSTILE_LIST, mutate, Edit("replace", index))
        assert surgical, index
        assert len(_changed_regions(HOSTILE_LIST, after)) == 1, index
    assert "# Hand-maintained automations. Do not let a tool eat this comment." in after
    assert "# dangling comment at end of file" in after


# ---------------------------------------------------------------------------
# the same, for a top-level mapping (scripts.yaml, <helper_domain>.yaml)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("edit", "mutate", "survivor"),
    [
        (
            Edit("replace", "putzen_komplett"),
            lambda d: d.__setitem__("putzen_komplett", _load("alias: Neu\n")),
            "          payload: desired-temp {{states.input_number.gastezimmer_solltemp.state|float}}\n",
        ),
        (
            Edit("delete", "putzen_komplett"),
            lambda d: d.pop("putzen_komplett"),
            '          qos: "0"\n',
        ),
        (
            Edit("append", "neues_skript"),
            lambda d: d.__setitem__("neues_skript", _load("alias: Neu\n")),
            "  description: |-\n",
        ),
    ],
    ids=["replace", "delete", "append"],
)
def test_mapping_writes_touch_only_their_own_key(tmp_path: Path, edit: Edit, mutate: Any, survivor: str) -> None:
    assert survivor in HOSTILE_MAP
    after, surgical = _write(tmp_path, HOSTILE_MAP, mutate, edit)
    assert surgical
    assert survivor in after
    assert len(_changed_regions(HOSTILE_MAP, after)) == 1
    assert "# Räumt die ganze Wohnung; bitte Reihenfolge nicht ändern." in after


# ---------------------------------------------------------------------------
# line endings, indentation, degenerate files
# ---------------------------------------------------------------------------


def test_crlf_file_keeps_crlf_everywhere(tmp_path: Path) -> None:
    """A config written on Windows must not come back with mixed endings.

    ``Path.read_text`` would hide this: universal-newline translation makes the
    source look like LF, and the write then converts every line in the file —
    a whole-file change under a single-entry promise.
    """
    crlf = HOSTILE_LIST.replace("\n", "\r\n")
    after, surgical = _write(tmp_path, crlf, _replace_second, Edit("replace", 1))
    assert surgical
    assert "\n" not in after.replace("\r\n", ""), "spliced block introduced a bare LF into a CRLF file"
    assert len(_changed_regions(crlf, after)) == 1


def test_indented_top_level_sequence_keeps_its_column(tmp_path: Path) -> None:
    indented = "".join("  " + line if line.strip() else line for line in HOSTILE_LIST.splitlines(keepends=True))
    after, surgical = _write(tmp_path, indented, _replace_second, Edit("replace", 1))
    assert surgical
    assert "  - id: '1600000000002'" in after
    assert len(_changed_regions(indented, after)) == 1


@pytest.mark.parametrize("text", ["", "# only a header comment\n"], ids=["empty", "comment-only"])
def test_first_entry_in_an_empty_file_is_still_surgical(tmp_path: Path, text: str) -> None:
    def mutate(data: Any) -> None:
        data.append(_load("id: '1'\nalias: First\n"))

    after, surgical = _write(tmp_path, text, mutate, Edit("append"))
    assert surgical
    assert after.startswith(text)
    assert "alias: First" in after


# ---------------------------------------------------------------------------
# fallback: still correct, and it says so
# ---------------------------------------------------------------------------

FLOW_LIST = "[{id: one, alias: One}, {id: two, alias: Two}]\n"

ANCHORED_LIST = """\
- id: base
  alias: Base
  action: &shared
  - service: light.turn_on
- id: user
  alias: User
  action: *shared
"""


def test_flow_style_top_level_falls_back_and_stays_correct(tmp_path: Path) -> None:
    after, surgical = _write(
        tmp_path, FLOW_LIST, lambda d: d.__setitem__(1, _load("id: two\nalias: Renamed\n")), Edit("replace", 1)
    )
    assert not surgical
    assert _load(after)[1]["alias"] == "Renamed"


def test_anchor_defined_in_the_replaced_entry_falls_back_and_stays_correct(tmp_path: Path) -> None:
    """Replacing the entry that defines ``&shared`` would strand the alias below it.

    Nothing special-cases anchors: the spliced text simply fails to parse back to
    the tree the route meant to write, and the verification step routes the call
    to the whole-file dump. That is the argument for the whole design — a case
    nobody enumerated degrades to the previous behaviour instead of corrupting a
    file.
    """
    after, surgical = _write(
        tmp_path,
        ANCHORED_LIST,
        lambda d: d.__setitem__(0, _load("id: base\nalias: Renamed\naction: []\n")),
        Edit("replace", 0),
    )
    assert not surgical
    reparsed = _load(after)
    assert reparsed[0]["alias"] == "Renamed"
    assert reparsed[1]["action"] == [{"service": "light.turn_on"}]


def test_write_fields_reports_only_the_fallback() -> None:
    assert write_fields(True) == {}
    assert write_fields(False) == {"reformatted": True}


def test_backup_is_written_on_both_paths(tmp_path: Path) -> None:
    """C-5 does not get to depend on which writer ran."""
    for text, edit, mutate in (
        (HOSTILE_LIST, Edit("replace", 1), _replace_second),
        (FLOW_LIST, Edit("replace", 1), lambda d: d.__setitem__(1, _load("id: two\nalias: R\n"))),
    ):
        target = tmp_path / str(abs(hash(text)))
        target.mkdir()
        _write(target, text, mutate, edit)
        backups = list((target / BACKUP_DIRNAME).glob("target.yaml.bak.*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == text


@pytest.mark.parametrize("index", [0, 2], ids=["first", "last"])
def test_deleting_an_entry_at_either_end_keeps_the_file_s_own_comments(tmp_path: Path, index: int) -> None:
    """The ends are where the span arithmetic has no neighbour to bound it.

    Deleting the last entry bounds the span at EOF, so the trailing comment is
    inside it unless the scan back over blank and comment lines is right; the
    first entry has the header comment directly above its own first line.
    """
    after, surgical = _write(tmp_path, HOSTILE_LIST, lambda d: d.pop(index), Edit("delete", index))
    assert surgical
    assert "# Hand-maintained automations. Do not let a tool eat this comment." in after
    assert "# dangling comment at end of file" in after
    assert len(_load(after)) == 2


def test_deleting_the_only_entry_falls_back_rather_than_guessing(tmp_path: Path) -> None:
    """Emptying a file leaves nothing for a top-level list to be parsed from.

    The splice would leave only comments, which reads back as ``None`` rather
    than the empty list the route means to write — the verification sees the
    difference and the whole-file dump produces a file that is honestly empty.
    Recorded because deleting your last automation is a real thing to do.
    """
    single = "# a comment\n- id: only\n  alias: Only\n"
    after, surgical = _write(tmp_path, single, lambda d: d.pop(0), Edit("delete", 0))
    assert not surgical
    assert _load(after) == []


def test_a_write_outside_the_config_base_is_refused(tmp_path: Path) -> None:
    """C-3 at the point of use: the chokepoint does not inherit its precondition.

    The routes reach here through the wiring resolver, which already contains the
    path — but this module is the one place where being wrong is unrecoverable,
    so it checks rather than trusting, and a caller that skips the resolver gets
    the same answer.
    """
    base = tmp_path / "config"
    base.mkdir()
    outside = tmp_path / "elsewhere.yaml"
    outside.write_text("- id: a\n", encoding="utf-8")

    with pytest.raises(web.HTTPBadRequest):
        read_source(base, outside)
    with pytest.raises(web.HTTPBadRequest):
        save_entry(base, outside, [], "", Edit("append"), _yaml())
    with pytest.raises(web.HTTPForbidden):
        contained(base, base / "secrets.yaml")
    assert outside.read_text(encoding="utf-8") == "- id: a\n"
