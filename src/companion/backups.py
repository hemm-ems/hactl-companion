"""Timestamped file backups with bounded retention.

Every applied write drops a ``<name>.bak.<ts>`` next to the file. Without a cap
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


def make_backup(path: str | Path, *, keep: int = MAX_BACKUPS) -> str | None:
    """Back up ``path`` to ``<name>.bak.<ts>`` and prune old backups.

    Returns the backup filename, or ``None`` if ``path`` does not exist yet
    (a brand-new file has nothing to back up).
    """
    path = Path(path)
    if not path.is_file():
        return None
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    backup_name = f"{path.name}.bak.{timestamp}"
    shutil.copy2(path, path.parent / backup_name)
    _prune_backups(path, keep=keep)
    return backup_name


def _prune_backups(path: Path, *, keep: int) -> None:
    """Delete all but the newest ``keep`` backups of ``path`` (no-op if keep <= 0 keeps all)."""
    if keep <= 0:
        return
    # Timestamp format sorts lexicographically == chronologically; oldest first.
    backups = sorted(path.parent.glob(f"{path.name}.bak.*"))
    for stale in backups[:-keep]:
        with contextlib.suppress(OSError):
            stale.unlink()
