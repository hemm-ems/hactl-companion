"""Tests for the HA config mount detection (companion.paths)."""

from __future__ import annotations

from pathlib import Path

import pytest

from companion import paths


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMPANION_CONFIG_BASE", raising=False)


def test_prefers_supervisor_mount(tmp_path: Path) -> None:
    ha = tmp_path / "homeassistant"
    ha.mkdir()
    legacy = tmp_path / "config"
    assert paths.config_base(ha_mount=ha, legacy_mount=legacy) == ha


def test_falls_back_to_legacy_mount(tmp_path: Path) -> None:
    ha = tmp_path / "homeassistant"  # not created
    legacy = tmp_path / "config"
    assert paths.config_base(ha_mount=ha, legacy_mount=legacy) == legacy


def test_env_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ha = tmp_path / "homeassistant"
    ha.mkdir()
    override = tmp_path / "elsewhere"
    monkeypatch.setenv("COMPANION_CONFIG_BASE", str(override))
    assert paths.config_base(ha_mount=ha, legacy_mount=tmp_path / "config") == override


def test_hactl_dir_under_base(tmp_path: Path) -> None:
    assert paths.hactl_dir(tmp_path) == tmp_path / "hactl"
