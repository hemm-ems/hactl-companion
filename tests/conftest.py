"""Shared test fixtures."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient

from companion import core_api
from companion.server import create_app

FIXTURES_DIR = Path(__file__).parent.parent / "testdata" / "fixtures"
TEST_TOKEN = "test-supervisor-token-12345"


@pytest.fixture(autouse=True)
def core_api_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Fake the HA core API so tests never make real network calls.

    Records (domain, service) tuples; returns success for everything.
    """
    calls: list[tuple[str, str]] = []

    async def _fake_call_service(domain: str, service: str, data: object = None) -> bool:
        calls.append((domain, service))
        return True

    async def _fake_check_config() -> tuple[bool, str]:
        return True, ""

    monkeypatch.setattr(core_api, "call_service", _fake_call_service)
    monkeypatch.setattr(core_api, "check_config", _fake_check_config)
    return calls


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Create a temporary config directory populated with test fixtures."""
    # Copy YAML fixture files to the temp dir
    for src_file in FIXTURES_DIR.iterdir():
        if src_file.is_file() and src_file.suffix in (".yaml", ".yml"):
            shutil.copy2(src_file, tmp_path / src_file.name)
        elif src_file.is_dir():
            shutil.copytree(src_file, tmp_path / src_file.name)
    return tmp_path


@pytest.fixture
def app(config_dir: Path) -> None:
    """Create test application with temp config dir."""
    os.environ["SUPERVISOR_TOKEN"] = TEST_TOKEN
    application = create_app(config_base_path=str(config_dir))
    return application  # type: ignore[return-value]


@pytest.fixture
async def client(app: object, aiohttp_client: object) -> TestClient:
    """Create an authenticated test client."""
    return await aiohttp_client(app)  # type: ignore[misc]


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Return headers with valid auth token."""
    return {"Authorization": f"Bearer {TEST_TOKEN}"}
