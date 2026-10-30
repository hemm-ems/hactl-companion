"""``ref replace`` rewrites only the scalars that hold the token (C-14, #88).

Every assertion here is on **bytes**. The defect this closes is semantically
lossless by construction: ``YamlResolver.save`` produced a file that parsed to
exactly the intended tree while re-indenting and re-folding every other entry.
A parsed comparison would pass against the old writer and the new one alike.
Each byte-level case also carries the anti-vacuity half: the whole-file dump,
run on the same input, must have changed some line that does not hold the
token. Otherwise the fixture could not tell the two writers apart.
"""

from __future__ import annotations

import difflib
from io import StringIO
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from companion.refscan import replace_yaml_literal

OLD = "binary_sensor.door_old"
NEW = "binary_sensor.door_new"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as stream:
        stream.write(text)


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as stream:
        return stream.read()


def _changed_pairs(before: str, after: str) -> list[tuple[str, str]]:
    """``(old line, new line)`` for every line the rewrite touched.

    Lines are compared with their endings, so a CRLF turned into LF counts as a
    change. A rewrite that adds or drops a line has no partner for it, and is
    reported as a pair with an empty side so the caller's check fails on it.
    """
    old = before.splitlines(keepends=True)
    new = after.splitlines(keepends=True)
    pairs: list[tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        left, right = old[i1:i2], new[j1:j2]
        if len(left) != len(right):
            return [("".join(left), "".join(right))]
        pairs.extend(zip(left, right, strict=True))
    return pairs


def assert_only_token_lines_changed(before: str, after: str, old: str = OLD, new: str = NEW) -> int:
    """Every changed line held ``old`` and differs from before only by the swap. Returns the count."""
    pairs = _changed_pairs(before, after)
    assert pairs, "nothing changed — the replace did not write"
    for was, now in pairs:
        assert old in was, f"a line without the token was rewritten:\n  before: {was!r}\n  after:  {now!r}"
        assert was.replace(old, new) == now, f"a token line changed beyond the swap:\n  {was!r}\n  {now!r}"
    return len(pairs)


def whole_file_dump(before: str) -> str:
    """What ``ref replace`` used to write: a round-trip load and dump of the whole file."""
    yaml = YAML()
    yaml.preserve_quotes = True
    buffer = StringIO()
    yaml.dump(yaml.load(StringIO(before)), buffer)
    return buffer.getvalue()


def _rewritten_lines(before: str, after: str) -> list[str]:
    """The ``before`` lines that ``after`` does not keep verbatim."""
    old = before.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, old, after.splitlines(keepends=True), autojunk=False)
    return [line for tag, i1, i2, _j1, _j2 in matcher.get_opcodes() if tag != "equal" for line in old[i1:i2]]


def assert_dump_would_damage_other_lines(before: str, old: str = OLD) -> None:
    damaged = [was for was in _rewritten_lines(before, whole_file_dump(before)) if old not in was]
    assert damaged, "a whole-file dump leaves this fixture alone too, so it cannot tell the writers apart"


# A hand-maintained automations.yaml in the shape the live-fire finding was
# filed against: four-space sequence indentation, a long unwrapped template, a
# block scalar, comments between and inside entries, and the token in two of
# four entries.
HAND_MAINTAINED = f"""\
# Hand-maintained. Formatting below is deliberate.
- id: door_light
  alias: Door light
  trigger:
      - platform: state
        entity_id: {OLD}   # the door contact
        to: "on"
  action:
      - service: light.turn_on
        target:
            entity_id: light.hall
        data:
          message: Door:{{{{ states('{OLD}') }}}}:{{{{states.input_number.posclock_speed.state|int}}}}:tail
  mode: single

# Unrelated, and must come back byte for byte.
- id: night_mode
  alias: Night mode
  description: |-
    Alle Phasen mit water=high und fan=high;
    zweiter Absatz bleibt erhalten.
  trigger:
      - platform: time
        at: "23:00:00"
  action: []

- id: door_notify
  alias: 'Door notify'
  condition:
      - condition: state
        entity_id: '{OLD}'
        state: "on"
  action: []

- id: untouched_last
  alias: Last
  trigger: []
  action: []
# dangling comment at end of file
"""


