"""Integration tests for WireGuard VPN client — real Docker WG tunnel."""

from __future__ import annotations

import json
import subprocess
import time

import pytest
import requests

WG_COMPOSE_FILE = "docker-compose.wireguard.yaml"
WG_TOKEN = "wg-test-token-12345"


# ---------------------------------------------------------------------------
# Docker Compose lifecycle
# ---------------------------------------------------------------------------


def _compose(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "compose", "-f", WG_COMPOSE_FILE, *args]
    return subprocess.run(cmd, capture_output=capture, text=True, check=True, timeout=360)


def _get_mapped_port(service: str, container_port: int) -> str:
    result = _compose("port", service, str(container_port), capture=True)
    return result.stdout.strip().rsplit(":", maxsplit=1)[-1]


def _wait_for_url(url: str, timeout: int = 60) -> None:
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


def _wait_for_ready(timeout: int = 60) -> None:
    """Wait for the wg-server to write the /shared/ready marker."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["docker", "exec", "wg-server", "test", "-f", "/shared/ready"],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                return
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        time.sleep(1)
    msg = "wg-server /shared/ready marker not found"
    raise TimeoutError(msg)


def _read_from_container(container: str, path: str) -> str:
    result = subprocess.run(
        ["docker", "exec", container, "cat", path],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return result.stdout


@pytest.fixture(scope="module")
def wg_stack():
    """Start the WG integration stack and yield URLs, then tear down."""
    _compose("up", "-d", "--build")
    try:
        _wait_for_ready()
        comp_port = _get_mapped_port("companion-wg", 9100)
        companion_url = f"http://localhost:{comp_port}"
        _wait_for_url(f"{companion_url}/v1/health")
        yield {"companion_url": companion_url}
    finally:
        _compose("down", "-v")


@pytest.fixture(scope="module")
def companion_url(wg_stack: dict[str, str]) -> str:
    return wg_stack["companion_url"]


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {WG_TOKEN}"}


@pytest.fixture(scope="module")
def client_conf(wg_stack: dict[str, str]) -> str:
    """Read the auto-generated client.conf from wg-server."""
    return _read_from_container("wg-server", "/shared/client.conf")


@pytest.fixture(scope="module")
def client_json(wg_stack: dict[str, str]) -> dict[str, object]:
    """Read the auto-generated client.json from wg-server."""
    raw = _read_from_container("wg-server", "/shared/client.json")
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Tests — executed in order via pytest-ordering or class grouping
# ---------------------------------------------------------------------------


class TestWireGuardFlow:
    """End-to-end WireGuard tunnel flow: config → start → status → ping → stop."""

    def test_01_health(self, companion_url: str) -> None:
        """Companion is reachable."""
        r = requests.get(f"{companion_url}/v1/health", timeout=10)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_02_status_before_config(self, companion_url: str, auth_headers: dict[str, str]) -> None:
        """Status returns inactive when no tunnel exists."""
        r = requests.get(f"{companion_url}/v1/wireguard/status", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        assert r.json()["state"] == "inactive"

    def test_03_config_raw(self, companion_url: str, auth_headers: dict[str, str], client_conf: str) -> None:
        """Push raw .conf content."""
        r = requests.post(
            f"{companion_url}/v1/wireguard/config?tunnel=wg0",
            data=client_conf,
            headers={**auth_headers, "Content-Type": "text/plain"},
            timeout=10,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "configured"
        assert body["tunnel"] == "wg0"

    def test_04_config_invalid_tunnel(self, companion_url: str, auth_headers: dict[str, str]) -> None:
        """Reject invalid tunnel names."""
        r = requests.post(
            f"{companion_url}/v1/wireguard/config?tunnel=../etc",
            data=b"[Interface]\nPrivateKey=X\n[Peer]\nPublicKey=Y\n",
            headers={**auth_headers, "Content-Type": "text/plain"},
            timeout=10,
        )
        assert r.status_code == 400

    def test_05_start(self, companion_url: str, auth_headers: dict[str, str]) -> None:
        """Start the tunnel."""
        r = requests.post(
            f"{companion_url}/v1/wireguard/start?tunnel=wg0",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "started"
        assert body["tunnel"] == "wg0"

    def test_06_start_already_up(self, companion_url: str, auth_headers: dict[str, str]) -> None:
        """Starting an already-active tunnel returns 409."""
        r = requests.post(
            f"{companion_url}/v1/wireguard/start?tunnel=wg0",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 409

    def test_07_status_active(self, companion_url: str, auth_headers: dict[str, str]) -> None:
        """Status shows active with peer info."""
        # Give WG a moment to establish handshake
        time.sleep(3)
        r = requests.get(
            f"{companion_url}/v1/wireguard/status?tunnel=wg0",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "active"
        assert body["tunnel"] == "wg0"
        assert "public_key" in body.get("interface", {})
        assert len(body.get("peers", [])) >= 1

    def test_08_ping_through_tunnel(self) -> None:
        """Verify actual connectivity: ping the WG server through the tunnel."""
        result = subprocess.run(
            ["docker", "exec", "companion-wg", "ping", "-c", "2", "-W", "5", "10.13.13.1"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, f"Ping failed: {result.stderr}"

    def test_09_stop(self, companion_url: str, auth_headers: dict[str, str]) -> None:
        """Stop the tunnel."""
        r = requests.post(
            f"{companion_url}/v1/wireguard/stop?tunnel=wg0",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "stopped"

    def test_10_status_after_stop(self, companion_url: str, auth_headers: dict[str, str]) -> None:
        """Status shows inactive after stop."""
        r = requests.get(
            f"{companion_url}/v1/wireguard/status?tunnel=wg0",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["state"] == "inactive"

    def test_11_ping_after_stop(self) -> None:
        """Ping should fail after tunnel is down."""
        result = subprocess.run(
            ["docker", "exec", "companion-wg", "ping", "-c", "1", "-W", "3", "10.13.13.1"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode != 0, "Ping should fail after tunnel stop"

    def test_12_config_json(
        self, companion_url: str, auth_headers: dict[str, str], client_json: dict[str, object]
    ) -> None:
        """Push JSON-format config."""
        r = requests.post(
            f"{companion_url}/v1/wireguard/config",
            json=client_json,
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "configured"

    def test_13_restart_after_json_config(self, companion_url: str, auth_headers: dict[str, str]) -> None:
        """Start tunnel again with JSON-configured config, verify connectivity."""
        r = requests.post(
            f"{companion_url}/v1/wireguard/start?tunnel=wg0",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 200

        time.sleep(3)
        result = subprocess.run(
            ["docker", "exec", "companion-wg", "ping", "-c", "2", "-W", "5", "10.13.13.1"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, f"Ping through JSON-configured tunnel failed: {result.stderr}"

        # Clean up
        r = requests.post(
            f"{companion_url}/v1/wireguard/stop?tunnel=wg0",
            headers=auth_headers,
            timeout=30,
        )
        assert r.status_code == 200

    def test_14_auth_required(self, companion_url: str) -> None:
        """All wireguard endpoints require auth."""
        for endpoint in ["/v1/wireguard/config", "/v1/wireguard/start", "/v1/wireguard/stop"]:
            r = requests.post(f"{companion_url}{endpoint}", timeout=10)
            assert r.status_code == 401, f"{endpoint} should require auth"
        r = requests.get(f"{companion_url}/v1/wireguard/status", timeout=10)
        assert r.status_code == 401
