"""Tests for the HA core API client (Supervisor proxy)."""

from __future__ import annotations

import re
from typing import Any

import pytest
from aiohttp import web

# Bind the real implementations at import time: the autouse core_api_calls
# fixture replaces the module attributes, but these references stay intact.
from companion.core_api import (
    _EXCERPT_CHARS,
    CoreAPIUnavailableError,
    call_service,
    check_config,
    get_state,
    is_live_state,
    poll_for_entity,
)

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


def test_is_live_state_absent() -> None:
    assert is_live_state(None) is False


def test_is_live_state_ordinary_state() -> None:
    assert is_live_state({"state": "on", "attributes": {}}) is True
    assert is_live_state({"state": "unknown", "attributes": {}}) is True


def test_is_live_state_excludes_restored_ghost() -> None:
    """A dropped entity can come back as `state: unavailable` + `restored: true`
    — HA's own record of something that used to exist, not proof this create
    worked. Presence alone would read this as "still there"."""
    assert is_live_state({"state": "unavailable", "attributes": {"restored": True}}) is False


def test_is_live_state_bare_unavailable_without_restored_flag_still_counts_as_created() -> None:
    """`unavailable` alone, with no `restored` flag, is a freshly-set-up entity
    whose own value has not resolved yet (e.g. a template sensor referencing
    another entity that is itself not ready) — a real, HA-registered entity,
    not a leftover from a previous delete. Deliberately narrower than
    `tests/integration/test_live.py::_entity_is_live`, which treats *any*
    `unavailable` as not-loaded: that check answers "is this automation
    currently active" (where `unavailable` only ever means not-loaded-yet),
    this one answers "did the create register a real entity" (where a
    template/script/helper can legitimately be unavailable while genuinely
    existing) — same wire shape, different question, recorded as a decision
    because no test can derive it from HA's behaviour alone."""
    assert is_live_state({"state": "unavailable", "attributes": {}}) is True


async def test_poll_for_entity_succeeds_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def _fake_get_state(entity_id: str) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"state": "on", "attributes": {}}

    monkeypatch.setattr("companion.core_api.get_state", _fake_get_state)
    assert await poll_for_entity("sensor.x", attempts=5, delay=0) is True
    assert calls == 1


async def test_poll_for_entity_ignores_a_restored_ghost(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get_state(entity_id: str) -> dict[str, object]:
        return {"state": "unavailable", "attributes": {"restored": True}}

    monkeypatch.setattr("companion.core_api.get_state", _fake_get_state)
    assert await poll_for_entity("sensor.ghost", attempts=2, delay=0) is False


async def test_poll_for_entity_gives_up_after_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def _fake_get_state(entity_id: str) -> dict[str, object] | None:
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr("companion.core_api.get_state", _fake_get_state)
    assert await poll_for_entity("sensor.never", attempts=3, delay=0) is False
    assert calls == 3
