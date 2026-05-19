"""Assert version consistency across all four version-bearing files and CHANGELOG."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def _read_version(path: Path, pattern: str) -> str:
    text = path.read_text()
    m = re.search(pattern, text, re.MULTILINE)
    assert m, f"version pattern not found in {path}"
    return m.group(1)


@pytest.fixture(scope="module")
def expected_version() -> str:
    from companion import __version__

    return __version__


def test_pyproject_version(expected_version: str) -> None:
    ver = _read_version(ROOT / "pyproject.toml", r'^version = "([^"]+)"')
    assert ver == expected_version, f"pyproject.toml {ver!r} != __version__ {expected_version!r}"


def test_config_yaml_version(expected_version: str) -> None:
    ver = _read_version(ROOT / "hactl_companion/config.yaml", r'^version: "([^"]+)"')
    assert ver == expected_version, f"config.yaml {ver!r} != __version__ {expected_version!r}"


def test_openapi_version(expected_version: str) -> None:
    ver = _read_version(ROOT / "openapi/companion-v1.yaml", r"^\s+version: ([^\s]+)")
    assert ver == expected_version, f"openapi spec {ver!r} != __version__ {expected_version!r}"


def test_changelog_has_current_version(expected_version: str) -> None:
    changelog = ROOT / "hactl_companion/CHANGELOG.md"
    assert changelog.exists(), "hactl_companion/CHANGELOG.md does not exist"
    assert f"## {expected_version}" in changelog.read_text(), (
        f"CHANGELOG.md has no entry for version {expected_version}"
    )
