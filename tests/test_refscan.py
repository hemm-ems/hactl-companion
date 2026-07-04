"""Tests for literal entity-reference scanning (refscan)."""

from __future__ import annotations

from pathlib import Path

from companion.refscan import ScanHit, scan_yaml_for_literal


def test_scan_finds_literal_in_top_level_file(tmp_path: Path) -> None:
    (tmp_path / "configuration.yaml").write_text(
        "sensor:\n  platform: template\n  value: sensor.gone\n",
        encoding="utf-8",
    )
    hits = scan_yaml_for_literal(tmp_path, "sensor.gone")
    assert hits == [ScanHit("configuration.yaml", "sensor.value", "sensor.gone")]


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
