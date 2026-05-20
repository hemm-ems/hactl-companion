"""Tests for GET /v1/status."""

from aiohttp.test_utils import TestClient

from companion import __version__


async def test_status_returns_expected_fields(client: TestClient) -> None:
    resp = await client.get("/v1/status")
    assert resp.status == 200
    data = await resp.json()
    for field in ("version", "supervisor_reachable", "has_ha_cli", "config_writable", "ingress_active", "auth_mode"):
        assert field in data, f"missing field: {field}"


async def test_status_version_matches(client: TestClient) -> None:
    resp = await client.get("/v1/status")
    data = await resp.json()
    assert data["version"] == __version__


async def test_status_no_auth_required(client: TestClient) -> None:
    """Status endpoint must be accessible without authentication (same as health)."""
    resp = await client.get("/v1/status")
    assert resp.status == 200


async def test_status_auth_mode_bearer_without_ingress(client: TestClient) -> None:
    """Without X-Ingress-Path header, auth_mode should be 'bearer'."""
    resp = await client.get("/v1/status")
    data = await resp.json()
    assert data["auth_mode"] == "bearer"


async def test_status_ingress_active_with_header(client: TestClient) -> None:
    """With X-Ingress-Path header, ingress_active should be True and auth_mode 'ingress'."""
    resp = await client.get("/v1/status", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})
    data = await resp.json()
    assert data["ingress_active"] is True
    assert data["auth_mode"] == "ingress"
