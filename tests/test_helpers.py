"""Tests for helper CRUD endpoints."""

from __future__ import annotations

from pathlib import Path

from aiohttp.test_utils import TestClient


async def test_list_helpers_all(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Should list helpers across all domains."""
    resp = await client.get("/v1/config/helpers", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    helpers = data["helpers"]
    # We have fixtures for input_boolean (2), input_number (1), counter (1)
    assert len(helpers) >= 4
    ids = [h["id"] for h in helpers]
    assert "guest_mode" in ids
    assert "vacation_mode" in ids
    assert "target_temperature" in ids


async def test_list_helpers_by_domain(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Should filter helpers by domain."""
    resp = await client.get("/v1/config/helpers?domain=input_boolean", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    helpers = data["helpers"]
    assert len(helpers) == 2
    assert all(h["domain"] == "input_boolean" for h in helpers)


async def test_list_helpers_invalid_domain(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Should reject invalid domain."""
    resp = await client.get("/v1/config/helpers?domain=bogus", headers=auth_headers)
    assert resp.status == 400


async def test_get_helper(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Should return a single helper by id."""
    resp = await client.get("/v1/config/helper?id=guest_mode", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    assert data["id"] == "guest_mode"
    assert data["domain"] == "input_boolean"
    assert "Guest Mode" in data["content"]


async def test_get_helper_not_found(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.get("/v1/config/helper?id=nonexistent", headers=auth_headers)
    assert resp.status == 404


async def test_get_helper_missing_id(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.get("/v1/config/helper", headers=auth_headers)
    assert resp.status == 400


async def test_create_helper(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """POST should create a new helper."""
    body = "party_mode:\n  name: Party Mode\n  icon: mdi:party-popper\n"
    resp = await client.post(
        "/v1/config/helper?domain=input_boolean",
        data=body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 201
    data = await resp.json()
    assert data["status"] == "created"
    assert data["id"] == "party_mode"

    # Verify it appears in list
    resp2 = await client.get("/v1/config/helpers?domain=input_boolean", headers=auth_headers)
    data2 = await resp2.json()
    ids = [h["id"] for h in data2["helpers"]]
    assert "party_mode" in ids


async def test_create_helper_duplicate(client: TestClient, auth_headers: dict[str, str]) -> None:
    """POST should reject duplicate id."""
    body = "guest_mode:\n  name: Duplicate\n"
    resp = await client.post(
        "/v1/config/helper?domain=input_boolean",
        data=body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 409


async def test_create_helper_missing_domain(client: TestClient, auth_headers: dict[str, str]) -> None:
    """POST without domain should fail."""
    body = "test:\n  name: Test\n"
    resp = await client.post(
        "/v1/config/helper",
        data=body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 400


async def test_create_helper_invalid_yaml(client: TestClient, auth_headers: dict[str, str]) -> None:
    """POST with non-mapping body should fail."""
    resp = await client.post(
        "/v1/config/helper?domain=input_boolean",
        data="- just a list item",
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 400


async def test_update_helper(client: TestClient, auth_headers: dict[str, str]) -> None:
    """PUT should update an existing helper."""
    body = "name: Guest Mode Updated\nicon: mdi:account-check\n"
    resp = await client.put(
        "/v1/config/helper?id=guest_mode",
        data=body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "applied"


async def test_update_helper_not_found(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.put(
        "/v1/config/helper?id=nonexistent",
        data="name: X\n",
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 404


async def test_delete_helper(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """DELETE should remove a helper and create backup."""
    resp = await client.delete("/v1/config/helper?id=vacation_mode", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "deleted"

    # Verify it's gone
    resp2 = await client.get("/v1/config/helper?id=vacation_mode", headers=auth_headers)
    assert resp2.status == 404

    # Verify backup was created
    backups = list(config_dir.glob("input_boolean.yaml.bak.*"))
    assert len(backups) >= 1


async def test_delete_helper_not_found(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.delete("/v1/config/helper?id=nonexistent", headers=auth_headers)
    assert resp.status == 404


async def test_delete_helper_missing_id(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.delete("/v1/config/helper", headers=auth_headers)
    assert resp.status == 400


async def test_create_helper_new_domain_file(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """POST should create the YAML file if it doesn't exist."""
    body = "wash_cycle:\n  name: Wash Cycle\n  duration: '00:01:30'\n"
    resp = await client.post(
        "/v1/config/helper?domain=timer",
        data=body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 201
    assert (config_dir / "timer.yaml").is_file()
