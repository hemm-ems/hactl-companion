"""Tests for the HA core API client (Supervisor proxy)."""

from __future__ import annotations

import re
from typing import Any

import pytest
from aiohttp import web

# Bind the real implementations at import time: the autouse core_api_calls
# fixture replaces the module attributes, but these references stay intact.
from companion.core_api import _EXCERPT_CHARS, CoreAPIUnavailableError, call_service, check_config, get_state

TOKEN = "core-api-test-token"


def _make_core_app(
    check_result: dict[str, Any] | None = None,
    fail: bool = False,
    fail_status: int = 500,
    fail_body: str = '{"message": "boom"}',
) -> web.Application:
    """Fake HA core API recording service calls."""
    app = web.Application()
    app["service_calls"] = []
    app["auth_headers"] = []

    async def handle_service(request: web.Request) -> web.Response:
        app["auth_headers"].append(request.headers.get("Authorization", ""))
        app["service_calls"].append((request.match_info["domain"], request.match_info["service"]))
        if fail:
            return web.Response(text=fail_body, status=fail_status, content_type="application/json")
        return web.json_response([])

    async def handle_check(request: web.Request) -> web.Response:
        app["auth_headers"].append(request.headers.get("Authorization", ""))
        return web.json_response(check_result or {"result": "valid", "errors": None})

    app.router.add_post("/services/{domain}/{service}", handle_service)
    app.router.add_post("/config/core/check_config", handle_check)
    return app


@pytest.fixture
def core_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("SUPERVISOR_TOKEN", TOKEN)
    return monkeypatch


async def test_call_service_success(aiohttp_server: Any, core_env: pytest.MonkeyPatch) -> None:
    app = _make_core_app()
    server = await aiohttp_server(app)
    core_env.setenv("CORE_API_URL", str(server.make_url("")))

    result = await call_service("automation", "reload")
    assert result.ok is True
    assert result.error is None
    assert app["service_calls"] == [("automation", "reload")]
    assert app["auth_headers"] == [f"Bearer {TOKEN}"]


async def test_call_service_http_error(aiohttp_server: Any, core_env: pytest.MonkeyPatch) -> None:
    """A refusal by HA is reported as not-ok **and** says what HA said.

    The status and HA's own words are the whole diagnosis: with only a bool, a
    "Service not found" (the integration is not loaded) and a "not authorized"
    are the same unactionable failure to everyone upstream.
    """
    server = await aiohttp_server(
        _make_core_app(fail=True, fail_status=400, fail_body='{"message": "Service not found"}')
    )
    core_env.setenv("CORE_API_URL", str(server.make_url("")))

    result = await call_service("automation", "reload")
    assert result.ok is False
    assert result.error is not None
    assert "HTTP 400" in result.error
    assert "Service not found" in result.error
    assert TOKEN not in result.error, "the reason must never carry the Supervisor token"


async def test_call_service_error_body_is_bounded(aiohttp_server: Any, core_env: pytest.MonkeyPatch) -> None:
    """HA's body is excerpted, not forwarded whole — the reason goes on the wire."""
    server = await aiohttp_server(_make_core_app(fail=True, fail_status=400, fail_body="x" * 5000))
    core_env.setenv("CORE_API_URL", str(server.make_url("")))

    result = await call_service("automation", "reload")
    assert result.error is not None
    assert result.error.count("x") == _EXCERPT_CHARS
    assert len(result.error) < 2 * _EXCERPT_CHARS


async def test_call_service_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    result = await call_service("automation", "reload")
    assert result.ok is False
    assert result.error == "SUPERVISOR_TOKEN not set"


async def test_call_service_unreachable(core_env: pytest.MonkeyPatch) -> None:
    """A transport failure names the exception class — there is no HTTP status to name."""
    core_env.setenv("CORE_API_URL", "http://127.0.0.1:1")
    result = await call_service("automation", "reload")
    assert result.ok is False
    assert result.error is not None
    assert re.match(r"^\w+Error: ", result.error), result.error
    assert TOKEN not in result.error


async def test_check_config_valid(aiohttp_server: Any, core_env: pytest.MonkeyPatch) -> None:
    server = await aiohttp_server(_make_core_app())
    core_env.setenv("CORE_API_URL", str(server.make_url("")))

    valid, errors = await check_config()
    assert valid is True
    assert errors == ""


async def test_check_config_invalid(aiohttp_server: Any, core_env: pytest.MonkeyPatch) -> None:
    server = await aiohttp_server(_make_core_app(check_result={"result": "invalid", "errors": "bad yaml"}))
    core_env.setenv("CORE_API_URL", str(server.make_url("")))

    valid, errors = await check_config()
    assert valid is False
    assert "bad yaml" in errors


async def test_check_config_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    with pytest.raises(CoreAPIUnavailableError, match="SUPERVISOR_TOKEN"):
        await check_config()


async def test_check_config_unreachable(core_env: pytest.MonkeyPatch) -> None:
    core_env.setenv("CORE_API_URL", "http://127.0.0.1:1")
    with pytest.raises(CoreAPIUnavailableError):
        await check_config()


async def test_get_state_url_encodes_entity_id(aiohttp_server: Any, core_env: pytest.MonkeyPatch) -> None:
    """entity_id must be percent-encoded into the path, not interpolated raw.

    A raw '?' would otherwise be parsed as a query separator, truncating the id.
    """
    app = web.Application()
    seen: dict[str, str] = {}

    async def handle(request: web.Request) -> web.Response:
        seen["entity_id"] = request.match_info["entity_id"]
        return web.json_response({"entity_id": request.match_info["entity_id"], "state": "on"})

    app.router.add_get("/states/{entity_id}", handle)
    server = await aiohttp_server(app)
    core_env.setenv("CORE_API_URL", str(server.make_url("")))

    result = await get_state("sensor.x?evil=1")
    assert result is not None
    assert seen["entity_id"] == "sensor.x?evil=1"
