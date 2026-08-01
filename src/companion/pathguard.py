"""Shared filesystem-containment guards for config file access.

A single source of truth for the two things every config-file path must satisfy:
it has to live inside the configured base directory, and it must not be a denied
file or fall under a denied directory. Kept here so ``routes/config.py``,
``wiring.py``, ``surgical.py`` and ``yaml_resolver.py`` (and anything else
touching config paths) apply identical rules.
"""

from __future__ import annotations

from pathlib import Path

# Files that must never be exposed or written, regardless of location.
DENIED_FILES: frozenset[str] = frozenset({"secrets.yaml"})

# Directories that must never be exposed or written, regardless of depth —
# denied by directory membership, not by enumerating the files inside them.
#
# ``.storage`` is Home Assistant's own state, not config a human authors:
# ``core.config_entries`` holds cleartext credentials for every integration
# (verified live 2026-08-01 — 213 entries, 38 integrations, including an MQTT
# broker password, an OIDC client secret and NAS credentials), ``auth`` and
# ``auth_provider.homeassistant`` are the login/session store, and
# ``core.restore_state`` is a full state snapshot. A filename-only denylist
# (the ``DENIED_FILES`` shape) protects ``secrets.yaml`` — the file holding
# *references* to secrets — while leaving every file that holds the secrets
# themselves reachable; extending that denylist one credential file at a time
# is the same mistake that let this happen, so the whole directory is denied
# instead.
#
# Checked before this rule existed: neither this service's own code
# (``routes/related.py``, ``routes/helpers.py``) nor hactl's Go client
# (``internal/companion/client.go`` — the only caller of the file/files/block
# routes) ever requests a ``.storage`` path through the guarded routes; the two
# in-service readers reach ``.storage`` directly on disk with a fixed,
# internally-enumerated key, never a caller-supplied path, so they are outside
# this guard's scope and unaffected by denying the directory here.
DENIED_DIRS: frozenset[str] = frozenset({".storage"})


def is_within(target: Path, base: Path) -> bool:
    """True if ``target`` is ``base`` or lives inside it, by path semantics.

    Unlike a ``str.startswith`` prefix check, ``Path.is_relative_to`` correctly
    rejects a sibling directory that merely shares the prefix — e.g. base
    ``/config`` does not contain ``/config2/x.yaml`` (reachable via
    ``path=../config2/x.yaml``). Both arguments should already be ``resolve()``d.
    """
    return target.is_relative_to(base)


def is_denied(name: str) -> bool:
    """True if the bare filename ``name`` is on the file deny list (case-insensitive).

    Filename-only on purpose: this is also used to filter file*names* while
    iterating an already-contained directory (``yaml_resolver``'s
    ``!include_dir_*`` handlers, the config-file listing), where there is no
    caller-supplied path to check for a denied *directory* — the directory
    membership has already been decided by the time these run. A path a caller
    supplies directly should go through :func:`is_denied_path` instead, which
    also enforces ``DENIED_DIRS``.
    """
    return name.lower() in DENIED_FILES


def is_denied_path(target: Path, base: Path) -> bool:
    """True if ``target`` (already resolved, already proven inside ``base``) must
    never be exposed or written.

    Two rules combine: the bare filename is on :data:`DENIED_FILES`
    (``secrets.yaml``, wherever it lives), or any path component between
    ``base`` and ``target`` — including ``target`` itself — names a directory in
    :data:`DENIED_DIRS`. This is the check every route that takes a
    caller-supplied path must apply; :func:`is_denied` alone is not enough,
    because a bare-filename check can never refuse a *directory*.
    """
    if is_denied(target.name):
        return True
    try:
        parts = target.relative_to(base).parts
    except ValueError:
        parts = target.parts
    return any(part.lower() in DENIED_DIRS for part in parts)
