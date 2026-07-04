"""Integration tests — live endpoints against real HA Core + companion Docker stack."""

from __future__ import annotations

import subprocess
import time

import requests


class TestStatus:
    """Integration tests for GET /v1/status."""

    def test_status_returns_200(self, companion_url: str) -> None:
        r = requests.get(f"{companion_url}/v1/status", timeout=10)
        assert r.status_code == 200

    def test_status_no_auth_required(self, companion_url: str) -> None:
        """Status is auth-exempt, same as health."""
        r = requests.get(f"{companion_url}/v1/status", timeout=10)
        assert r.status_code == 200

    def test_status_has_required_fields(self, companion_url: str) -> None:
        r = requests.get(f"{companion_url}/v1/status", timeout=10)
        data = r.json()
        for field in (
            "version",
            "supervisor_reachable",
            "has_ha_cli",
            "config_writable",
            "ingress_active",
            "auth_mode",
        ):
            assert field in data, f"missing: {field}"

    def test_status_response_matches_spec_fields(self, companion_url: str) -> None:
        """Response keys must be exactly the fields declared in the OpenAPI spec — no extras, no missing."""
        r = requests.get(f"{companion_url}/v1/status", timeout=10)
        data = r.json()
        expected = {"version", "supervisor_reachable", "has_ha_cli", "config_writable", "ingress_active", "auth_mode"}
        assert set(data.keys()) == expected, f"unexpected keys in /v1/status response: {set(data.keys()) ^ expected}"

    def test_status_supervisor_token_present_in_stack(self, companion_url: str) -> None:
        """Integration stack sets SUPERVISOR_TOKEN, so supervisor_reachable must be True."""
        r = requests.get(f"{companion_url}/v1/status", timeout=10)
        data = r.json()
        assert data["supervisor_reachable"] is True


