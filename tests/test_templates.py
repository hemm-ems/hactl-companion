"""Tests for template sensor CRUD endpoints (Phase 3)."""

from __future__ import annotations

from pathlib import Path

from aiohttp.test_utils import TestClient


async def test_list_templates(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Should list all template sensor definitions."""
    resp = await client.get("/v1/config/templates", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    templates = data["templates"]
    assert len(templates) == 3
    uids = [t["unique_id"] for t in templates]
    assert "tpl_energie_zaehler" in uids
    assert "tpl_avg_temperature" in uids
    assert "tpl_wohnzimmer_motion" in uids


async def test_list_templates_domains(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Template list should include correct domains."""
    resp = await client.get("/v1/config/templates", headers=auth_headers)
    data = await resp.json()
    domains = {t["unique_id"]: t["domain"] for t in data["templates"]}
    assert domains["tpl_energie_zaehler"] == "sensor"
    assert domains["tpl_wohnzimmer_motion"] == "binary_sensor"


async def test_get_template_by_id(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Should return a single template by unique_id."""
    resp = await client.get("/v1/config/template?id=tpl_energie_zaehler", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    assert data["unique_id"] == "tpl_energie_zaehler"
    assert "content" in data
    assert "Energie" in data["content"]


async def test_get_template_not_found(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.get("/v1/config/template?id=nonexistent", headers=auth_headers)
    assert resp.status == 404


async def test_get_template_missing_id(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.get("/v1/config/template", headers=auth_headers)
    assert resp.status == 400


async def test_update_template_dry_run(client: TestClient, auth_headers: dict[str, str]) -> None:
    """PUT with dry_run=true should return diff without modifying."""
    new_body = """name: "Updated Sensor"
unique_id: tpl_energie_zaehler
unit_of_measurement: "kWh"
state: "{{ 42 }}"
"""
    resp = await client.put(
        "/v1/config/template?id=tpl_energie_zaehler&dry_run=true",
        data=new_body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "dry_run"
    assert "diff" in data


async def test_update_template_apply(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """PUT with dry_run=false should update the template and create backup."""
    new_body = """name: "Updated Sensor"
unique_id: tpl_energie_zaehler
unit_of_measurement: "kWh"
state: "{{ 42 }}"
"""
    resp = await client.put(
        "/v1/config/template?id=tpl_energie_zaehler&dry_run=false",
        data=new_body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "applied"

    # Verify backup exists
    backups = list(config_dir.glob("template.yaml.bak.*"))
    assert len(backups) >= 1

    # Verify updated content
    resp2 = await client.get("/v1/config/template?id=tpl_energie_zaehler", headers=auth_headers)
    data2 = await resp2.json()
    assert "Updated Sensor" in data2["content"]


async def test_create_template(client: TestClient, auth_headers: dict[str, str]) -> None:
    """POST should create a new template sensor."""
    new_body = """name: "New Sensor"
unique_id: tpl_new_sensor
state: "{{ 123 }}"
"""
    resp = await client.post(
        "/v1/config/template?domain=sensor",
        data=new_body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 201
    data = await resp.json()
    assert data["status"] == "created"
    assert data["unique_id"] == "tpl_new_sensor"


async def test_create_template_duplicate(client: TestClient, auth_headers: dict[str, str]) -> None:
    """POST with existing unique_id should return 409."""
    body = """name: "Duplicate"
unique_id: tpl_energie_zaehler
state: "{{ 0 }}"
"""
    resp = await client.post(
        "/v1/config/template?domain=sensor",
        data=body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 409


async def test_create_template_missing_unique_id(client: TestClient, auth_headers: dict[str, str]) -> None:
    body = """name: "No ID"
state: "{{ 0 }}"
"""
    resp = await client.post(
        "/v1/config/template?domain=sensor",
        data=body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 400


async def test_delete_template(client: TestClient, auth_headers: dict[str, str]) -> None:
    """DELETE should remove the template."""
    resp = await client.delete("/v1/config/template?id=tpl_avg_temperature", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "deleted"

    # Verify it's gone
    resp2 = await client.get("/v1/config/template?id=tpl_avg_temperature", headers=auth_headers)
    assert resp2.status == 404


async def test_delete_template_not_found(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.delete("/v1/config/template?id=nonexistent", headers=auth_headers)
    assert resp.status == 404
