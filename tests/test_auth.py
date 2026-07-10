"""Tests for auth middleware."""

from __future__ import annotations

import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from companion.server import access_log_middleware


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


async def test_ingress_header_from_untrusted_source_rejected(client: TestClient) -> None:
    """An X-Ingress-Path header from a non-proxy source must NOT bypass auth.

    The header is client-controlled; only requests provably from the Supervisor
    ingress proxy (request.remote in INGRESS_PROXY_IPS) may skip bearer auth.
    The test client connects from 127.0.0.1, which is not the default proxy IP.
    """
    resp = await client.get(
        "/v1/config/files",
        headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"},
    )
    assert resp.status == 401


async def test_ingress_header_from_trusted_source_bypasses_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the request's source IP is a trusted ingress proxy, the header bypasses auth."""
    # The aiohttp test server binds to loopback, so the request comes from 127.0.0.1.
    monkeypatch.setenv("INGRESS_PROXY_IPS", "127.0.0.1")
    resp = await client.get(
        "/v1/config/files",
        headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"},
    )
    assert resp.status == 200


async def test_auth_empty_server_token_fails_closed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """With SUPERVISOR_TOKEN unset, an empty Bearer credential must NOT be accepted."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "")
    # Empty credential (the classic fail-open trigger) must be rejected.
    resp = await client.get("/v1/config/files", headers={"Authorization": "Bearer "})
    assert resp.status == 503
    # And so must a well-formed token, since there is nothing to compare against.
    resp2 = await client.get("/v1/config/files", headers={"Authorization": "Bearer anything"})
    assert resp2.status == 503


async def test_health_no_auth_required(client: TestClient) -> None:
    """Health endpoint should not require auth."""
    resp = await client.get("/v1/health")
    assert resp.status == 200


async def test_error_responses_use_json_envelope(client: TestClient, auth_headers: dict[str, str]) -> None:
    """4xx errors are returned as a JSON envelope {"error": {"code", "message"}}."""
    unauth = await client.get("/v1/config/files")
    assert unauth.status == 401
    assert unauth.content_type == "application/json"
    body = await unauth.json()
    assert body["error"]["code"] == 401
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]

    bad = await client.get("/v1/config/file", headers=auth_headers)  # missing ?path -> 400
    assert bad.status == 400
    assert bad.content_type == "application/json"
    assert (await bad.json())["error"]["code"] == 400


async def test_access_log_middleware_survives_non_http_exception(
    aiohttp_client: object, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-HTTP exception from a handler must not be masked by UnboundLocalError.

    Before the fix, `status` was only bound inside the try/except, so any
    non-HTTPException reaching `finally` raised UnboundLocalError there — hiding
    the real traceback. With `status` seeded to 500, the access line is emitted
    cleanly and the original error propagates to aiohttp's 500 handler.
    """

    async def boom(_request: web.Request) -> web.Response:
        raise RuntimeError("kaboom")

    app = web.Application(middlewares=[access_log_middleware])
    app.router.add_get("/boom", boom)
    client = await aiohttp_client(app)  # type: ignore[operator]

    with caplog.at_level(logging.INFO, logger="companion.access"):
        resp = await client.get("/boom")

    assert resp.status == 500
    assert any("status=500" in record.getMessage() for record in caplog.records), (
        "access_log_middleware did not log a clean status=500 line — the finally block likely raised UnboundLocalError"
    )
