"""Shared filesystem-containment guards for config file access.

A single source of truth for the two things every config-file path must satisfy:
it has to live inside the configured base directory, and it must not be a denied
file. Kept here so ``routes/config.py`` and ``yaml_resolver.py`` (and anything
else touching config paths) apply identical rules.
"""

from __future__ import annotations

from pathlib import Path

# Files that must never be exposed or written, regardless of location.
DENIED_FILES: frozenset[str] = frozenset({"secrets.yaml"})


def is_within(target: Path, base: Path) -> bool:
    """True if ``target`` is ``base`` or lives inside it, by path semantics.

    Unlike a ``str.startswith`` prefix check, ``Path.is_relative_to`` correctly
    rejects a sibling directory that merely shares the prefix — e.g. base
    ``/config`` does not contain ``/config2/x.yaml`` (reachable via
    ``path=../config2/x.yaml``). Both arguments should already be ``resolve()``d.
    """
    return target.is_relative_to(base)


def is_denied(name: str) -> bool:
    """True if the bare filename ``name`` is on the deny list (case-insensitive)."""
    return name.lower() in DENIED_FILES