class TestRoot:
    def test_root_ok(self, companion_url: str, auth_headers: dict[str, str]) -> None:
        r = requests.get(f"{companion_url}/", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        assert "hactl-companion" in r.text

    def test_root_multiple_slashes(self, companion_url: str, auth_headers: dict[str, str]) -> None:
        """Regression: GET //// must not 404 — normalize_path_middleware must fire in Docker."""
        r = requests.get(f"{companion_url}////", headers=auth_headers, timeout=10)
        assert r.status_code == 200


class TestHealth:
    def test_health_ok(self, companion_url: str) -> None:
        r = requests.get(f"{companion_url}/v1/health", timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_health_no_auth_required(self, companion_url: str) -> None:
        r = requests.get(f"{companion_url}/v1/health", timeout=10)
        assert r.status_code == 200


class TestConfigRead:
    """Tests that read /config — requires HA to have written its initial files."""

    def test_list_files(self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None) -> None:
        r = requests.get(f"{companion_url}/v1/config/files", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        files = r.json()["files"]
        assert isinstance(files, list)
        assert len(files) > 0
        # HA Core always creates configuration.yaml
        assert any("configuration" in f for f in files)

    def test_list_files_excludes_secrets(
        self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None
    ) -> None:
        r = requests.get(f"{companion_url}/v1/config/files", headers=auth_headers, timeout=10)
        files = r.json()["files"]
        assert "secrets.yaml" not in files

    def test_read_configuration_yaml(self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None) -> None:
        r = requests.get(
            f"{companion_url}/v1/config/file",
            params={"path": "configuration.yaml"},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["path"] == "configuration.yaml"
        assert len(data["content"]) > 0

    def test_read_nonexistent_file(self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None) -> None:
        r = requests.get(
            f"{companion_url}/v1/config/file",
            params={"path": "does_not_exist.yaml"},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 404

    def test_path_traversal_rejected(self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None) -> None:
        r = requests.get(
            f"{companion_url}/v1/config/file",
            params={"path": "../etc/passwd"},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 400

    def test_secrets_yaml_denied(self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None) -> None:
        r = requests.get(
            f"{companion_url}/v1/config/file",
            params={"path": "secrets.yaml"},
            headers=auth_headers,
            timeout=10,
        )
        # 403 if the file exists, 403 either way (deny-list checked before existence)
        assert r.status_code == 403


class TestConfigWrite:
    """Tests that write to /config via the companion."""

    def test_dry_run_no_changes(self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None) -> None:
        # Read current content
        r = requests.get(
            f"{companion_url}/v1/config/file",
            params={"path": "configuration.yaml"},
            headers=auth_headers,
            timeout=10,
        )
        content = r.json()["content"]

        # Dry-run with same content → empty diff
        r = requests.put(
            f"{companion_url}/v1/config/file",
            params={"path": "configuration.yaml", "dry_run": "true"},
            data=content,
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["status"] == "dry_run"

    def test_write_new_file(self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None) -> None:
        yaml_content = "integration_test:\n  key: value\n"

        r = requests.put(
            f"{companion_url}/v1/config/file",
            params={"path": "test-integration.yaml", "dry_run": "false"},
            data=yaml_content,
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "applied"
        assert "backup" in data

        # Verify the file is now readable
        r = requests.get(
            f"{companion_url}/v1/config/file",
            params={"path": "test-integration.yaml"},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        assert "integration_test" in r.json()["content"]

    def test_write_path_traversal_rejected(
        self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None
    ) -> None:
        r = requests.put(
            f"{companion_url}/v1/config/file",
            params={"path": "../etc/evil.yaml", "dry_run": "false"},
            data="evil: true\n",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 400

    def test_write_invalid_yaml_rejected(
        self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None
    ) -> None:
        r = requests.put(
            f"{companion_url}/v1/config/file",
            params={"path": "bad.yaml", "dry_run": "false"},
            data=": invalid:\n  - :\n  [broken",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 400


class TestHaReload:
    """Integration tests for POST /v1/ha/reload/{domain} against real HA.

    This stack wires CORE_API_URL + a real onboarding-issued token (see
    conftest.py's compose_up) so these calls hit real HA instead of the
    unreachable Supervisor proxy — the 502-on-unreachable-core-API fallback
    itself is covered at the unit level (tests/test_ha.py).
    """

    def test_reload_invalid_domain(self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None) -> None:
        r = requests.post(f"{companion_url}/v1/ha/reload/evil_domain", headers=auth_headers, timeout=10)
        assert r.status_code == 400

    def test_reload_automation_succeeds(
        self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None
    ) -> None:
        r = requests.post(f"{companion_url}/v1/ha/reload/automation", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestAutomationsCRUD:
    """Integration tests for automation CRUD endpoints."""

    def test_create_and_list_automation(
        self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None
    ) -> None:
        # First write an automations.yaml so endpoints work
        automations_yaml = """- id: integ_test_auto_1
  alias: Integration Test Auto
  trigger:
    - platform: time
      at: "12:00:00"
  action:
    - service: light.turn_on
"""
        r = requests.put(
            f"{companion_url}/v1/config/file",
            params={"path": "automations.yaml", "dry_run": "false"},
            data=automations_yaml,
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200

        # List automations
        r = requests.get(f"{companion_url}/v1/config/automations", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert "automations" in data
        assert len(data["automations"]) >= 1

    def test_get_automation_by_id(self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None) -> None:
        r = requests.get(
            f"{companion_url}/v1/config/automation",
            params={"id": "integ_test_auto_1"},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == "integ_test_auto_1"
        assert "content" in data

    def test_create_and_delete_by_alias(
        self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None
    ) -> None:
        """HA derives entity_id from alias, not config id — deleting by the
        display identifier (alias) must still remove the config entry and
        the live entity, not just 404."""
        alias = "Integration Test Alias Delete Case"
        body = f"""id: integ_test_auto_alias_case
alias: {alias}
trigger:
  - platform: time
    at: "13:00:00"
action:
  - service: light.turn_on
"""
        r = requests.post(
            f"{companion_url}/v1/config/automation",
            data=body,
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 201
        data = r.json()
        assert data["id"] == "integ_test_auto_alias_case"
        assert data["reloaded"] is True
        assert data["entity_id"], "expected HA to confirm a live entity_id for the new automation"
        assert data["entity_id"] != data["id"], "HA derives entity_id from alias, not id"

        # Delete by alias — the display identifier — not the config id.
        r = requests.delete(
            f"{companion_url}/v1/config/automation",
            params={"id": alias},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200

        r = requests.get(
            f"{companion_url}/v1/config/automation",
            params={"id": "integ_test_auto_alias_case"},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 404

        r = requests.get(f"{companion_url}/v1/config/automations", headers=auth_headers, timeout=10)
        ids = [a["id"] for a in r.json()["automations"]]
        assert "integ_test_auto_alias_case" not in ids


class TestHelpersCRUD:
    """Integration tests for helper CRUD — asserts real HA entity materialization."""

    def test_create_helper_materializes_live_entity(
        self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None
    ) -> None:
        # HA's default onboarding config doesn't wire up helper domains via
        # YAML — create the backing file, then add the !include ourselves.
        r = requests.put(
            f"{companion_url}/v1/config/file",
            params={"path": "input_boolean.yaml", "dry_run": "false"},
            data="# managed by hactl integration test\n",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200

        r = requests.get(
            f"{companion_url}/v1/config/file",
            params={"path": "configuration.yaml", "resolve": "false"},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        base_config = r.json()["content"]
        if "input_boolean:" not in base_config:
            new_config = base_config.rstrip("\n") + "\ninput_boolean: !include input_boolean.yaml\n"
            r = requests.put(
                f"{companion_url}/v1/config/file",
                params={"path": "configuration.yaml", "dry_run": "false"},
                data=new_config,
                headers=auth_headers,
                timeout=10,
            )
            assert r.status_code == 200

        body = "integ_test_toggle:\n  name: Integration Test Toggle\n"
        r = requests.post(
            f"{companion_url}/v1/config/helper",
            params={"domain": "input_boolean"},
            data=body,
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 201
        data = r.json()
        assert data["id"] == "integ_test_toggle"
        assert data["entity_id"] == "input_boolean.integ_test_toggle"
        assert data["reloaded"] is True
        assert data["entity_created"] is True


class TestScriptsCRUD:
    """Integration tests for script CRUD endpoints."""

    def test_create_and_list_scripts(self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None) -> None:
        scripts_yaml = """integ_test_script:
  alias: Integration Test Script
  mode: single
  sequence:
    - service: light.turn_on
"""
        r = requests.put(
            f"{companion_url}/v1/config/file",
            params={"path": "scripts.yaml", "dry_run": "false"},
            data=scripts_yaml,
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200

        r = requests.get(f"{companion_url}/v1/config/scripts", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert "scripts" in data
        assert len(data["scripts"]) >= 1

    def test_get_script_by_id(self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None) -> None:
        r = requests.get(
            f"{companion_url}/v1/config/script",
            params={"id": "integ_test_script"},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == "integ_test_script"


class TestTemplatesCRUD:
    """Integration tests for template CRUD endpoints."""

    def test_create_and_list_templates(self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None) -> None:
        templates_yaml = """- sensor:
    - name: "Integration Test Sensor"
      unique_id: integ_test_tpl_1
      state: "{{ 42 }}"
"""
        r = requests.put(
            f"{companion_url}/v1/config/file",
            params={"path": "template.yaml", "dry_run": "false"},
            data=templates_yaml,
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200

        r = requests.get(f"{companion_url}/v1/config/templates", headers=auth_headers, timeout=10)
        assert r.status_code == 200
        data = r.json()
        assert "templates" in data
        assert len(data["templates"]) >= 1

    def test_get_template_by_id(self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None) -> None:
        r = requests.get(
            f"{companion_url}/v1/config/template",
            params={"id": "integ_test_tpl_1"},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["unique_id"] == "integ_test_tpl_1"


def _container_logs(container_name: str) -> str:
    result = subprocess.run(
        ["docker", "logs", container_name],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout + result.stderr


class TestStartupLogs:
    """Verify that the companion logs key diagnostic info at startup."""

    def test_version_logged(self, compose_up: dict[str, str]) -> None:
        logs = _container_logs("companion-integration")
        assert "hactl-companion v" in logs, f"version not in startup logs:\n{logs[:500]}"

    def test_supervisor_token_status_logged(self, compose_up: dict[str, str]) -> None:
        logs = _container_logs("companion-integration")
        assert "supervisor token:" in logs, f"supervisor token status not in startup logs:\n{logs[:500]}"
        # In the integration stack SUPERVISOR_TOKEN is set, so it should say "present"
        assert "present" in logs, f"expected 'present' in logs:\n{logs[:500]}"


class TestAccessLogMiddleware:
    """Verify that the access log middleware emits entries for each request."""

    def test_successful_request_logged(
        self, compose_up: dict[str, str], auth_headers: dict[str, str], _ha_ready: None
    ) -> None:
        # Make a request and give the log a moment to flush
        requests.get(
            f"{compose_up['companion_url']}/v1/config/files",
            headers=auth_headers,
            timeout=10,
        )
        time.sleep(0.2)
        logs = _container_logs("companion-integration")
        assert "GET /v1/config/files" in logs, f"access log entry not found:\n{logs[-1000:]}"
        assert "status=200" in logs, f"status=200 not in access logs:\n{logs[-1000:]}"

    def test_auth_mode_bearer_logged(
        self, compose_up: dict[str, str], auth_headers: dict[str, str], _ha_ready: None
    ) -> None:
        requests.get(
            f"{compose_up['companion_url']}/v1/config/files",
            headers=auth_headers,
            timeout=10,
        )
        time.sleep(0.2)
        logs = _container_logs("companion-integration")
        assert "auth=bearer" in logs, f"auth=bearer not in access logs:\n{logs[-1000:]}"

    def test_ingress_bypass_logged(self, compose_up: dict[str, str], _ha_ready: None) -> None:
        requests.get(
            f"{compose_up['companion_url']}/v1/config/files",
            headers={"X-Ingress-Path": "/api/hassio_ingress/test"},
            timeout=10,
        )
        time.sleep(0.2)
        logs = _container_logs("companion-integration")
        assert "auth=ingress" in logs, f"auth=ingress not in access logs:\n{logs[-1000:]}"

    def test_exempt_path_logged_as_none(self, compose_up: dict[str, str]) -> None:
        requests.get(f"{compose_up['companion_url']}/v1/health", timeout=10)
        time.sleep(0.2)
        logs = _container_logs("companion-integration")
        assert "auth=none" in logs, f"auth=none not in access logs:\n{logs[-1000:]}"

    def test_auth_failure_logged_at_warning(self, compose_up: dict[str, str]) -> None:
        requests.get(
            f"{compose_up['companion_url']}/v1/config/files",
            headers={"Authorization": "Bearer wrong-token"},
            timeout=10,
        )
        time.sleep(0.2)
        logs = _container_logs("companion-integration")
        assert "status=401" in logs, f"401 not logged:\n{logs[-1000:]}"
