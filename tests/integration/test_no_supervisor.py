"""Integration tests — endpoints that need the HA core API (should return 502 gracefully without a Supervisor)."""

from __future__ import annotations

import requests


class TestCoreApi502:
    """Core-API-backed endpoints should return 502 when no Supervisor proxy is reachable."""

    def test_reload_automation(self, companion_url: str, auth_headers: dict[str, str]) -> None:
        r = requests.post(f"{companion_url}/v1/ha/reload/automation", headers=auth_headers, timeout=10)
        assert r.status_code == 502
        assert "Reload failed" in r.text

    def test_reload_script(self, companion_url: str, auth_headers: dict[str, str]) -> None:
        r = requests.post(f"{companion_url}/v1/ha/reload/script", headers=auth_headers, timeout=10)
        assert r.status_code == 502

    def test_reload_invalid_domain(self, companion_url: str, auth_headers: dict[str, str]) -> None:
        r = requests.post(f"{companion_url}/v1/ha/reload/evil_domain", headers=auth_headers, timeout=10)
        assert r.status_code == 400

    def test_check_config(self, companion_url: str, auth_headers: dict[str, str]) -> None:
        r = requests.post(f"{companion_url}/v1/ha/check-config", headers=auth_headers, timeout=10)
        assert r.status_code == 502
