"""Tests for GET /v1/health."""

from aiohttp.test_utils import TestClient
from companion import __version__


async def test_health_returns_ok(client: TestClient) -> None:
    resp = await client.get("/v1/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"


async def test_health_includes_version(client: TestClient) -> None:
    resp = await client.get("/v1/health")
    data = await resp.json()
    assert "version" in data
    assert data["version"] == __version__


async def test_health_no_auth_required(client: TestClient) -> None:
    """Health endpoint must work without any auth headers."""
    resp = await client.get("/v1/health")
    assert resp.status == 200
