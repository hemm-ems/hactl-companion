"""Tests for timestamped backup retention (companion.backups)."""

from __future__ import annotations

from pathlib import Path

from companion.backups import backup_dir, make_backup


def test_make_backup_none_for_missing_file(tmp_path: Path) -> None:
    assert make_backup(tmp_path / "nope.yaml", base=tmp_path) is None


def test_make_backup_creates_named_copy(tmp_path: Path) -> None:
    f = tmp_path / "x.yaml"
    f.write_text("a: 1\n", encoding="utf-8")
    name = make_backup(f, base=tmp_path)
    assert name is not None and name.startswith("x.yaml.bak.")
    # The copy lands in the hidden subfolder, not next to the file.
    assert not list(tmp_path.glob("x.yaml.bak.*"))
    assert (backup_dir(f) / name).read_text(encoding="utf-8") == "a: 1\n"


def test_make_backup_prunes_to_keep_newest(tmp_path: Path) -> None:
    f = tmp_path / "x.yaml"
    f.write_text("a: 1\n", encoding="utf-8")
    # Seed older backups with distinct, chronologically-ordered timestamps.
    bdir = backup_dir(f)
    bdir.mkdir(parents=True, exist_ok=True)
    for ts in ("20200101T000001", "20200101T000002", "20200101T000003", "20200101T000004", "20200101T000005"):
        (bdir / f"x.yaml.bak.{ts}").write_text("old\n", encoding="utf-8")

    make_backup(f, base=tmp_path, keep=3)  # + the brand-new (2026-dated) backup, then prune to 3

    backups = sorted(bdir.glob("x.yaml.bak.*"))
    assert len(backups) == 3
    names = {b.name for b in backups}
    # Oldest three pruned; the two newest seeds plus the new backup survive.
    assert "x.yaml.bak.20200101T000001" not in names
    assert "x.yaml.bak.20200101T000005" in names


def test_prune_is_scoped_to_the_file_even_when_its_name_is_a_glob(tmp_path: Path) -> None:
    """A file literally named `*.yaml` must not prune other files' backups (CodeQL #5).

    The prune pattern was built from the bare file name, so `*.yaml.bak.*`
    matched every root file's backups and retention cut them all to the newest
    few — an authenticated `PUT ?path=*.yaml` deleted everyone else's C-5 history.
    """
    other = tmp_path / "automations.yaml"
    other.write_text("a: 1\n", encoding="utf-8")
    bdir = backup_dir(other)
    bdir.mkdir(parents=True, exist_ok=True)
    for i in range(1, 4):
        (bdir / f"automations.yaml.bak.2020010{i}T000000").write_text("old\n", encoding="utf-8")

    star = tmp_path / "*.yaml"
    star.write_text("b: 2\n", encoding="utf-8")
    make_backup(star, base=tmp_path, keep=1)

    assert len(list(bdir.glob("automations.yaml.bak.*"))) == 3


def test_make_backup_refuses_a_path_outside_base(tmp_path: Path) -> None:
    """The guard sits where the copy and the prune touch the disk, not only in the callers."""
    import pytest

    base = tmp_path / "config"
    base.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("a: 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Path traversal"):
        make_backup(outside, base=base)
    assert not backup_dir(outside).exists()
