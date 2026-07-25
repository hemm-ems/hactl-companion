"""Tests for literal entity-reference scanning (refscan)."""

from __future__ import annotations

from pathlib import Path

import pytest

from companion.refscan import (
    EntityRef,
    ScanHit,
    replace_yaml_literal,
    scan_yaml_for_entities,
    scan_yaml_for_literal,
)
from companion.yaml_resolver import UnknownIncludeTagError


def test_scan_finds_literal_in_top_level_file(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text(
        "sensor:\n  platform: template\n  value: sensor.gone\n",
        encoding="utf-8",
    )
    hits = scan_yaml_for_literal(tmp_path, "sensor.gone")
    assert hits == [ScanHit("configuration.yaml", "sensor.value", "sensor.gone")]


def _change(location: str, path: str, before: str, after: str) -> dict[str, str]:
    return {"location": location, "path": path, "before": before, "after": after}


def test_replace_dry_run_reports_but_writes_nothing(tmp_path: Path) -> None:
    cfg = tmp_path / "configuration.yaml"
    original = "sensor:\n  value: sensor.gone\n"
    cfg.write_text(original, encoding="utf-8")

    changes = replace_yaml_literal(tmp_path, "sensor.gone", "sensor.new", dry_run=True)

    assert changes == [_change("configuration.yaml", "sensor.value", "sensor.gone", "sensor.new")]
    # Dry-run must not touch the file at all.
    assert cfg.read_text(encoding="utf-8") == original


def test_replace_rewrites_only_the_owning_file(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text(
        "automation: !include automations.yaml\nsensor:\n  value: sensor.keep\n",
        encoding="utf-8",
    )
    autos = tmp_path / "automations.yaml"
    autos.write_text(
        "- alias: A\n  trigger:\n    - platform: state\n      entity_id: binary_sensor.gone\n",
        encoding="utf-8",
    )

    changes = replace_yaml_literal(tmp_path, "binary_sensor.gone", "binary_sensor.new", dry_run=False)

    assert changes == [
        _change("automations.yaml", "[0].trigger[0].entity_id", "binary_sensor.gone", "binary_sensor.new")
    ]
    autos_text = autos.read_text(encoding="utf-8")
    assert "binary_sensor.new" in autos_text
    assert "binary_sensor.gone" not in autos_text
    # The literal lives in automations.yaml, not configuration.yaml — leave the latter alone.
    assert "sensor.keep" in (tmp_path / "configuration.yaml").read_text(encoding="utf-8")


def test_replace_preserves_adjacent_comments_and_quote_style(tmp_path: Path) -> None:
    cfg = tmp_path / "configuration.yaml"
    cfg.write_text(
        'sensor:\n  value: "sensor.gone"  # keep me\n  other: 1  # and me\n',
        encoding="utf-8",
    )

    replace_yaml_literal(tmp_path, "sensor.gone", "sensor.new", dry_run=False)

    text = cfg.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert "# and me" in text
    # Round-trip keeps the original double-quote style on the rewritten scalar.
    assert '"sensor.new"' in text


def test_replace_never_touches_secrets(tmp_path: Path) -> None:
    # !include secrets.yaml forces it into the include graph; the resolver must still refuse it.
    (tmp_path / "configuration.yaml").write_text(
        "leak: !include secrets.yaml\nsensor:\n  value: sensor.gone\n",
        encoding="utf-8",
    )
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("token: sensor.gone\n", encoding="utf-8")

    changes = replace_yaml_literal(tmp_path, "sensor.gone", "sensor.new", dry_run=False)

    assert changes == [_change("configuration.yaml", "sensor.value", "sensor.gone", "sensor.new")]
    # secrets.yaml is never scanned nor written, even though it contains the literal.
    assert secrets.read_text(encoding="utf-8") == "token: sensor.gone\n"


def test_replace_rewrites_token_embedded_in_jinja_template(tmp_path: Path) -> None:
    cfg = tmp_path / "configuration.yaml"
    cfg.write_text(
        "sensor:\n  - platform: template\n    sensors:\n      liters:\n"
        "        value_template: \"{{ states('sensor.zisterne_liter') }} plus 1\"\n",
        encoding="utf-8",
    )

    changes = replace_yaml_literal(tmp_path, "sensor.zisterne_liter", "sensor.zisterne_neu", dry_run=False)

    assert changes == [
        _change(
            "configuration.yaml",
            "sensor[0].sensors.liters.value_template",
            "sensor.zisterne_liter",
            "sensor.zisterne_neu",
        )
    ]
    text = cfg.read_text(encoding="utf-8")
    # Only the entity id token is swapped — the rest of the template survives.
    assert "{{ states('sensor.zisterne_neu') }} plus 1" in text
    assert "sensor.zisterne_liter" not in text


def test_replace_boundary_leaves_longer_or_glued_token_untouched(tmp_path: Path) -> None:
    cfg = tmp_path / "configuration.yaml"
    original = "a: sensor.foo_bar\nb: asensor.foo\n"
    cfg.write_text(original, encoding="utf-8")

    changes = replace_yaml_literal(tmp_path, "sensor.foo", "sensor.new", dry_run=False)

    assert changes == []
    # Neither the longer entity nor the glued-on text is touched.
    assert cfg.read_text(encoding="utf-8") == original


def test_replace_no_match_writes_nothing(tmp_path: Path) -> None:
    cfg = tmp_path / "configuration.yaml"
    original = "a: b\n"
    cfg.write_text(original, encoding="utf-8")

    changes = replace_yaml_literal(tmp_path, "sensor.absent", "sensor.new", dry_run=False)

    assert changes == []
    assert cfg.read_text(encoding="utf-8") == original


def test_scan_follows_include_and_reports_owning_file(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text("automation: !include automations.yaml\n", encoding="utf-8")
    (tmp_path / "automations.yaml").write_text(
        "- alias: A\n  trigger:\n    - platform: state\n      entity_id: binary_sensor.gone\n",
        encoding="utf-8",
    )
    hits = scan_yaml_for_literal(tmp_path, "binary_sensor.gone")
    # Reported against the file it actually lives in, with a path local to that file.
    assert hits == [ScanHit("automations.yaml", "[0].trigger[0].entity_id", "binary_sensor.gone")]


def test_scan_no_match_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text("a: b\n", encoding="utf-8")
    assert scan_yaml_for_literal(tmp_path, "sensor.absent") == []


def test_scan_matches_target_embedded_in_larger_string(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text('note: "prefix sensor.gone suffix"\n', encoding="utf-8")
    # Boundary-aware, not exact-value: a whole-token mention inside a larger
    # string (delimited by non-word chars, here spaces) is still a hit.
    assert scan_yaml_for_literal(tmp_path, "sensor.gone") == [ScanHit("configuration.yaml", "note", "sensor.gone")]


def test_scan_finds_target_embedded_in_jinja_template(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text(
        "sensor:\n  - platform: template\n    sensors:\n      liters:\n"
        "        value_template: \"{{ states('sensor.zisterne_liter') }}\"\n",
        encoding="utf-8",
    )
    hits = scan_yaml_for_literal(tmp_path, "sensor.zisterne_liter")
    assert hits == [ScanHit("configuration.yaml", "sensor[0].sensors.liters.value_template", "sensor.zisterne_liter")]


def test_scan_finds_target_split_by_an_escaped_line_continuation(tmp_path: Path) -> None:
    """A token the raw bytes do not contain, but the parsed value does.

    Inside a double-quoted scalar a trailing backslash joins the two lines with
    no separator, so `sensor.` + `gone` parse as `sensor.gone` even though the
    file text never holds that substring. The scan's raw-text pre-filter (which
    exists to avoid parsing files that cannot match) must not be fooled into
    skipping this file.
    """
    (tmp_path / "configuration.yaml").write_text(
        'automation: !include automations.yaml\nnote: "harmless"\n', encoding="utf-8"
    )
    (tmp_path / "automations.yaml").write_text(
        "- alias: A\n  condition:\n    - condition: template\n"
        "      value_template: \"{{ states('sensor.\\\n        gone') }}\"\n",
        encoding="utf-8",
    )
    assert "sensor.gone" not in (tmp_path / "automations.yaml").read_text(encoding="utf-8")

    hits = scan_yaml_for_literal(tmp_path, "sensor.gone")

    assert hits == [ScanHit("automations.yaml", "[0].condition[0].value_template", "sensor.gone")]


def test_scan_boundary_rejects_longer_or_glued_token(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text(
        "a: sensor.foo_bar\nb: asensor.foo\n",
        encoding="utf-8",
    )
    # "sensor.foo" is a prefix of "sensor.foo_bar" (same domain, longer object
    # id) and a suffix of "asensor.foo" (glued onto other text) — neither is a
    # whole-token match for target "sensor.foo".
    assert scan_yaml_for_literal(tmp_path, "sensor.foo") == []


def test_scan_multiple_hits_sorted_by_path(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text(
        "two:\n  y: sensor.gone\none:\n  x: sensor.gone\n",
        encoding="utf-8",
    )
    hits = scan_yaml_for_literal(tmp_path, "sensor.gone")
    assert [h.path for h in hits] == ["one.x", "two.y"]
    assert all(h.location == "configuration.yaml" for h in hits)


def test_scan_missing_include_is_skipped_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text(
        "automation: !include missing.yaml\nsensor:\n  value: sensor.gone\n",
        encoding="utf-8",
    )
    hits = scan_yaml_for_literal(tmp_path, "sensor.gone")
    assert hits == [ScanHit("configuration.yaml", "sensor.value", "sensor.gone")]


def test_entities_collects_every_entity_shaped_leaf_across_files(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text(
        "automation: !include automations.yaml\nsensor:\n  value: binary_sensor.multi_word\n",
        encoding="utf-8",
    )
    (tmp_path / "automations.yaml").write_text(
        "- trigger:\n    - platform: state\n      entity_id: light.kitchen\n",
        encoding="utf-8",
    )
    refs = scan_yaml_for_entities(tmp_path)
    assert refs == [
        EntityRef("automations.yaml", "[0].trigger[0].entity_id", "entity_id", "light.kitchen"),
        EntityRef("configuration.yaml", "sensor.value", "value", "binary_sensor.multi_word"),
    ]


def test_entities_ignores_non_entity_shaped_scalars(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text(
        # "a"/"d" have no entity-shaped token at all; "b" has none either since
        # the domain class is lowercase-only, so "System.Ready" doesn't qualify.
        # "c" embeds a real entity-shaped token in a longer string — de-anchored
        # matching now finds it too, alongside the bare leaf "e".
        'a: hello\nb: "System.Ready"\nc: "prefix light.kitchen suffix"\nd: 42\ne: light.kitchen\n',
        encoding="utf-8",
    )
    refs = scan_yaml_for_entities(tmp_path)
    assert refs == [
        EntityRef("configuration.yaml", "c", "c", "light.kitchen"),
        EntityRef("configuration.yaml", "e", "e", "light.kitchen"),
    ]


def test_entities_finds_token_embedded_in_jinja_template(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text(
        "sensor:\n  - platform: template\n    sensors:\n      liters:\n"
        "        value_template: \"{{ states('sensor.zisterne_liter') }}\"\n",
        encoding="utf-8",
    )
    refs = scan_yaml_for_entities(tmp_path)
    assert refs == [
        EntityRef(
            "configuration.yaml",
            "sensor[0].sensors.liters.value_template",
            "value_template",
            "sensor.zisterne_liter",
        )
    ]


def test_entities_finds_multiple_tokens_embedded_in_one_leaf(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text(
        "value_template: \"{{ states('sensor.a') }} vs {{ states('binary_sensor.b') }}\"\n",
        encoding="utf-8",
    )
    refs = scan_yaml_for_entities(tmp_path)
    # Both embedded tokens are reported, sharing the one leaf's path.
    assert [(r.path, r.matched_value) for r in refs] == [
        ("value_template", "sensor.a"),
        ("value_template", "binary_sensor.b"),
    ]


def test_entities_boundary_rejects_truncated_token(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text(
        "value_template: \"{{ states('sensor.foo_bar') }} and asensor.foo\"\n",
        encoding="utf-8",
    )
    refs = scan_yaml_for_entities(tmp_path)
    values = [r.matched_value for r in refs]
    # Each is its own whole token — "sensor.foo_bar" (longer object id) and
    # "asensor.foo" (glued onto other text, but "asensor" is itself a valid
    # domain-shaped run) — neither yields a truncated "sensor.foo" match.
    assert "sensor.foo" not in values
    assert values == ["sensor.foo_bar", "asensor.foo"]


def test_entities_carry_key_to_distinguish_service_from_entity(tmp_path: Path) -> None:
    # The primitive is shape-only: a service name matches the same domain.object
    # shape and IS returned — but its key ("service") lets a caller exclude it,
    # while the real entity position carries key "entity_id".
    (tmp_path / "configuration.yaml").write_text(
        "- service: light.turn_on\n  target:\n    entity_id: light.kitchen\n",
        encoding="utf-8",
    )
    refs = scan_yaml_for_entities(tmp_path)
    assert EntityRef("configuration.yaml", "[0].service", "service", "light.turn_on") in refs
    assert EntityRef("configuration.yaml", "[0].target.entity_id", "entity_id", "light.kitchen") in refs


def test_entities_list_item_key_is_the_enclosing_mapping_key(tmp_path: Path) -> None:
    # A bare entity in an entity_id list: the key is the list's key, not an index.
    (tmp_path / "configuration.yaml").write_text(
        "- entity_id:\n    - light.one\n    - light.two\n",
        encoding="utf-8",
    )
    refs = scan_yaml_for_entities(tmp_path)
    assert [(r.path, r.key, r.matched_value) for r in refs] == [
        ("[0].entity_id[0]", "entity_id", "light.one"),
        ("[0].entity_id[1]", "entity_id", "light.two"),
    ]


def test_entities_no_matches_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text("a:\n  b: c\n", encoding="utf-8")
    assert scan_yaml_for_entities(tmp_path) == []


def test_scan_skips_out_of_base_include(tmp_path: Path) -> None:
    """An `!include ../outside.yaml` must be skipped, never retargeted in-base.

    Regression for the `_rel_to` fallback that returned `path.name`, which would
    re-resolve a same-named file *inside* base and scan the wrong file.
    """
    base = tmp_path / "config"
    base.mkdir()
    # A same-named file INSIDE base that is never legitimately included.
    (base / "outside.yaml").write_text("y: sensor.gone\n", encoding="utf-8")
    # A file OUTSIDE base holding the literal — must never be reached.
    (tmp_path / "outside.yaml").write_text("x: sensor.gone\n", encoding="utf-8")
    (base / "configuration.yaml").write_text("sensor: !include ../outside.yaml\n", encoding="utf-8")

    hits = scan_yaml_for_literal(base, "sensor.gone")

    locations = {h.location for h in hits}
    assert "outside.yaml" not in locations, "escaping include was retargeted to an in-base file"
    assert hits == []


def test_scan_refuses_unknown_include_tag(tmp_path: Path) -> None:
    """C-11: an unimplemented include tag stops the scan instead of pruning the graph.

    `include_tag` returning None here would drop every file the tag names out of
    the walked config, and `ref scan`'s hit list, `ref validate`'s dangling-
    reference verdict and `ref replace`'s rewrite set would all answer
    confidently about a config they had only partly read. `ref replace` is the
    sharp end: a pruned file keeps the old entity id while the response says the
    replacement succeeded.
    """
    split = tmp_path / "autos"
    split.mkdir()
    (split / "a.yaml").write_text("- id: one\n  action:\n    - entity_id: sensor.gone\n", encoding="utf-8")
    (tmp_path / "configuration.yaml").write_text("automation: !include_dir_merge_flat autos/\n", encoding="utf-8")

    with pytest.raises(UnknownIncludeTagError) as excinfo:
        scan_yaml_for_literal(tmp_path, "sensor.gone")
    assert "!include_dir_merge_flat" in str(excinfo.value)


def test_scan_still_follows_every_known_include_dir_tag(tmp_path: Path) -> None:
    """Control: the refusal above must not have cost us the tags we do implement."""
    for tag in ("!include_dir_list", "!include_dir_merge_list", "!include_dir_named", "!include_dir_merge_named"):
        base = tmp_path / tag.strip("!")
        split = base / "autos"
        split.mkdir(parents=True)
        (split / "a.yaml").write_text("- id: one\n  action:\n    - entity_id: sensor.gone\n", encoding="utf-8")
        (base / "configuration.yaml").write_text(f"automation: {tag} autos/\n", encoding="utf-8")

        hits = scan_yaml_for_literal(base, "sensor.gone")
        assert [h.location for h in hits] == ["autos/a.yaml"], f"{tag} did not reach the file it names: {hits}"
