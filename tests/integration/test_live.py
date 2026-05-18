"""Integration tests — live endpoints against real HA Core + companion Docker stack."""

from __future__ import annotations

import requests


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
