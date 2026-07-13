"""Timestamped file backups with bounded retention.

Every applied write drops a ``<name>.bak.<ts>`` into a hidden ``.hactl_backups``
subfolder next to the file, rather than beside the file itself — a frequently
edited config root would otherwise fill up with ``.bak`` clutter. Without a cap
these accumulate forever; :func:`make_backup` prunes to the newest ``MAX_BACKUPS``
per file so a frequently-written config can't fill the volume.
"""

from __future__ import annotations

import contextlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

# How many timestamped backups to retain per file.
MAX_BACKUPS = 10

# Hidden subfolder (next to the backed-up file) that holds its timestamped
# copies. The leading dot keeps HA's config loader and the config tree walker
# in ``routes/config.py`` from descending into it, and keeps the backups out of
# an operator's ``ls`` of the config root.
BACKUP_DIRNAME = ".hactl_backups"


def backup_dir(path: str | Path) -> Path:
    """Directory that holds ``path``'s timestamped backups."""
    return Path(path).parent / BACKUP_DIRNAME


def make_backup(path: str | Path, *, keep: int = MAX_BACKUPS) -> str | None:
    """Back up ``path`` into ``.hactl_backups/<name>.bak.<ts>`` and prune old backups.

    Returns the backup filename (not its path), or ``None`` if ``path`` does not
    exist yet (a brand-new file has nothing to back up). Reconstruct the full
    path with :func:`backup_dir` when you need it (e.g. for rollback).
    """
    path = Path(path)
    if not path.is_file():
        return None
    dest_dir = backup_dir(path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    backup_name = f"{path.name}.bak.{timestamp}"
    shutil.copy2(path, dest_dir / backup_name)
    _prune_backups(path, keep=keep)
    return backup_name


def _prune_backups(path: Path, *, keep: int) -> None:
    """Delete all but the newest ``keep`` backups of ``path`` (no-op if keep <= 0 keeps all)."""
    if keep <= 0:
        return
    # Timestamp format sorts lexicographically == chronologically; oldest first.
    backups = sorted(backup_dir(path).glob(f"{path.name}.bak.*"))
    for stale in backups[:-keep]:
        with contextlib.suppress(OSError):
            stale.unlink()
