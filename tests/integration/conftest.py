"""Session-scoped fixtures: docker compose lifecycle, HA onboarding, companion access."""

from __future__ import annotations

import json
import subprocess
import time

import pytest
import requests
import websocket

COMPOSE_FILE = "docker-compose.integration.yaml"
COMPANION_TOKEN = "integration-test-token-12345"
CLIENT_ID = "http://hactl-test"

# ---------------------------------------------------------------------------
# Docker Compose lifecycle
# ---------------------------------------------------------------------------


def _compose(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "compose", "-f", COMPOSE_FILE, *args]
    return subprocess.run(cmd, capture_output=capture, text=True, check=True, timeout=360)


def _get_mapped_port(service: str, container_port: int) -> str:
    result = _compose("port", service, str(container_port), capture=True)
    # Output is like "0.0.0.0:55123" or "[::]:55123"
    return result.stdout.strip().rsplit(":", maxsplit=1)[-1]


@pytest.fixture(scope="session")
def compose_up():
    """Start the integration stack and yield port mappings, then tear down."""
    _compose("up", "-d", "--build")
    try:
        ha_port = _get_mapped_port("homeassistant", 8123)
        comp_port = _get_mapped_port("companion", 9100)
        ha_url = f"http://localhost:{ha_port}"
        companion_url = f"http://localhost:{comp_port}"
        # Wait for HA to be reachable before yielding
        _wait_for_ha(ha_url)
        # Wait for companion to be reachable
        _wait_for_url(f"{companion_url}/v1/health", timeout=30)
        yield {
            "ha_url": ha_url,
            "companion_url": companion_url,
        }
    finally:
        _compose("down", "-v")


# ---------------------------------------------------------------------------
# HA headless onboarding (mirrors hactl's hatest.go sequence)
# ---------------------------------------------------------------------------


def _wait_for_ha(base_url: str, timeout: int = 180) -> None:
    """Wait until HA's onboarding endpoint is reachable."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{base_url}/api/onboarding", timeout=5)
            if r.status_code == 200:
                return
        except requests.ConnectionError:
            pass
        time.sleep(2)
    msg = f"HA did not become ready at {base_url} within {timeout}s"
    raise TimeoutError(msg)


def _wait_for_url(url: str, timeout: int = 30) -> None:
    """Wait until a URL returns 200."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                return
        except requests.ConnectionError:
            pass
        time.sleep(1)
    msg = f"URL {url} did not become reachable within {timeout}s"
    raise TimeoutError(msg)


def _onboard_ha(base_url: str) -> str:
    """Run the 5-step headless onboarding and return a long-lived access token."""
    _wait_for_ha(base_url)

    # Step 1: Create owner user
    r = requests.post(
        f"{base_url}/api/onboarding/users",
        json={
            "client_id": CLIENT_ID,
            "name": "Test Owner",
            "username": "testowner",
            "password": "testpass1234!",
            "language": "en",
        },
        timeout=30,
    )
    r.raise_for_status()
    auth_code = r.json()["auth_code"]

    # Step 2: Exchange auth code for access token
    r = requests.post(
        f"{base_url}/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "client_id": CLIENT_ID,
        },
        timeout=30,
    )
    r.raise_for_status()
    access_token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    # Step 3: Complete core_config wizard step
    r = requests.post(f"{base_url}/api/onboarding/core_config", json={}, headers=headers, timeout=30)
    r.raise_for_status()

    # Step 4: Complete analytics wizard step
    r = requests.post(f"{base_url}/api/onboarding/analytics", json={}, headers=headers, timeout=30)
    r.raise_for_status()

    # Step 5: Create long-lived token via WebSocket
    ws_url = base_url.replace("http://", "ws://") + "/api/websocket"
    ws = websocket.create_connection(ws_url, timeout=30)
    try:
        ws.recv()  # {"type": "auth_required", ...}
        ws.send(json.dumps({"type": "auth", "access_token": access_token}))
        auth_resp = json.loads(ws.recv())
        assert auth_resp["type"] == "auth_ok", f"WS auth failed: {auth_resp}"

        ws.send(
            json.dumps(
                {
                    "id": 1,
                    "type": "auth/long_lived_access_token",
                    "client_name": "companion-e2e",
                    "lifespan": 365,
                }
            )
        )
        token_resp = json.loads(ws.recv())
        assert token_resp.get("success"), f"WS token creation failed: {token_resp}"
        return token_resp["result"]
    finally:
        ws.close()


@pytest.fixture(scope="session")
def ha_token(compose_up: dict[str, str]) -> str:
    """Complete HA onboarding and return a long-lived token."""
    return _onboard_ha(compose_up["ha_url"])


@pytest.fixture(scope="session")
def companion_url(compose_up: dict[str, str]) -> str:
    return compose_up["companion_url"]


@pytest.fixture(scope="session")
def ha_url(compose_up: dict[str, str]) -> str:
    return compose_up["ha_url"]


@pytest.fixture()
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {COMPANION_TOKEN}"}


@pytest.fixture(scope="session")
def _ha_ready(companion_url: str, ha_token: str) -> None:
    """Ensure HA has finished starting and written logs before tests run.

    Depends on ha_token (which implies onboarding is done). Gives HA a
    moment to write its initial log entries and config files.
    """
    # Poll until companion can list config files (proves /config is populated)
    deadline = time.monotonic() + 60
    headers = {"Authorization": f"Bearer {COMPANION_TOKEN}"}
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{companion_url}/v1/config/files", headers=headers, timeout=5)
            if r.status_code == 200 and len(r.json().get("files", [])) > 0:
                return
        except requests.ConnectionError:
            pass
        time.sleep(2)
    msg = "Companion never saw config files in /config"
    raise TimeoutError(msg)
