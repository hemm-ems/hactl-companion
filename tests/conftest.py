"""Shared test fixtures."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient

from companion import core_api
from companion.server import create_app

TREE_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = TREE_ROOT / "testdata" / "fixtures"
TEST_TOKEN = "test-supervisor-token-12345"


def _inside_tree(path: Path) -> bool:
    return path == TREE_ROOT or TREE_ROOT in path.parents


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to run this tree's tests against another tree's interpreter or sources.

    ``git worktree add`` creates a tree with no ``.venv``. The first ``uv run``
    builds one but installs only the **base** dependency group — pytest, ruff and
    mypy live under ``[project.optional-dependencies] dev`` — so ``uv run pytest``
    finds no pytest in the new venv and falls through ``PATH`` to the next one,
    normally the main checkout's. ``make test`` then executes *this* tree's
    ``tests/`` against the *other* tree's ``src/companion``: every result, green or
    red, is evidence about the wrong branch. The failure is silent and has already
    cost real debugging time (an agent "reproduced" a defect that did not exist).

    Both halves are checked because they fail independently: ``sys.prefix`` names
    the environment that actually resolved ``pytest``, while ``companion.__file__``
    names the sources under test — an editable install pointing elsewhere would
    pass the first check and fail the second.

    This lives in ``conftest.py`` rather than the ``Makefile`` on purpose: it then
    also fires for a bare ``pytest`` or an IDE runner, which never go through
    ``make``. It costs two path comparisons once per session.
    """
    import companion

    problems = []
    prefix = Path(sys.prefix).resolve()
    if not _inside_tree(prefix):
        problems.append(f"interpreter/venv is {prefix}, not inside {TREE_ROOT}")
    package = Path(companion.__file__).resolve()
    if not _inside_tree(package):
        problems.append(f"'companion' imports from {package}, not inside {TREE_ROOT}")
    if problems:
        raise pytest.UsageError(
            "tests would not exercise this tree's code: "
            + "; ".join(problems)
            + f"\nfix: cd {TREE_ROOT} && uv sync --all-extras   (then re-run; 'uv sync' alone omits the dev extra "
            "that provides pytest, which is how the wrong venv gets used)"
        )


@pytest.fixture(autouse=True)
def core_api_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Fake the HA core API so tests never make real network calls.

    Records (domain, service) tuples; returns success for everything.
    """
    calls: list[tuple[str, str]] = []

    async def _fake_call_service(domain: str, service: str, data: object = None) -> core_api.ServiceResult:
        calls.append((domain, service))
        return core_api.ServiceResult(True)

    async def _fake_check_config() -> tuple[bool, str]:
        return True, ""

    async def _fake_get_state(entity_id: str) -> dict[str, object] | None:
        return {"entity_id": entity_id, "state": "unknown", "attributes": {}}

    async def _fake_get_states() -> list[dict[str, object]] | None:
        return []

    monkeypatch.setattr(core_api, "call_service", _fake_call_service)
    monkeypatch.setattr(core_api, "check_config", _fake_check_config)
    monkeypatch.setattr(core_api, "get_state", _fake_get_state)
    monkeypatch.setattr(core_api, "get_states", _fake_get_states)
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