def test_a_hand_maintained_file_changes_only_the_lines_carrying_the_token(tmp_path: Path) -> None:
    _write(tmp_path / "configuration.yaml", "automation: !include automations.yaml\n")
    autos = tmp_path / "automations.yaml"
    _write(autos, HAND_MAINTAINED)

    changes = replace_yaml_literal(tmp_path, OLD, NEW, dry_run=False)

    assert [c["path"] for c in changes] == [
        "[0].action[0].data.message",
        "[0].trigger[0].entity_id",
        "[2].condition[0].entity_id",
    ]
    assert not any("reformatted" in c for c in changes), changes
    after = _read(autos)
    assert assert_only_token_lines_changed(HAND_MAINTAINED, after) == 3
    assert_dump_would_damage_other_lines(HAND_MAINTAINED)
    # The file the route reports nothing about is left alone entirely.
    assert _read(tmp_path / "configuration.yaml") == "automation: !include automations.yaml\n"


def test_a_token_split_by_an_escaped_line_continuation_falls_back_rather_than_corrupting(tmp_path: Path) -> None:
    """Inside double quotes, a backslash before a line break joins the lines with nothing between.

    So the raw text holds ``binary_sensor.door_o`` and ``ld`` on two lines, and
    only the value holds the token. Substituting in the raw text would find
    nothing, so the in-place rewrite must give up. The file is then
    re-serialized whole, which is correct, and the caller is told.

    (A *plain* or folded scalar can only fold at a space, and no entity id holds
    one, so the escaped continuation is the one way YAML can split a token
    across lines.)
    """
    cfg = tmp_path / "configuration.yaml"
    before = f'sensor:\n    value: "{OLD[:-2]}\\\n      {OLD[-2:]}"\n    other: 1\n'
    _write(cfg, before)

    preview = replace_yaml_literal(tmp_path, OLD, NEW, dry_run=True)
    changes = replace_yaml_literal(tmp_path, OLD, NEW, dry_run=False)

    expected = [
        {"location": "configuration.yaml", "path": "sensor.value", "before": OLD, "after": NEW, "reformatted": True}
    ]
    assert preview == expected, "the dry run must predict the fallback the apply takes"
    assert changes == expected
    yaml = YAML()
    assert yaml.load(StringIO(_read(cfg))) == {"sensor": {"value": NEW, "other": 1}}


def test_several_files_across_the_include_graph_each_change_only_their_token_lines(tmp_path: Path) -> None:
    config = (
        "homeassistant:\n    name: Home\n"
        "automation: !include automations.yaml\n"
        "script: !include_dir_merge_named scripts\n"
    )
    autos = f"- id: a\n  trigger:\n      - platform: state\n        entity_id: {OLD}\n  action: []\n"
    nested = f'door_script:\n    sequence:\n        - service: homeassistant.turn_off\n          entity_id: "{OLD}"\n'
    other = "unrelated_script:\n    sequence:\n        - delay: 5\n"
    _write(tmp_path / "configuration.yaml", config)
    _write(tmp_path / "automations.yaml", autos)
    _write(tmp_path / "scripts" / "nested" / "door.yaml", nested)
    _write(tmp_path / "scripts" / "other.yaml", other)

    changes = replace_yaml_literal(tmp_path, OLD, NEW, dry_run=False)

    assert sorted({c["location"] for c in changes}) == ["automations.yaml", "scripts/nested/door.yaml"]
    assert not any("reformatted" in c for c in changes), changes
    assert assert_only_token_lines_changed(autos, _read(tmp_path / "automations.yaml")) == 1
    assert assert_only_token_lines_changed(nested, _read(tmp_path / "scripts" / "nested" / "door.yaml")) == 1
    assert _read(tmp_path / "configuration.yaml") == config
    assert _read(tmp_path / "scripts" / "other.yaml") == other
    assert_dump_would_damage_other_lines(autos)
    assert_dump_would_damage_other_lines(nested)


