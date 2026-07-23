"""Session-scoped fixtures: docker compose lifecycle, HA onboarding, companion access."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator

import pytest
import requests
import websocket

from tests.related_fixture import docker_seed_script

COMPOSE_FILE = "docker-compose.integration.yaml"
CLIENT_ID = "http://hactl-test"

# ---------------------------------------------------------------------------
# Docker Compose lifecycle
# ---------------------------------------------------------------------------


def _compose(*args: str, capture: bool = False, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "compose", "-f", COMPOSE_FILE, *args]
    return subprocess.run(cmd, capture_output=capture, input=input_text, text=True, check=True, timeout=360)


def _get_mapped_port(service: str, container_port: int) -> str:
    result = _compose("port", service, str(container_port), capture=True)
    # Output is like "0.0.0.0:55123" or "[::]:55123"
    return result.stdout.strip().rsplit(":", maxsplit=1)[-1]


@pytest.fixture(scope="session")
def compose_up():
    """Start HA, onboard it, then start companion with a real HA token.

    There is no real Supervisor in this stack, so companion's SUPERVISOR_TOKEN
    (used both for its own incoming Bearer auth and, via CORE_API_URL, for
    outgoing HA core API calls — see core_api.py) must be a real HA
    long-lived access token. That token only exists after onboarding, so HA
    has to come up and be onboarded before companion starts.
    """
    t0 = time.monotonic()
    print("\n[integration] docker compose up --build homeassistant ...", flush=True)
    _compose("up", "-d", "--build", "homeassistant")
    env_file_path: str | None = None
    try:
        ha_port = _get_mapped_port("homeassistant", 8123)
        ha_url = f"http://localhost:{ha_port}"
        print(f"[integration] waiting for HA at {ha_url} ...", flush=True)
        _wait_for_ha(ha_url)

        print("[integration] onboarding HA to obtain a long-lived token ...", flush=True)
        token = _onboard_ha(ha_url)
        env_file_path = _write_env_file({"SUPERVISOR_TOKEN": token})

        print("[integration] docker compose up --build companion ...", flush=True)
        _compose("--env-file", env_file_path, "up", "-d", "--build", "companion")

        comp_port = _get_mapped_port("companion", 9100)
        companion_url = f"http://localhost:{comp_port}"
        print(f"[integration] waiting for companion at {companion_url} ...", flush=True)
        _wait_for_url(f"{companion_url}/v1/health", timeout=30)
        elapsed = time.monotonic() - t0
        print(f"[integration] stack ready in {elapsed:.1f}s", flush=True)
        yield {
            "ha_url": ha_url,
            "companion_url": companion_url,
            "ha_token": token,
        }
    finally:
        print("\n[integration] docker compose down -v ...", flush=True)
        _compose("down", "-v")
        if env_file_path is not None:
            os.unlink(env_file_path)


def _write_env_file(values: dict[str, str]) -> str:
    """Write a dotenv file for `docker compose --env-file`, return its path."""
    fd, path = tempfile.mkstemp(prefix="hactl-companion-integration-", suffix=".env")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for key, value in values.items():
            f.write(f"{key}={value}\n")
    return path


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
    """The long-lived HA token obtained during onboarding in compose_up.

    Onboarding can only run once per HA instance, so this must reuse the
    token compose_up already created rather than onboarding again.
    """
    return compose_up["ha_token"]


@pytest.fixture(scope="session")
def companion_url(compose_up: dict[str, str]) -> str:
    return compose_up["companion_url"]


@pytest.fixture(scope="session")
def ha_url(compose_up: dict[str, str]) -> str:
    return compose_up["ha_url"]


def _ha_ws_command(ha_url: str, ha_token: str, msg_type: str, **payload: object) -> object:
    """Authenticate over WS and run one HA command, returning its ``result``.

    Reuses the auth handshake from ``_onboard_ha``. Raises AssertionError on a
    failed handshake or an unsuccessful command so a broken oracle query is a
    loud failure, never a silently-empty answer that could pass a ⊇ check.
    """
    ws_url = ha_url.replace("http://", "ws://") + "/api/websocket"
    ws = websocket.create_connection(ws_url, timeout=30)
    try:
        ws.recv()  # {"type": "auth_required", ...}
        ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
        auth_resp = json.loads(ws.recv())
        assert auth_resp["type"] == "auth_ok", f"WS auth failed: {auth_resp}"

        ws.send(json.dumps({"id": 1, "type": msg_type, **payload}))
        while True:
            resp = json.loads(ws.recv())
            if resp.get("id") == 1 and resp.get("type") == "result":
                assert resp.get("success"), f"WS command {msg_type} failed: {resp}"
                return resp["result"]
    finally:
        ws.close()


@pytest.fixture(scope="session")
def ha_ws_command(ha_url: str, ha_token: str):
    """A callable ``(msg_type, **payload) -> result`` bound to the live HA.

    Lets a test ask HA's own WebSocket API (e.g. ``search/related``) so the
    expected value is computed *from HA at test time* rather than hand-authored.
    """

    def _call(msg_type: str, **payload: object) -> object:
        return _ha_ws_command(ha_url, ha_token, msg_type, **payload)

    return _call


@pytest.fixture()
def auth_headers(ha_token: str) -> dict[str, str]:
    """Bearer token for companion's own direct-access auth.

    Companion has no real Supervisor in this stack, so its SUPERVISOR_TOKEN
    (see docker-compose.integration.yaml) is the real HA long-lived token —
    the same value authenticates both companion's incoming requests and its
    outgoing core API calls.
    """
    return {"Authorization": f"Bearer {ha_token}"}


@pytest.fixture(scope="session")
def _ha_ready(companion_url: str, ha_token: str) -> None:
    """Ensure HA has finished starting and written logs before tests run.

    Depends on ha_token (which implies onboarding is done). Gives HA a
    moment to write its initial log entries and config files.
    """
    # Poll until companion can list config files (proves /config is populated)
    deadline = time.monotonic() + 60
    headers = {"Authorization": f"Bearer {ha_token}"}
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


@pytest.fixture(scope="session")
def related_fixture_seeded(companion_url: str, ha_token: str, _ha_ready: None) -> Iterator[None]:
    """Seed the disposable Docker /config volume with related graph fixture data."""
    _compose("exec", "-T", "companion", "python3", "-", input_text=docker_seed_script())
    _wait_for_related_fixture(companion_url, ha_token)
    yield


def _wait_for_related_fixture(companion_url: str, ha_token: str) -> None:
    headers = {"Authorization": f"Bearer {ha_token}"}
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            r = requests.get(
                f"{companion_url}/v1/related/entity",
                params={"entity_id": "sensor.hactl_related_source"},
                headers=headers,
                timeout=5,
            )
            if r.status_code == 200:
                return
        except requests.ConnectionError:
            pass
        time.sleep(1)
    msg = "Related graph fixture was not visible through companion"
    raise TimeoutError(msg)
