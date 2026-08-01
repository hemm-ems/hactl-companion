"""Tests for the shared path-containment guard (companion.pathguard)."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web

from companion.pathguard import is_denied, is_denied_path, is_within
from companion.routes.config import _resolve_config_path


def test_is_within_accepts_child(tmp_path: Path) -> None:
    base = tmp_path / "config"
    base.mkdir()
    assert is_within(base / "automations.yaml", base)
    assert is_within(base, base)


def test_is_within_rejects_sibling_sharing_prefix(tmp_path: Path) -> None:
    """The classic bug: /config2 shares the string prefix of /config but is NOT inside it."""
    base = tmp_path / "config"
    base.mkdir()
    sibling = tmp_path / "config2"
    sibling.mkdir()
    assert not is_within(sibling / "x.yaml", base)


def test_is_within_rejects_parent_traversal(tmp_path: Path) -> None:
    base = tmp_path / "config"
    base.mkdir()
    assert not is_within((base / ".." / "etc" / "passwd").resolve(), base)


def test_is_denied() -> None:
    assert is_denied("secrets.yaml")
    assert is_denied("SECRETS.YAML")
    assert not is_denied("automations.yaml")


def test_is_denied_path_denies_secrets_yaml(tmp_path: Path) -> None:
    base = tmp_path / "config"
    base.mkdir()
    assert is_denied_path(base / "secrets.yaml", base)
    assert is_denied_path(base / "SECRETS.YAML", base)


def test_is_denied_path_denies_anything_under_storage(tmp_path: Path) -> None:
    """The credential-bearing directory is denied whole, by directory membership —
    not by enumerating the filenames inside it (that was the defect: a
    filename-only denylist protects the reference file, `.storage/core.config_entries`
    holds the secrets themselves and was never on the list)."""
    base = tmp_path / "config"
    base.mkdir()
    assert is_denied_path(base / ".storage" / "core.config_entries", base)
    assert is_denied_path(base / ".storage" / "auth", base)
    assert is_denied_path(base / ".storage" / "auth_provider.homeassistant", base)
    assert is_denied_path(base / ".storage" / "core.restore_state", base)
    # The directory itself, not just files inside it.
    assert is_denied_path(base / ".storage", base)
    # Case-insensitive, matching is_denied's existing behaviour.
    assert is_denied_path(base / ".STORAGE" / "core.config_entries", base)


def test_is_denied_path_allows_ordinary_yaml(tmp_path: Path) -> None:
    base = tmp_path / "config"
    base.mkdir()
    assert not is_denied_path(base / "automations.yaml", base)
    assert not is_denied_path(base / "packages" / "energy.yaml", base)


def test_resolve_config_path_rejects_storage(tmp_path: Path) -> None:
    base = tmp_path / "config"
    base.mkdir()
    with pytest.raises(web.HTTPForbidden):
        _resolve_config_path(str(base), ".storage/core.config_entries")


def test_resolve_config_path_rejects_sibling_prefix(tmp_path: Path) -> None:
    """A ../config2 escape must be rejected even though it shares the base prefix."""
    base = tmp_path / "config"
    base.mkdir()
    (tmp_path / "config2").mkdir()
    with pytest.raises(web.HTTPBadRequest):
        _resolve_config_path(str(base), "../config2/x.yaml")
