"""Tests for GET /v1/health."""

from aiohttp.test_utils import TestClient


async def test_health_returns_ok(client: TestClient) -> None:
    resp = await client.get("/v1/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"


async def test_health_includes_version(client: TestClient) -> None:
    resp = await client.get("/v1/health")
    data = await resp.json()
    assert "version" in data
    assert data["version"] == "0.3.0"


async def test_health_no_auth_required(client: TestClient) -> None:
    """Health endpoint must work without any auth headers."""
    resp = await client.get("/v1/health")
    assert resp.status == 200
