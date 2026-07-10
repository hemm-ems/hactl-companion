"""Tests for the shared path-containment guard (companion.pathguard)."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web

from companion.pathguard import is_denied, is_within
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


def test_resolve_config_path_rejects_sibling_prefix(tmp_path: Path) -> None:
    """A ../config2 escape must be rejected even though it shares the base prefix."""
    base = tmp_path / "config"
    base.mkdir()
    (tmp_path / "config2").mkdir()
    with pytest.raises(web.HTTPBadRequest):
        _resolve_config_path(str(base), "../config2/x.yaml")
