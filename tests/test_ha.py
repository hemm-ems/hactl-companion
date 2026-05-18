"""Tests for HA CLI reload endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient


async def test_reload_valid_domain(client: TestClient, auth_headers: dict[str, str]) -> None:
    """POST /v1/ha/reload/{domain} should call ha CLI and return ok."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"", b"")

    with patch("companion.routes.ha.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        resp = await client.post("/v1/ha/reload/automation", headers=auth_headers)
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert data["domain"] == "automation"
        mock_exec.assert_called_once()


async def test_reload_disallowed_domain(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Domains not in the allowlist should be rejected with 400."""
    resp = await client.post("/v1/ha/reload/evil_domain", headers=auth_headers)
    assert resp.status == 400


async def test_reload_invalid_domain_chars(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Domain with special chars should be rejected."""
    resp = await client.post("/v1/ha/reload/auto;rm%20-rf", headers=auth_headers)
    assert resp.status == 400


async def test_reload_subprocess_failure(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Non-zero exit code from ha CLI should return 502."""
    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"", b"reload failed\n")

    with patch("companion.routes.ha.asyncio.create_subprocess_exec", return_value=mock_proc):
        resp = await client.post("/v1/ha/reload/automation", headers=auth_headers)
        assert resp.status == 502


async def test_reload_ha_cli_not_available(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Missing ha CLI should return 502."""
    with patch("companion.routes.ha.asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        resp = await client.post("/v1/ha/reload/automation", headers=auth_headers)
        assert resp.status == 502
