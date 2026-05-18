"""Tests for auth middleware."""

from aiohttp.test_utils import TestClient


async def test_auth_missing_token(client: TestClient) -> None:
    """Request without auth header to a protected endpoint should return 401."""
    resp = await client.get("/v1/config/files")
    assert resp.status == 401


async def test_auth_invalid_token(client: TestClient) -> None:
    """Request with wrong token should return 401."""
    resp = await client.get(
        "/v1/config/files",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status == 401


async def test_auth_valid_token(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Request with valid token should succeed."""
    resp = await client.get("/v1/config/files", headers=auth_headers)
    assert resp.status == 200


async def test_auth_ingress_header_bypasses_token(client: TestClient) -> None:
    """Requests with X-Ingress-Path header should bypass token auth."""
    resp = await client.get(
        "/v1/config/files",
        headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"},
    )
    assert resp.status == 200


async def test_health_no_auth_required(client: TestClient) -> None:
    """Health endpoint should not require auth."""
    resp = await client.get("/v1/health")
    assert resp.status == 200
