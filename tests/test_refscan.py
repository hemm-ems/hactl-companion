"""Tests for literal entity-reference scanning (refscan)."""

from __future__ import annotations

from pathlib import Path

from companion.refscan import ScanHit, replace_yaml_literal, scan_yaml_for_literal


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


def test_scan_does_not_match_embedded_substring(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text('note: "prefix sensor.gone suffix"\n', encoding="utf-8")
    # Literal scan is exact-value, not substring — an embedded mention is not a hit.
    assert scan_yaml_for_literal(tmp_path, "sensor.gone") == []


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
