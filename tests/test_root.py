"""Tests for GET / — status page."""

import pytest
from aiohttp.test_utils import TestClient

from companion import __version__

_INGRESS = {"X-Ingress-Path": "/test_addon"}


@pytest.fixture(autouse=True)
def _trust_loopback_ingress(monkeypatch: pytest.MonkeyPatch) -> None:
    """The root status page is served via ingress; trust the loopback test client."""
    monkeypatch.setenv("INGRESS_PROXY_IPS", "127.0.0.1")


async def test_root_returns_ok(client: TestClient) -> None:
    resp = await client.get("/", headers=_INGRESS)
    assert resp.status == 200


async def test_root_contains_version(client: TestClient) -> None:
    resp = await client.get("/", headers=_INGRESS)
    text = await resp.text()
    assert __version__ in text


async def test_root_multiple_slashes_normalized(client: TestClient) -> None:
    """Ingress sometimes delivers paths with extra leading slashes."""
    resp = await client.get("////", headers=_INGRESS)
    assert resp.status == 200


async def test_root_requires_auth(client: TestClient) -> None:
    resp = await client.get("/")
    assert resp.status == 401
