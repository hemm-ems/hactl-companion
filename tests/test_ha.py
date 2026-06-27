"""Tests for HA reload and check-config endpoints (core API backed)."""

from __future__ import annotations

from unittest.mock import patch

from aiohttp.test_utils import TestClient

from companion import core_api


async def test_reload_valid_domain(
    client: TestClient, auth_headers: dict[str, str], core_api_calls: list[tuple[str, str]]
) -> None:
    """POST /v1/ha/reload/{domain} should call the core API and return ok."""
    resp = await client.post("/v1/ha/reload/automation", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert data["domain"] == "automation"
    assert ("automation", "reload") in core_api_calls


async def test_reload_disallowed_domain(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Domains not in the allowlist should be rejected with 400."""
    resp = await client.post("/v1/ha/reload/evil_domain", headers=auth_headers)
    assert resp.status == 400


async def test_reload_invalid_domain_chars(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Domain with special chars should be rejected."""
    resp = await client.post("/v1/ha/reload/auto;rm%20-rf", headers=auth_headers)
    assert resp.status == 400


async def test_reload_core_api_failure(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Failed reload service call should return 502."""

    async def _fail(domain: str, service: str, data: object = None) -> bool:
        return False

    with patch("companion.core_api.call_service", _fail):
        resp = await client.post("/v1/ha/reload/automation", headers=auth_headers)
        assert resp.status == 502


async def test_check_config_ok(client: TestClient, auth_headers: dict[str, str]) -> None:
    """POST /v1/ha/check-config should return ok when config is valid."""
    resp = await client.post("/v1/ha/check-config", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert data["valid"] is True
    assert data["errors"] == ""


async def test_check_config_invalid(client: TestClient, auth_headers: dict[str, str]) -> None:
    """An invalid config is a completed check: 200 with valid=false, not 5xx."""

    async def _invalid() -> tuple[bool, str]:
        return False, "broken automation"

    with patch("companion.core_api.check_config", _invalid):
        resp = await client.post("/v1/ha/check-config", headers=auth_headers)
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "invalid"
        assert data["valid"] is False
        assert "broken automation" in data["errors"]


async def test_check_config_core_unreachable(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Core API transport failure should return 502 with the reason."""

    async def _unreachable() -> tuple[bool, str]:
        raise core_api.CoreAPIUnavailableError("connection refused")

    with patch("companion.core_api.check_config", _unreachable):
        resp = await client.post("/v1/ha/check-config", headers=auth_headers)
        assert resp.status == 502
        assert "connection refused" in await resp.text()
