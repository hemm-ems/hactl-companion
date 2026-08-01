"""Tests for YAML config write endpoints (Phase 3)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from companion import core_api

if TYPE_CHECKING:
    from aiohttp.test_utils import TestClient


NEW_YAML_CONTENT = """- id: automation.door_light
  alias: Door Light Updated
  trigger:
    - platform: state
      entity_id: binary_sensor.front_door
      to: "on"
  action:
    - service: light.turn_on
      target:
        entity_id: light.hallway_new
"""


async def test_dry_run_returns_diff(client: TestClient, auth_headers: dict[str, str]) -> None:
    """dry_run=true should return a diff without modifying the file."""
    resp = await client.put(
        "/v1/config/file?path=automations.yaml&dry_run=true",
        data=NEW_YAML_CONTENT,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "dry_run"
    assert "diff" in data
    # Diff should show changes
    assert "---" in data["diff"] or data["diff"] == ""


async def test_dry_run_is_default(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Default should be dry_run=true."""
    resp = await client.put(
        "/v1/config/file?path=automations.yaml",
        data=NEW_YAML_CONTENT,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "dry_run"


async def test_apply_creates_backup(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """dry_run=false should create a backup file (core check-config is faked valid)."""
    resp = await client.put(
        "/v1/config/file?path=automations.yaml&dry_run=false",
        data=NEW_YAML_CONTENT,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "applied"
    assert data["validated"] is True
    assert "backup" in data

    # Verify backup file exists
    backup_files = list((config_dir / ".hactl_backups").glob("automations.yaml.bak.*"))
    assert len(backup_files) == 1

    # Verify new content was written
    content = (config_dir / "automations.yaml").read_text()
    assert "Door Light Updated" in content


async def test_apply_validation_failure_restores(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If config validation fails, the backup should be restored."""

    async def _invalid() -> tuple[bool, str]:
        return False, "Invalid config"

    monkeypatch.setattr(core_api, "check_config", _invalid)

    original_content = (config_dir / "automations.yaml").read_text()

    resp = await client.put(
        "/v1/config/file?path=automations.yaml&dry_run=false",
        data=NEW_YAML_CONTENT,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 400
    body = await resp.text()
    assert "Backup restored" in body

    # Original content should be restored
    restored_content = (config_dir / "automations.yaml").read_text()
    assert restored_content == original_content


async def test_apply_new_file_validation_failure_removes_file(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NEW file that fails validation must be deleted — there is no backup to restore."""

    async def _invalid() -> tuple[bool, str]:
        return False, "Invalid config"

    monkeypatch.setattr(core_api, "check_config", _invalid)

    target = config_dir / "brand_new.yaml"
    assert not target.exists()

    resp = await client.put(
        "/v1/config/file?path=brand_new.yaml&dry_run=false",
        data="some_key: value\n",
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 400
    body = await resp.text()
    assert "New file removed" in body
    # The invalid new file must NOT be left on disk.
    assert not target.exists()


async def test_apply_validation_unavailable_rolls_back(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validator that cannot run (timeout/unreachable) must gate the write, not skip it."""

    async def _unavailable() -> tuple[bool, str]:
        raise core_api.CoreAPIUnavailableError("timed out")

    monkeypatch.setattr(core_api, "check_config", _unavailable)

    original_content = (config_dir / "automations.yaml").read_text()

    resp = await client.put(
        "/v1/config/file?path=automations.yaml&dry_run=false",
        data=NEW_YAML_CONTENT,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 503
    # Write must have been rolled back — a failed check never un-gates the write.
    assert (config_dir / "automations.yaml").read_text() == original_content


async def test_apply_skipped_when_no_supervisor_token(
    client: TestClient, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No SUPERVISOR_TOKEN -> validation is skipped and the write is allowed (dev stack).

    Auth fails closed for bearer when the token is unset, so this exercises the
    handler via a trusted ingress source instead.
    """
    monkeypatch.setenv("SUPERVISOR_TOKEN", "")
    monkeypatch.setenv("INGRESS_PROXY_IPS", "127.0.0.1")

    resp = await client.put(
        "/v1/config/file?path=automations.yaml&dry_run=false",
        data=NEW_YAML_CONTENT,
        headers={"X-Ingress-Path": "/api/hassio_ingress/x", "Content-Type": "text/plain"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "applied"
    assert data["validated"] is False


async def test_write_empty_body_rejected(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.put(
        "/v1/config/file?path=automations.yaml&dry_run=false",
        data="",
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 400


async def test_write_invalid_yaml_rejected(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.put(
        "/v1/config/file?path=automations.yaml&dry_run=false",
        data=": invalid:\n  - :\n  [broken",
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 400


async def test_write_path_traversal_rejected(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.put(
        "/v1/config/file?path=../etc/passwd&dry_run=false",
        data="hacked: true\n",
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 400


async def test_write_secrets_denied(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    (config_dir / "secrets.yaml").write_text("wifi_password: hunter2\n")
    resp = await client.put(
        "/v1/config/file?path=secrets.yaml&dry_run=false",
        data="wifi_password: newpassword\n",
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 403


async def test_write_storage_denied(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """The write route refuses `.storage` exactly like the two read routes.

    A guard applied to one route and not its siblings is the "two of four
    sites" defect class this project keeps hitting (see INVARIANTS.md C-3).
    """
    resp = await client.put(
        "/v1/config/file?path=.storage/core.config_entries&dry_run=false",
        data="hacked: true\n",
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 403
    # Nothing must have been written under .storage by a refused request.
    assert not (config_dir / ".storage" / "core.config_entries").exists()


async def test_concurrent_writes_no_corruption(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Two simultaneous writes to the same file must not corrupt it — last write wins cleanly."""
    import asyncio

    payload_a = "concurrent_test:\n  writer: A\n  value: 1\n"
    payload_b = "concurrent_test:\n  writer: B\n  value: 2\n"

    async def write(payload: str) -> int:
        resp = await client.put(
            "/v1/config/file?path=concurrent-test.yaml&dry_run=false",
            data=payload,
            headers={**auth_headers, "Content-Type": "text/plain"},
        )
        return resp.status

    results = await asyncio.gather(write(payload_a), write(payload_b))

    # Both must succeed (200) — no 500/lock errors
    for status in results:
        assert status == 200, f"unexpected status {status} during concurrent write"

    # File must be valid YAML and match exactly one of the two payloads
    resp = await client.get(
        "/v1/config/file?path=concurrent-test.yaml",
        headers=auth_headers,
    )
    assert resp.status == 200
    content = (await resp.json())["content"]

    assert content in (payload_a, payload_b), (
        f"file content is neither payload_a nor payload_b — possible corruption:\n{content!r}"
    )