def test_quoted_block_literal_and_folded_scalars_keep_their_style(tmp_path: Path) -> None:
    cfg = tmp_path / "configuration.yaml"
    before = (
        "styles:\n"
        f"    single: '{OLD}'\n"
        f'    double: "it\'s {OLD} \\"quoted\\""\n'
        "    literal: |  # a header comment that names " + OLD + " stays as written\n"
        f"        first line {OLD}\n"
        "        second line\n"
        "    folded: >-\n"
        f"        folded {OLD} text\n"
        "        that continues\n"
        "    plain: a plain scalar that\n"
        f"        folds over {OLD} two lines\n"
    )
    _write(cfg, before)

    changes = replace_yaml_literal(tmp_path, OLD, NEW, dry_run=False)

    assert len(changes) == 5
    assert not any("reformatted" in c for c in changes), changes
    after = _read(cfg)
    pairs = _changed_pairs(before, after)
    # The header comment holds the token too, and is NOT one of the lines rewritten.
    assert len(pairs) == 5, pairs
    assert f"literal: |  # a header comment that names {OLD} stays as written\n" in after
    for was, now in pairs:
        assert was.replace(OLD, NEW) == now
    assert_dump_would_damage_other_lines(before)


def test_a_crlf_file_keeps_every_line_ending(tmp_path: Path) -> None:
    cfg = tmp_path / "configuration.yaml"
    before = (
        "﻿# BOM, CRLF and a non-ASCII prefix before the token\r\n"
        "sensor:\r\n"
        f"    value: 'é😀 {OLD}'\r\n"
        "    long: |\r\n"
        f"        {OLD}\r\n"
        "        tail\r\n"
        "    other: 1\r\n"
    )
    _write(cfg, before)

    changes = replace_yaml_literal(tmp_path, OLD, NEW, dry_run=False)

    assert len(changes) == 2
    assert not any("reformatted" in c for c in changes), changes
    after_bytes = cfg.read_bytes()
    assert after_bytes == before.replace(OLD, NEW).encode("utf-8")
    assert after_bytes.count(b"\r\n") == before.count("\r\n")


def test_an_aliased_scalar_is_rewritten_once_at_its_anchor(tmp_path: Path) -> None:
    cfg = tmp_path / "configuration.yaml"
    before = f"base:\n    entity_id: &door {OLD}\nuses:\n    entity_id: *door\n"
    _write(cfg, before)

    changes = replace_yaml_literal(tmp_path, OLD, NEW, dry_run=False)

    assert [c["path"] for c in changes] == ["base.entity_id", "uses.entity_id"]
    assert not any("reformatted" in c for c in changes), changes
    assert _read(cfg) == before.replace(OLD, NEW)


def test_a_key_reached_through_a_merge_is_rewritten_where_it_is_written(tmp_path: Path) -> None:
    """The old writer materialised the merged key as a local copy in every mapping that merged it."""
    cfg = tmp_path / "configuration.yaml"
    before = f"base: &base\n    entity_id: {OLD}\nchild:\n    <<: *base\n    name: Child\n"
    _write(cfg, before)

    changes = replace_yaml_literal(tmp_path, OLD, NEW, dry_run=False)

    assert [c["path"] for c in changes] == ["base.entity_id", "child.entity_id"]
    assert not any("reformatted" in c for c in changes), changes
    assert _read(cfg) == before.replace(OLD, NEW)


