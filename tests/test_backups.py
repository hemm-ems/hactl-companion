"""Tests for timestamped backup retention (companion.backups)."""

from __future__ import annotations

from pathlib import Path

from companion.backups import make_backup


def test_make_backup_none_for_missing_file(tmp_path: Path) -> None:
    assert make_backup(tmp_path / "nope.yaml") is None


def test_make_backup_creates_named_copy(tmp_path: Path) -> None:
    f = tmp_path / "x.yaml"
    f.write_text("a: 1\n", encoding="utf-8")
    name = make_backup(f)
    assert name is not None and name.startswith("x.yaml.bak.")
    assert (tmp_path / name).read_text(encoding="utf-8") == "a: 1\n"


def test_make_backup_prunes_to_keep_newest(tmp_path: Path) -> None:
    f = tmp_path / "x.yaml"
    f.write_text("a: 1\n", encoding="utf-8")
    # Seed older backups with distinct, chronologically-ordered timestamps.
    for ts in ("20200101T000001", "20200101T000002", "20200101T000003", "20200101T000004", "20200101T000005"):
        (tmp_path / f"x.yaml.bak.{ts}").write_text("old\n", encoding="utf-8")

    make_backup(f, keep=3)  # + the brand-new (2026-dated) backup, then prune to 3

    backups = sorted(tmp_path.glob("x.yaml.bak.*"))
    assert len(backups) == 3
    names = {b.name for b in backups}
    # Oldest three pruned; the two newest seeds plus the new backup survive.
    assert "x.yaml.bak.20200101T000001" not in names
    assert "x.yaml.bak.20200101T000005" in names
