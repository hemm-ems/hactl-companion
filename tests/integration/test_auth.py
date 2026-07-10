"""Integration tests — authentication enforcement on the live container."""

from __future__ import annotations

import requests


class TestAuthEnforcement:
    """Verify that auth middleware works correctly on the live container."""

    def test_no_token_returns_401(self, companion_url: str) -> None:
        r = requests.get(f"{companion_url}/v1/config/files", timeout=10)
        assert r.status_code == 401

    def test_wrong_token_returns_401(self, companion_url: str) -> None:
        r = requests.get(
            f"{companion_url}/v1/config/files",
            headers={"Authorization": "Bearer wrong-token"},
            timeout=10,
        )
        assert r.status_code == 401

    def test_valid_token_returns_200(self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None) -> None:
        r = requests.get(f"{companion_url}/v1/config/files", headers=auth_headers, timeout=10)
        assert r.status_code == 200

    def test_spoofed_ingress_header_rejected(self, companion_url: str, _ha_ready: None) -> None:
        """A client-supplied X-Ingress-Path from outside the trusted proxy must not bypass auth.

        The integration stack has no real Supervisor ingress proxy, so a request
        carrying the header from the test runner is a spoof and must be rejected.
        """
        r = requests.get(
            f"{companion_url}/v1/config/files",
            headers={"X-Ingress-Path": "/api/hassio_ingress/fake"},
            timeout=10,
        )
        assert r.status_code == 401

    def test_health_exempt_from_auth(self, companion_url: str) -> None:
        r = requests.get(f"{companion_url}/v1/health", timeout=10)
        assert r.status_code == 200