def test_a_replacement_the_scalar_quoting_cannot_hold_falls_back_to_correct_content(tmp_path: Path) -> None:
    """Splicing ``o'neil`` into a single-quoted scalar would end the quotes early.

    Nothing special-cases that. The spliced text fails to parse back to the
    intended tree, and the file takes the whole-file path, which quotes the
    value correctly. The route accepts any string as ``new``, so this input can
    reach it.
    """
    cfg = tmp_path / "configuration.yaml"
    _write(cfg, f"sensor:\n    value: '{OLD}'\n")

    changes = replace_yaml_literal(tmp_path, OLD, "sensor.o'neil", dry_run=False)

    assert changes[0]["reformatted"] is True
    assert YAML().load(StringIO(_read(cfg))) == {"sensor": {"value": "sensor.o'neil"}}


def test_a_dry_run_writes_nothing_and_reports_the_splice(tmp_path: Path) -> None:
    cfg = tmp_path / "configuration.yaml"
    _write(cfg, f"sensor:\n    value: {OLD}\n")

    changes = replace_yaml_literal(tmp_path, OLD, NEW, dry_run=True)

    assert changes == [{"location": "configuration.yaml", "path": "sensor.value", "before": OLD, "after": NEW}]
    assert _read(cfg) == f"sensor:\n    value: {OLD}\n"
    assert not (tmp_path / ".hactl_backups").exists()


# Replacements that, spliced into a *plain* scalar, resolve to an implicit or
# explicit tag whose constructor raises rather than raising a YAMLError: a
# date-shaped id that is no calendar date, and explicit tags with a body their
# constructor cannot take. hactl's ref-target check lets a date-shaped id
# through, and the raw API takes any string.
UNCONSTRUCTIBLE = ["2020-13-45", "2020-02-30", "!!int abc", "!!float abc", "!!timestamp nope", "!!set"]


@pytest.mark.parametrize("replacement", UNCONSTRUCTIBLE)
@pytest.mark.parametrize("dry_run", [True, False])
def test_a_replacement_that_cannot_be_constructed_falls_back_instead_of_raising(
    tmp_path: Path, replacement: str, dry_run: bool
) -> None:
    """A re-parse that raises anything counts as "does not parse back".

    The old whole-file writer never constructed the spliced text, so it could
    not fail this way. The verification step must not add a failure the writer
    it replaces did not have.
    """
    cfg = tmp_path / "configuration.yaml"
    before = f"sensor:\n    value: {OLD}\n"
    _write(cfg, before)

    changes = replace_yaml_literal(tmp_path, OLD, replacement, dry_run=dry_run)

    assert [(c["path"], c["after"], c.get("reformatted")) for c in changes] == [("sensor.value", replacement, True)]
    if dry_run:
        assert _read(cfg) == before
    else:
        assert YAML().load(StringIO(_read(cfg))) == {"sensor": {"value": replacement}}


def test_a_multi_file_replace_with_an_unconstructible_replacement_rewrites_every_file(tmp_path: Path) -> None:
    """The file that splices cleanly and the one that cannot both end up rewritten.

    configuration.yaml holds the token single-quoted, where ``2020-13-45`` is
    still a string, so it splices. automations.yaml holds it plain, where the
    same text is a malformed timestamp. A crash on the second file used to
    leave the first already written: a half-applied rename.
    """
    cfg = f"automation: !include automations.yaml\nsensor:\n    value: '{OLD}'\n"
    autos = f"- id: a\n  trigger:\n      - platform: state\n        entity_id: {OLD}\n  action: []\n"
    _write(tmp_path / "configuration.yaml", cfg)
    _write(tmp_path / "automations.yaml", autos)

    changes = replace_yaml_literal(tmp_path, OLD, "2020-13-45", dry_run=False)

    assert {(c["location"], c.get("reformatted")) for c in changes} == {
        ("configuration.yaml", None),
        ("automations.yaml", True),
    }
    assert _read(tmp_path / "configuration.yaml") == cfg.replace(OLD, "2020-13-45")
    assert YAML().load(StringIO(_read(tmp_path / "automations.yaml")))[0]["trigger"][0]["entity_id"] == "2020-13-45"
