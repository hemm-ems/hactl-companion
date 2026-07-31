"""Integration tests — live endpoints against real HA Core + companion Docker stack."""

from __future__ import annotations

import contextlib
import re
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
        # A brand-new file has nothing to back up, so no backup is reported.
        assert "backup" not in data

        # Verify the file is now readable
        r = requests.get(
            f"{companion_url}/v1/config/file",
            params={"path": "test-integration.yaml"},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        assert "integration_test" in r.json()["content"]

        # Overwriting the now-existing file DOES produce a backup.
        r = requests.put(
            f"{companion_url}/v1/config/file",
            params={"path": "test-integration.yaml", "dry_run": "false"},
            data="integration_test:\n  key: value2\n",
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        overwrite = r.json()
        assert overwrite["status"] == "applied"
        assert "backup" in overwrite

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


class TestRefReplace:
    """Real cross-file literal scan + replace against the live /config volume.

    A mock can't prove the file on disk was actually rewritten, so this drives
    the whole path: seed a reference into automations.yaml (already in HA's
    default !include graph), scan for it, dry-run (must not write), then apply
    (must write) — verifying every step by reading the file back through the
    companion. Uses a unique literal so it never collides with other tests.
    """

    STALE = "binary_sensor.refscan_probe_stale"
    FRESH = "binary_sensor.refscan_probe_fresh"
    _SEED = (
        "- id: refscan_probe\n"
        "  alias: refscan probe\n"
        "  trigger:\n"
        "    - platform: state\n"
        f"      entity_id: {STALE}\n"
        "  action:\n"
        '    - delay: "00:00:01"\n'
    )

    def _read(self, companion_url: str, auth_headers: dict[str, str]) -> str:
        r = requests.get(
            f"{companion_url}/v1/config/file",
            params={"path": "automations.yaml", "resolve": "false"},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200, r.text
        return r.json()["content"]

    def _seed(self, companion_url: str, auth_headers: dict[str, str]) -> None:
        r = requests.put(
            f"{companion_url}/v1/config/file",
            params={"path": "automations.yaml", "dry_run": "false"},
            data=self._SEED,
            headers=auth_headers,
            timeout=15,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "applied"

    def test_scan_dry_run_then_apply(self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None) -> None:
        self._seed(companion_url, auth_headers)

        # Scan finds the literal in the file it actually lives in.
        r = requests.get(
            f"{companion_url}/v1/ref/scan",
            params={"target": self.STALE},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200, r.text
        hits = r.json()["hits"]
        assert {"location": "automations.yaml", "path": "[0].trigger[0].entity_id", "matched_value": self.STALE} in hits

        # Dry-run reports the change but must not touch the file.
        r = requests.post(
            f"{companion_url}/v1/ref/replace",
            json={"old": self.STALE, "new": self.FRESH, "dry_run": True},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "dry_run"
        assert {
            "location": "automations.yaml",
            "path": "[0].trigger[0].entity_id",
            "before": self.STALE,
            "after": self.FRESH,
        } in body["changes"]
        assert self.STALE in self._read(companion_url, auth_headers)

        # Apply actually rewrites the on-disk file.
        r = requests.post(
            f"{companion_url}/v1/ref/replace",
            json={"old": self.STALE, "new": self.FRESH, "dry_run": False},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "applied"

        after = self._read(companion_url, auth_headers)
        assert self.FRESH in after
        assert self.STALE not in after

    def test_entities_enumerates_seeded_reference(
        self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None
    ) -> None:
        # Self-contained: re-seed so this passes regardless of test order.
        self._seed(companion_url, auth_headers)

        r = requests.get(f"{companion_url}/v1/ref/entities", headers=auth_headers, timeout=10)
        assert r.status_code == 200, r.text
        values = {e["matched_value"] for e in r.json()["entities"]}
        assert self.STALE in values, f"{self.STALE} not enumerated; got {sorted(values)}"


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

    def test_create_non_sensor_block_full_crud(
        self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None, ha_url: str, ha_token: str
    ) -> None:
        """A non-sensor domain (number) block: create -> HA-validated -> get/list -> delete.

        Proves Option C end-to-end against real HA: the block (with a verbatim
        ``set_value`` action template) is accepted by HA as valid config, and the
        domain-agnostic extraction makes the number entity visible to get/list/delete.

        Note: the companion's per-write ``template.reload`` reports ``reloaded=False``
        in this harness only because HA boots without any ``template:`` config, so
        the ``template.reload`` service is never registered (and can't be reloaded
        into existence without a restart) — a test-stack quirk, identical for the
        existing sensor path, not a real-world issue. HA validity is therefore
        asserted via ``check_config`` rather than the reload flag.
        """
        ha_hdr = {"Authorization": f"Bearer {ha_token}"}
        # Wire template.yaml into HA's config (default onboarding doesn't) so
        # check_config actually validates the file we write.
        seed = '- sensor:\n    - name: Seed\n      unique_id: integ_seed_tpl\n      state: "{{ 1 }}"\n'
        r = requests.put(
            f"{companion_url}/v1/config/file",
            params={"path": "template.yaml", "dry_run": "false"},
            data=seed,
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        base_config = requests.get(
            f"{companion_url}/v1/config/file",
            params={"path": "configuration.yaml", "resolve": "false"},
            headers=auth_headers,
            timeout=10,
        ).json()["content"]
        if "template:" not in base_config:
            new_config = base_config.rstrip("\n") + "\ntemplate: !include template.yaml\n"
            r = requests.put(
                f"{companion_url}/v1/config/file",
                params={"path": "configuration.yaml", "dry_run": "false"},
                data=new_config,
                headers=auth_headers,
                timeout=10,
            )
            assert r.status_code == 200

        number_block = """number:
  - name: "Integration Test Number"
    unique_id: integ_test_num
    state: "{{ 5 }}"
    set_value:
      - service: input_number.set_value
        target: {entity_id: input_number.integ_backing}
        data: {value: "{{ value }}"}
"""
        r = requests.post(
            f"{companion_url}/v1/config/template",
            data=number_block,
            headers={**auth_headers, "Content-Type": "text/plain"},
            timeout=15,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["unique_id"] == "integ_test_num"
        assert "reloaded" in body  # field returned; value is harness-dependent (see docstring)

        # HA accepts the number template (verbatim action template included).
        chk = requests.post(f"{ha_url}/api/config/core/check_config", headers=ha_hdr, json={}, timeout=30)
        assert chk.status_code == 200, chk.text
        assert chk.json()["result"] == "valid", chk.text

        # Domain-agnostic extraction: the number entity is visible to get + list.
        got = requests.get(
            f"{companion_url}/v1/config/template",
            params={"id": "integ_test_num"},
            headers=auth_headers,
            timeout=10,
        )
        assert got.status_code == 200
        assert "set_value" in got.json()["content"]
        listed = requests.get(f"{companion_url}/v1/config/templates", headers=auth_headers, timeout=10).json()
        by_uid = {t["unique_id"]: t["domain"] for t in listed["templates"]}
        assert by_uid.get("integ_test_num") == "number"

        # Delete addressing works for a non-sensor domain.
        deleted = requests.delete(
            f"{companion_url}/v1/config/template",
            params={"id": "integ_test_num"},
            headers=auth_headers,
            timeout=15,
        )
        assert deleted.status_code == 200
        gone = requests.get(
            f"{companion_url}/v1/config/template",
            params={"id": "integ_test_num"},
            headers=auth_headers,
            timeout=10,
        )
        assert gone.status_code == 404


# --- C-13: a create never joins a block it did not write --------------------

# One user-owned block: a good sensor and, beside it, an entry HA will reject.
ISOLATION_USER_BLOCK = """\
- sensor:
    - name: Hactl Iso Neighbour
      unique_id: hactl_iso_neighbour
      state: "{{ 11 }}"
    - name: Hactl Iso Poison
      unique_id: hactl_iso_poison
      state: "{{ 1 }}"
      device_class: not_a_real_device_class
"""
ISOLATION_CREATED_ITEM = 'name: Hactl Iso Created\nunique_id: hactl_iso_created\nstate: "{{ 42 }}"\n'
ISOLATION_NEIGHBOUR = "sensor.hactl_iso_neighbour"
ISOLATION_CREATED = "sensor.hactl_iso_created"


def _restart_ha_core(ha_url: str, ha_token: str, settle_entity: str, settle_state: str, timeout: int = 300) -> None:
    """Restart HA Core in place, then wait for ``settle_entity`` to render.

    In place — the service call, not ``docker restart``: recreating the
    container re-randomises the published host port (measured: 54897 → 54920),
    and every later test in the session resolves HA through the session-scoped
    ``ha_url`` that was computed once, at compose time.

    A restart, rather than ``template.reload``, because a freshly onboarded HA
    has no ``template:`` configured, so the service does not exist yet and
    cannot be reloaded into existence. That is also what makes the wait
    self-synchronising: no template entity can exist until HA has come back up
    having read template.yaml, so the pre-restart process cannot satisfy this
    poll. Measured at ~6s locally.
    """
    hdr = {"Authorization": f"Bearer {ha_token}"}
    r = requests.post(f"{ha_url}/api/services/homeassistant/restart", headers=hdr, json={}, timeout=90)
    assert r.status_code == 200, f"homeassistant.restart failed: {r.status_code} {r.text}"

    deadline = time.monotonic() + timeout
    last: str | None = None
    while time.monotonic() < deadline:
        try:
            s = requests.get(f"{ha_url}/api/states/{settle_entity}", headers=hdr, timeout=5)
            if s.status_code == 200:
                last = s.json()["state"]
                if last == settle_state:
                    return
        except requests.RequestException:
            pass
        time.sleep(2)
    raise AssertionError(f"{settle_entity} never reached {settle_state!r} after the restart (last seen: {last!r})")


class TestTemplateBlockIsolation:
    """C-13, with HA supplying both halves: the hazard and the protection."""

    def test_a_created_entry_survives_a_poisoned_neighbouring_block(
        self, companion_url: str, auth_headers: dict[str, str], ha_url: str, ha_token: str, _ha_ready: None
    ) -> None:
        """The create's placement decision, measured where it actually matters.

        HA's unit of rejection is the whole top-level list item: one entity that
        fails validation takes its valid siblings with it. So this seeds a
        user-owned ``- sensor:`` block holding a good sensor *and* a bad one,
        creates an entry through the route, and asks HA what survived.

        Against the old behaviour the created entry was appended into that first
        existing block and went down with it — which is the live-fire P1: a bad
        payload could dark production sensors it had merely been filed next to.
        Against the current behaviour it is in a list item of its own and comes
        up, which simultaneously proves the premise the fix rests on (HA is
        perfectly happy with several top-level items declaring the same domain).

        The neighbour is asserted *dead* on purpose. Without it the test would
        still pass if HA had quietly started tolerating the bad entry, and it
        would be proving nothing at all.
        """
        original = _read_config_file(companion_url, auth_headers, "configuration.yaml")
        assert original is not None
        try:
            # File first, include second: wiring an `!include` at a file that is
            # not there yet fails HA's check_config, and C-6 then rolls the
            # configuration.yaml write back — which made this test pass only
            # when an earlier test happened to have created template.yaml.
            r = _write_config_file(companion_url, auth_headers, "template.yaml", ISOLATION_USER_BLOCK)
            assert r.status_code == 200, r.text

            if "template:" not in original:
                wired = original.rstrip("\n") + "\ntemplate: !include template.yaml\n"
                assert _write_config_file(companion_url, auth_headers, "configuration.yaml", wired).status_code == 200

            created = requests.post(
                f"{companion_url}/v1/config/template",
                params={"domain": "sensor"},
                data=ISOLATION_CREATED_ITEM,
                headers={**auth_headers, "Content-Type": "text/plain"},
                timeout=60,
            )
            assert created.status_code == 201, created.text

            # The user's bytes, unchanged and still first — the create appended.
            after = _read_config_file(companion_url, auth_headers, "template.yaml")
            assert after is not None
            assert after.startswith(ISOLATION_USER_BLOCK), (
                f"the create rewrote the user's block instead of appending after it:\n{after}"
            )
            assert "hactl_iso_created" in after

            # Recorded, not incidental: HA's config check does NOT see this.
            # A pre-write validity gate built on check_config would wave through
            # exactly the payload that darks a block, which is why there isn't
            # one. If this ever starts failing, HA has become able to answer the
            # question early and the gate becomes worth building.
            chk = requests.post(
                f"{ha_url}/api/config/core/check_config",
                headers={"Authorization": f"Bearer {ha_token}"},
                json={},
                timeout=120,
            )
            assert chk.status_code == 200, chk.text
            assert chk.json()["result"] == "valid", (
                "check_config now reports the invalid template entity — a pre-write gate has become "
                f"feasible and should be reconsidered: {chk.text}"
            )

            _restart_ha_core(ha_url, ha_token, ISOLATION_CREATED, "42")

            assert _entity_is_live(ha_url, ha_token, ISOLATION_CREATED), (
                "the created entry did not come up — either it was filed into the poisoned block, or HA "
                "has stopped accepting several top-level items for one domain (the premise of the fix)"
            )
            assert not _entity_is_live(ha_url, ha_token, ISOLATION_NEIGHBOUR), (
                f"{ISOLATION_NEIGHBOUR} is loaded despite an invalid sibling in its block — HA no longer "
                "drops the whole top-level item, so this test can no longer show what it claims to show"
            )
        finally:
            _write_config_file(
                companion_url,
                auth_headers,
                "template.yaml",
                '- sensor:\n    - name: Iso Cleanup\n      unique_id: hactl_iso_cleanup\n      state: "{{ 1 }}"\n',
            )
            _write_config_file(companion_url, auth_headers, "configuration.yaml", original)


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

    def test_spoofed_ingress_logged_as_bearer(self, compose_up: dict[str, str], _ha_ready: None) -> None:
        """A spoofed X-Ingress-Path from an untrusted source is treated as bearer auth and rejected.

        auth_mode reflects the decision actually taken, not the presence of the
        (client-controlled) header, so this request logs auth=bearer / status=401.
        """
        r = requests.get(
            f"{compose_up['companion_url']}/v1/config/files",
            headers={"X-Ingress-Path": "/api/hassio_ingress/test"},
            timeout=10,
        )
        assert r.status_code == 401
        time.sleep(0.2)
        logs = _container_logs("companion-integration")
        assert "auth=bearer" in logs, f"auth=bearer not in access logs:\n{logs[-1000:]}"

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


# ---------------------------------------------------------------------------
# C-10 / C-11 — include wiring and unknown include tags, against the live HA
# ---------------------------------------------------------------------------

WIRING_AUTOMATION_FILE = "automations_wiring_probe.yaml"
WIRING_AUTOMATION = "- id: hactl_wiring_probe\n  alias: Hactl Wiring Probe\n  trigger: []\n  action: []\n"
WIRING_ENTITY = "automation.hactl_wiring_probe"


def _read_config_file(companion_url: str, auth_headers: dict[str, str], path: str) -> str | None:
    """Raw (unresolved) content of a config file, or None if it does not exist."""
    r = requests.get(
        f"{companion_url}/v1/config/file",
        params={"path": path, "resolve": "false"},
        headers=auth_headers,
        timeout=15,
    )
    if r.status_code == 404:
        return None
    assert r.status_code == 200, r.text
    return str(r.json()["content"])


def _write_config_file(companion_url: str, auth_headers: dict[str, str], path: str, content: str) -> requests.Response:
    return requests.put(
        f"{companion_url}/v1/config/file",
        params={"path": path, "dry_run": "false"},
        data=content,
        headers=auth_headers,
        timeout=60,
    )


def _strip_domain_keys(config_text: str, domain: str) -> str:
    """Remove every top-level `<domain>:` / `<domain> <label>:` line.

    Mirrors HA's own extract_domain_configs matching, so the resulting config is
    one where HA genuinely does not read the domain's file — not one where we
    merely removed the spelling we happened to think of.
    """
    key = re.compile(rf"^{re.escape(domain)}(| .+):")
    return "".join(line for line in config_text.splitlines(keepends=True) if not key.match(line))


def _entity_is_live(ha_url: str, ha_token: str, entity_id: str) -> bool:
    """Ask HA whether it currently has this entity loaded from config.

    Entity *presence* is not the answer. HA keeps a dropped entity in the entity
    registry and serves it back as a restored ghost — `state: unavailable` with
    a `restored: true` attribute — so a presence check reports "still there" for
    something HA has actually dropped. Which of the two shapes a dropped entity
    takes (absent, or ghost) depends only on whether it was ever registered
    before, so neither may count as loaded.
    """
    r = requests.get(f"{ha_url}/api/states/{entity_id}", headers={"Authorization": f"Bearer {ha_token}"}, timeout=15)
    if r.status_code != 200:
        return False
    state = r.json()
    return state["state"] != "unavailable" and not state.get("attributes", {}).get("restored")


def _automation_is_loaded(ha_url: str, ha_token: str, entity_id: str) -> bool:
    """Whether HA currently has this automation loaded (see :func:`_entity_is_live`)."""
    return _entity_is_live(ha_url, ha_token, entity_id)


def _reload_automations(ha_url: str, ha_token: str) -> None:
    r = requests.post(
        f"{ha_url}/api/services/automation/reload",
        headers={"Authorization": f"Bearer {ha_token}"},
        json={},
        timeout=90,
    )
    assert r.status_code == 200, f"automation.reload failed: {r.status_code} {r.text}"
    time.sleep(2)


class TestIncludeWiring:
    """C-10: HA reads a file because configuration.yaml includes it — never because of its name."""

    def test_labelled_domain_key_is_live_config(
        self, companion_url: str, auth_headers: dict[str, str], ha_url: str, ha_token: str, _ha_ready: None
    ) -> None:
        """Both halves of D46, with HA as the oracle and the backing file never touched.

        The same bytes sit in `automations_wiring_probe.yaml` throughout. Only
        the `!include` in configuration.yaml changes, and HA's answer changes
        with it: the automation is loaded while the key is there and dropped
        when it is gone. That is the whole premise of the wiring guard, measured
        instead of assumed.

        It also settles the guard's one non-obvious modelling choice — that
        `automation <label>:` is real configuration (HA's
        `extract_domain_configs` matches `^<domain>(| .+)$`), which is why
        `wiring.domain_keys` accepts it. A guard that refused a labelled key
        would break a documented split-automation layout.
        """
        original = _read_config_file(companion_url, auth_headers, "configuration.yaml")
        assert original is not None
        assert (
            _write_config_file(companion_url, auth_headers, WIRING_AUTOMATION_FILE, WIRING_AUTOMATION).status_code
            == 200
        )

        try:
            labelled = original.rstrip("\n") + f"\nautomation hactlprobe: !include {WIRING_AUTOMATION_FILE}\n"
            r = _write_config_file(companion_url, auth_headers, "configuration.yaml", labelled)
            assert r.status_code == 200, f"HA rejected a labelled domain key: {r.text}"
            _reload_automations(ha_url, ha_token)
            assert _automation_is_loaded(ha_url, ha_token, WIRING_ENTITY), (
                f"HA did not load {WIRING_ENTITY} from 'automation hactlprobe: !include ...' — if HA no longer "
                "honours labelled domain keys, wiring.domain_keys must stop accepting them"
            )

            before = _read_config_file(companion_url, auth_headers, WIRING_AUTOMATION_FILE)
            assert _write_config_file(companion_url, auth_headers, "configuration.yaml", original).status_code == 200
            _reload_automations(ha_url, ha_token)
            after = _read_config_file(companion_url, auth_headers, WIRING_AUTOMATION_FILE)

            assert before == after, "the backing file changed; this test only proves anything if it did not"
            assert not _automation_is_loaded(ha_url, ha_token, WIRING_ENTITY), (
                f"HA still has {WIRING_ENTITY} loaded with no '{WIRING_AUTOMATION_FILE}' include in "
                "configuration.yaml — the file, not the include, would then be what makes HA read it, and "
                "the whole wiring guard would be unnecessary"
            )
        finally:
            _write_config_file(companion_url, auth_headers, "configuration.yaml", original)
            _reload_automations(ha_url, ha_token)

    def test_create_refuses_until_the_include_exists(
        self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None
    ) -> None:
        """D46 end to end on a real instance, in both directions.

        HA's default onboarding configuration.yaml wires `automation:`,
        `script:` and `scene:` and nothing else — so a stock instance is exactly
        the D46 case for `template:`, and this route used to answer 201 for an
        entity that could never appear.
        """
        original = _read_config_file(companion_url, auth_headers, "configuration.yaml")
        assert original is not None
        seed = '- sensor:\n    - name: Wiring Seed\n      unique_id: hactl_wiring_seed\n      state: "{{ 1 }}"\n'
        assert _write_config_file(companion_url, auth_headers, "template.yaml", seed).status_code == 200

        body = 'name: "Wiring Probe"\nunique_id: hactl_wiring_probe_tpl\nstate: "{{ 1 }}"\n'
        post_kwargs = {
            "params": {"domain": "sensor"},
            "data": body,
            "headers": {**auth_headers, "Content-Type": "text/plain"},
            "timeout": 30,
        }

        try:
            unwired = _strip_domain_keys(original, "template")
            assert _write_config_file(companion_url, auth_headers, "configuration.yaml", unwired).status_code == 200

            before = _read_config_file(companion_url, auth_headers, "template.yaml")
            r = requests.post(f"{companion_url}/v1/config/template", **post_kwargs)  # type: ignore[arg-type]
            assert r.status_code == 400, (
                f"answered {r.status_code} with no 'template:' key in configuration.yaml — HA never reads "
                f"template.yaml on this instance, so the entity could not appear (D46): {r.text}"
            )
            assert "template" in r.json()["error"]["message"]
            assert _read_config_file(companion_url, auth_headers, "template.yaml") == before, (
                "refused the create but wrote to template.yaml anyway"
            )

            wired = unwired.rstrip("\n") + "\ntemplate: !include template.yaml\n"
            assert _write_config_file(companion_url, auth_headers, "configuration.yaml", wired).status_code == 200
            r = requests.post(f"{companion_url}/v1/config/template", **post_kwargs)  # type: ignore[arg-type]
            assert r.status_code == 201, f"guard refused a properly wired instance: {r.text}"
            assert r.json()["unique_id"] == "hactl_wiring_probe_tpl"
        finally:
            requests.delete(
                f"{companion_url}/v1/config/template",
                params={"id": "hactl_wiring_probe_tpl"},
                headers=auth_headers,
                timeout=30,
            )
            _write_config_file(companion_url, auth_headers, "configuration.yaml", original)

    def test_home_assistant_refuses_any_tag_outside_its_vocabulary(
        self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None
    ) -> None:
        """The premise behind C-11's tag enumeration, asked of HA rather than assumed.

        `yaml_resolver.INCLUDE_TAGS | PRESERVED_TAGS` is claimed to be HA's
        entire YAML vocabulary. If that is true, HA must refuse to load anything
        else — which makes an unknown tag a *forward*-compatibility signal (an
        HA newer than this build) rather than an exotic user config, and that is
        precisely why resolving it to nothing would be so convincing and so
        wrong.

        The write goes through `PUT /v1/config/file`, which applies, asks HA's
        check_config, and rolls back on invalid (C-6) — so HA's verdict comes
        back in the refusal and the file is left as it was.
        """
        original = _read_config_file(companion_url, auth_headers, "configuration.yaml")
        assert original is not None

        for tag in ("!my_custom_thing", "!include_dir_merge_flat"):
            r = _write_config_file(
                companion_url, auth_headers, "configuration.yaml", original.rstrip("\n") + f"\nprobe_key: {tag} x\n"
            )
            assert r.status_code == 400, f"HA accepted the unknown tag {tag}: {r.status_code} {r.text}"
            assert "could not determine a constructor" in r.text, (
                f"HA refused {tag} for some other reason than an unknown tag: {r.text}"
            )
            assert _read_config_file(companion_url, auth_headers, "configuration.yaml") == original, (
                "C-6 rollback did not restore configuration.yaml"
            )

    def test_unknown_include_tag_is_refused_by_a_live_route(
        self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None
    ) -> None:
        """C-11 through the whole stack: 400 with the tag named, and the file still readable.

        The file is not referenced from configuration.yaml, so HA never parses
        it and this is the one shape an unknown tag can legitimately have on
        disk today.
        """
        assert (
            _write_config_file(
                companion_url, auth_headers, "probe_unknown_tag.yaml", "thing: !include_dir_merge_flat somewhere\n"
            ).status_code
            == 200
        )

        r = requests.get(
            f"{companion_url}/v1/config/file",
            params={"path": "probe_unknown_tag.yaml", "resolve": "true"},
            headers=auth_headers,
            timeout=15,
        )
        assert r.status_code == 400, f"resolved an unimplemented include tag instead of refusing: {r.text}"
        assert "!include_dir_merge_flat" in r.json()["error"]["message"]

        r = requests.get(
            f"{companion_url}/v1/config/file",
            params={"path": "probe_unknown_tag.yaml", "resolve": "false"},
            headers=auth_headers,
            timeout=15,
        )
        assert r.status_code == 200, "the escape hatch must stay open for exactly the file we refuse to resolve"
        assert "!include_dir_merge_flat" in r.json()["content"]


# ---------------------------------------------------------------------------
# C-14: a single-entry write rewrites only that entry's bytes
# ---------------------------------------------------------------------------

# A hand-maintained file: a header comment, a comment that introduces the entry
# below it, long unwrapped Jinja lines and non-default sequence indentation.
# Every one of those is something a whole-document re-serialization rewrites.
SURGICAL_AUTOMATIONS = """\
# Hand-maintained automations. A tool must not eat this comment.
- id: surgical_first
  alias: Surgical First
  trigger:
      - platform: state
        entity_id: input_boolean.surgical_probe
  action:
      - service: persistent_notification.create
        data:
          message: JansPos:{{ states.input_number.posclock_jan.state }}:{{states.input_number.posclock_speed.state|int}}
  mode: single

# The middle entry is the one every write below names.
- id: surgical_middle
  alias: Surgical Middle
  trigger: []
  action: []
  mode: single

- id: surgical_last
  alias: Surgical Last
  description: |-
    Alle Phasen mit water=high und fan=high;
    zweiter Absatz bleibt erhalten.
  trigger: []
  action: []
  mode: single
# dangling comment at end of file
"""

_UNTOUCHED_LINES = (
    "# Hand-maintained automations. A tool must not eat this comment.\n",
    "          message: JansPos:{{ states.input_number.posclock_jan.state }}:"
    "{{states.input_number.posclock_speed.state|int}}\n",
    "      - platform: state\n",
    "  description: |-\n",
    "# dangling comment at end of file\n",
)


class TestSurgicalWrites:
    """C-14 against a real Home Assistant, and the boundary of what it can cover."""

    def test_single_entry_write_keeps_every_other_byte_and_ha_still_loads_the_file(
        self, companion_url: str, auth_headers: dict[str, str], ha_url: str, ha_token: str, _ha_ready: None
    ) -> None:
        """One automation updated; the other two byte-identical; HA reads the result.

        Both halves are the test. Byte preservation alone would be satisfied by a
        writer that produces a file Home Assistant refuses — so HA is asked
        whether the spliced file is still valid configuration and whether the
        edited automation is actually live, rather than being trusted to be.

        The unit tier proves the splice against fixtures; this proves the file it
        produces is one HA accepts, which no amount of in-process testing can.
        """
        original = _read_config_file(companion_url, auth_headers, "automations.yaml")
        try:
            assert (
                _write_config_file(companion_url, auth_headers, "automations.yaml", SURGICAL_AUTOMATIONS).status_code
                == 200
            )
            before = _read_config_file(companion_url, auth_headers, "automations.yaml")
            assert before == SURGICAL_AUTOMATIONS

            r = requests.put(
                f"{companion_url}/v1/config/automation",
                params={"id": "surgical_middle", "dry_run": "false"},
                data="id: surgical_middle\nalias: Surgical Middle Renamed\ntrigger: []\naction: []\nmode: single\n",
                headers=auth_headers,
                timeout=60,
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert "reformatted" not in body, f"fell back to a whole-file re-serialization: {body}"
            assert body["reloaded"] is True, body

            after = _read_config_file(companion_url, auth_headers, "automations.yaml")
            assert after is not None
            assert "Surgical Middle Renamed" in after
            for line in _UNTOUCHED_LINES:
                assert line in after, f"a line outside the edited entry was rewritten: {line!r}"

            # HA's own verdict on the file this write produced.
            check = requests.post(
                f"{ha_url}/api/config/core/check_config",
                headers={"Authorization": f"Bearer {ha_token}"},
                timeout=90,
            )
            assert check.status_code == 200, check.text
            assert check.json()["result"] == "valid", check.json()
            _reload_automations(ha_url, ha_token)
            assert _automation_is_loaded(ha_url, ha_token, "automation.surgical_middle_renamed"), (
                "HA did not load the automation from the spliced file"
            )
        finally:
            if original is not None:
                _write_config_file(companion_url, auth_headers, "automations.yaml", original)

    def test_home_assistants_own_config_api_reserializes_the_whole_file(
        self, companion_url: str, auth_headers: dict[str, str], ha_url: str, ha_token: str, _ha_ready: None
    ) -> None:
        """The boundary: HA's `/api/config/automation/config/<id>` rewrites everything.

        This is not a defect in this service and it is not fixable here — it is
        recorded because it decides where the fix can reach. `hactl auto apply`
        and `auto rollback` post to this endpoint rather than to
        `PUT /v1/config/automation`, so they keep reformatting whole files until
        hactl is changed to route through the companion. Asserting HA's behaviour
        instead of describing it means the day HA stops doing this, the claim
        fails instead of quietly becoming folklore.
        """
        original = _read_config_file(companion_url, auth_headers, "automations.yaml")
        try:
            assert (
                _write_config_file(companion_url, auth_headers, "automations.yaml", SURGICAL_AUTOMATIONS).status_code
                == 200
            )

            r = requests.post(
                f"{ha_url}/api/config/automation/config/surgical_middle",
                headers={"Authorization": f"Bearer {ha_token}"},
                json={"alias": "Surgical Middle Via HA", "trigger": [], "action": [], "mode": "single"},
                timeout=60,
            )
            assert r.status_code == 200, r.text

            after = _read_config_file(companion_url, auth_headers, "automations.yaml")
            assert after is not None
            assert "Surgical Middle Via HA" in after, "HA did not apply the edit — the comparison below is vacuous"
            survivors = [line for line in _UNTOUCHED_LINES if line in after]
            assert not survivors, (
                "HA's config API preserved lines outside the edited entry — the premise of the note in "
                f"INVARIANTS.md C-14 no longer holds, survivors: {survivors}"
            )
        finally:
            if original is not None:
                _write_config_file(companion_url, auth_headers, "automations.yaml", original)


# ---------------------------------------------------------------------------
# Storage-backed helpers and the create-layout probe, against the live HA
# ---------------------------------------------------------------------------

#: Minimal `<domain>/create` payloads for every helper domain this service can
#: read. Only the fields HA requires — the point is what HA writes back, not
#: what we send.
UI_HELPER_CREATES: dict[str, dict[str, object]] = {
    "input_boolean": {"name": "Live Bool", "icon": "mdi:toggle-switch"},
    "input_number": {"name": "Live Number", "min": 0, "max": 100},
    "input_select": {"name": "Live Select", "options": ["a", "b"]},
    "input_text": {"name": "Live Text"},
    "input_datetime": {"name": "Live Datetime", "has_date": True, "has_time": True},
    "input_button": {"name": "Live Button"},
    "counter": {"name": "Live Counter"},
    "timer": {"name": "Live Timer", "duration": "00:05:00"},
    "schedule": {"name": "Live Schedule", "monday": [{"from": "08:00:00", "to": "17:00:00"}]},
}


def _wait_for_helper(companion_url: str, auth_headers: dict[str, str], helper_id: str, timeout: int = 60):
    """Poll GET /v1/config/helper until it answers 200, or fail with the last body.

    HA persists a collection change to `.storage` on a delay, so a helper made a
    moment ago is genuinely not on disk yet. That freshness window is a property
    of HA, not of this lookup; the retry is here so the test measures the lookup.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = requests.get(
            f"{companion_url}/v1/config/helper", params={"id": helper_id}, headers=auth_headers, timeout=15
        )
        if last.status_code == 200:
            return last
        time.sleep(2)
    raise AssertionError(
        f"{helper_id} never became readable: {last.status_code if last else '-'} {last.text if last else ''}"
    )


class TestStorageHelpers:
    """A helper made the normal way — in HA's UI — must be readable here.

    On a UI-managed instance every helper is storage-backed, so a lookup that
    only searched the YAML files answered "Helper not found" for 100% of them
    while the listing beside it enumerated them all.
    """

    def test_ui_created_helpers_are_readable_in_every_domain(
        self,
        companion_url: str,
        auth_headers: dict[str, str],
        ha_ws_command,
        ha_url: str,
        ha_token: str,
        _ha_ready: None,
    ) -> None:
        """HA creates them, HA names them, and this service must find every one.

        The expected entity_id and definition come from HA's own create result
        and its live state at test time — never from a fixture, which could be
        wrong in the same direction as the code that reads it.
        """
        created: dict[str, dict] = {}
        try:
            for domain, payload in UI_HELPER_CREATES.items():
                created[domain] = ha_ws_command(f"{domain}/create", **payload)

            for domain, item in created.items():
                entity_id = f"{domain}.{item['id']}"
                state = requests.get(
                    f"{ha_url}/api/states/{entity_id}",
                    headers={"Authorization": f"Bearer {ha_token}"},
                    timeout=15,
                )
                assert state.status_code == 200, f"HA did not materialise {entity_id}: {state.text}"

                resp = _wait_for_helper(companion_url, auth_headers, entity_id)
                body = resp.json()
                assert body["id"] == entity_id
                assert body["domain"] == domain
                assert body["source"] == "storage", f"{entity_id} reported source={body['source']}"
                assert str(item["name"]) in body["content"], (
                    f"{entity_id}: definition does not carry the name HA reports: {body['content']}"
                )

                bare = requests.get(
                    f"{companion_url}/v1/config/helper",
                    params={"id": item["id"]},
                    headers=auth_headers,
                    timeout=15,
                )
                assert bare.status_code == 200, f"collection id {item['id']} did not resolve: {bare.text}"
        finally:
            for domain, item in created.items():
                # Cleanup is best-effort: a helper that never appeared must not
                # replace the real failure with a teardown error.
                with contextlib.suppress(AssertionError):
                    ha_ws_command(f"{domain}/delete", **{f"{domain}_id": item["id"]})

    def test_the_write_half_explains_instead_of_denying(
        self, companion_url: str, auth_headers: dict[str, str], ha_ws_command, _ha_ready: None
    ) -> None:
        """DELETE cannot act on a UI helper — and must say why, now that GET finds it.

        hactl resolves a delete target through GET before printing its plan, so a
        GET that resolves storage helpers while DELETE answers a bare 404 would
        turn a correct refusal into a confident plan for an impossible delete.
        """
        item = ha_ws_command("input_boolean/create", name="Live Refusal Probe")
        entity_id = f"input_boolean.{item['id']}"
        try:
            _wait_for_helper(companion_url, auth_headers, entity_id)
            r = requests.delete(
                f"{companion_url}/v1/config/helper", params={"id": entity_id}, headers=auth_headers, timeout=30
            )
            assert r.status_code == 409, f"answered {r.status_code}: {r.text}"
            assert "storage" in r.json()["error"]["message"]

            still = requests.get(
                f"{companion_url}/v1/config/helper", params={"id": entity_id}, headers=auth_headers, timeout=15
            )
            assert still.status_code == 200, "the refused delete removed the helper anyway"
        finally:
            ha_ws_command("input_boolean/delete", input_boolean_id=item["id"])


class TestWiringProbeAgreesWithTheCreate:
    """H-2 across the wire: what the probe predicts is what the create does.

    The unit tier sweeps this over every create route with a synthetic config
    dir. This is the layout that produced the defect on a real instance —
    `input_boolean:` written inline in configuration.yaml, which HA accepts and
    loads, so nothing short of the actual wiring resolution can tell that a
    create is impossible.
    """

    def test_inline_and_included_layouts_predict_their_own_create(
        self, companion_url: str, auth_headers: dict[str, str], _ha_ready: None
    ) -> None:
        original = _read_config_file(companion_url, auth_headers, "configuration.yaml")
        assert original is not None
        stripped = _strip_domain_keys(original, "input_boolean")

        def probe() -> dict:
            r = requests.get(
                f"{companion_url}/v1/config/wiring",
                params={"domain": "input_boolean"},
                headers=auth_headers,
                timeout=15,
            )
            assert r.status_code == 200, r.text
            return dict(r.json())

        def create(helper_id: str) -> requests.Response:
            return requests.post(
                f"{companion_url}/v1/config/helper",
                params={"domain": "input_boolean"},
                data=f"{helper_id}:\n  name: Wiring Parity Probe\n",
                headers={**auth_headers, "Content-Type": "text/plain"},
                timeout=30,
            )

        try:
            inline = stripped.rstrip("\n") + "\ninput_boolean:\n  live_inline_flag:\n    name: Live Inline\n"
            assert _write_config_file(companion_url, auth_headers, "configuration.yaml", inline).status_code == 200, (
                "HA rejected an inline input_boolean mapping — then this is not the layout the real instance has"
            )
            verdict = probe()
            attempt = create("parity_probe_inline")
            assert verdict["wired"] is False, f"probe called an inline layout wired: {verdict}"
            assert attempt.status_code == 400, f"create succeeded on an inline layout: {attempt.text}"
            assert verdict["reason"] == attempt.json()["error"]["message"], (
                "probe and create refuse the same layout with different explanations"
            )

            assert (
                _write_config_file(companion_url, auth_headers, "input_boolean.yaml", "# parity probe\n").status_code
                == 200
            )
            wired = stripped.rstrip("\n") + "\ninput_boolean: !include input_boolean.yaml\n"
            assert _write_config_file(companion_url, auth_headers, "configuration.yaml", wired).status_code == 200
            verdict = probe()
            attempt = create("parity_probe_included")
            assert verdict["wired"] is True, f"probe called a wired layout unwired: {verdict}"
            assert attempt.status_code == 201, f"create refused a wired layout: {attempt.text}"
            assert verdict["file"] == "input_boolean.yaml", verdict
        finally:
            requests.delete(
                f"{companion_url}/v1/config/helper",
                params={"id": "parity_probe_included"},
                headers=auth_headers,
                timeout=30,
            )
            _write_config_file(companion_url, auth_headers, "configuration.yaml", original)
