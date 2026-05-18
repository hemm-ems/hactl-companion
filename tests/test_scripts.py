"""Tests for script CRUD endpoints (Phase 4)."""

from __future__ import annotations

from pathlib import Path

from aiohttp.test_utils import TestClient


async def test_list_scripts(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Should list all script definitions."""
    resp = await client.get("/v1/config/scripts", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    scripts = data["scripts"]
    assert len(scripts) == 3
    ids = [s["id"] for s in scripts]
    assert "welcome_home" in ids
    assert "kino_start" in ids
    assert "goodnight" in ids


async def test_list_scripts_fields(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Scripts with fields should include field metadata."""
    resp = await client.get("/v1/config/scripts", headers=auth_headers)
    data = await resp.json()
    kino = next(s for s in data["scripts"] if s["id"] == "kino_start")
    assert len(kino["fields"]) == 1
    assert kino["fields"][0]["name"] == "brightness"


async def test_get_script_by_id(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Should return a single script by id."""
    resp = await client.get("/v1/config/script?id=welcome_home", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    assert data["id"] == "welcome_home"
    assert "Welcome Home" in data["content"]


async def test_get_script_not_found(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.get("/v1/config/script?id=nonexistent", headers=auth_headers)
    assert resp.status == 404


async def test_get_script_missing_id(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.get("/v1/config/script", headers=auth_headers)
    assert resp.status == 400


async def test_update_script_dry_run(client: TestClient, auth_headers: dict[str, str]) -> None:
    """PUT with dry_run=true should return diff."""
    new_body = """alias: Welcome Home Updated
sequence:
  - service: light.turn_on
    target:
      entity_id: light.kitchen
"""
    resp = await client.put(
        "/v1/config/script?id=welcome_home&dry_run=true",
        data=new_body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "dry_run"
    assert "diff" in data


async def test_update_script_apply(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """PUT with dry_run=false should update and create backup."""
    new_body = """alias: Welcome Home Updated
sequence:
  - service: light.turn_on
    target:
      entity_id: light.kitchen
"""
    resp = await client.put(
        "/v1/config/script?id=welcome_home&dry_run=false",
        data=new_body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "applied"

    # Verify backup
    backups = list(config_dir.glob("scripts.yaml.bak.*"))
    assert len(backups) >= 1

    # Verify updated
    resp2 = await client.get("/v1/config/script?id=welcome_home", headers=auth_headers)
    data2 = await resp2.json()
    assert "Updated" in data2["content"]


async def test_create_script(client: TestClient, auth_headers: dict[str, str]) -> None:
    """POST should create a new script."""
    new_body = """new_script:
  alias: New Script
  mode: single
  sequence:
    - service: light.turn_off
      target:
        entity_id: all
"""
    resp = await client.post(
        "/v1/config/script",
        data=new_body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 201
    data = await resp.json()
    assert data["id"] == "new_script"


async def test_create_script_duplicate(client: TestClient, auth_headers: dict[str, str]) -> None:
    body = """welcome_home:
  alias: Duplicate
  sequence: []
"""
    resp = await client.post(
        "/v1/config/script",
        data=body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 409


async def test_delete_script(client: TestClient, auth_headers: dict[str, str]) -> None:
    """DELETE should remove the script."""
    resp = await client.delete("/v1/config/script?id=goodnight", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "deleted"

    resp2 = await client.get("/v1/config/script?id=goodnight", headers=auth_headers)
    assert resp2.status == 404


async def test_delete_script_not_found(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.delete("/v1/config/script?id=nonexistent", headers=auth_headers)
    assert resp.status == 404
