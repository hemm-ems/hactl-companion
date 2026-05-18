"""Tests for automation CRUD endpoints (Phase 5)."""

from __future__ import annotations

from pathlib import Path

from aiohttp.test_utils import TestClient


async def test_list_automations(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Should list all automation definitions."""
    resp = await client.get("/v1/config/automations", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    automations = data["automations"]
    assert len(automations) == 3
    ids = [a["id"] for a in automations]
    assert "automation.door_light" in ids
    assert "automation.morning_coffee" in ids
    assert "automation.sunset_lights" in ids


async def test_list_automations_metadata(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Automation list should include alias and description."""
    resp = await client.get("/v1/config/automations", headers=auth_headers)
    data = await resp.json()
    sunset = next(a for a in data["automations"] if a["id"] == "automation.sunset_lights")
    assert sunset["alias"] == "Turn on lights at sunset"
    assert "sunset" in sunset["description"].lower()


async def test_get_automation_by_id(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Should return a single automation by id."""
    resp = await client.get("/v1/config/automation?id=automation.door_light", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    assert data["id"] == "automation.door_light"
    assert "Door Light" in data["content"]


async def test_get_automation_not_found(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.get("/v1/config/automation?id=nonexistent", headers=auth_headers)
    assert resp.status == 404


async def test_get_automation_missing_id(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.get("/v1/config/automation", headers=auth_headers)
    assert resp.status == 400


async def test_update_automation_dry_run(client: TestClient, auth_headers: dict[str, str]) -> None:
    """PUT with dry_run=true should return diff."""
    new_body = """id: automation.door_light
alias: Door Light Updated
trigger:
  - platform: state
    entity_id: binary_sensor.front_door
    to: "on"
action:
  - service: light.turn_on
    target:
      entity_id: light.hallway_new
"""
    resp = await client.put(
        "/v1/config/automation?id=automation.door_light&dry_run=true",
        data=new_body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "dry_run"
    assert "diff" in data


async def test_update_automation_apply(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """PUT with dry_run=false should update and create backup."""
    new_body = """id: automation.door_light
alias: Door Light Updated
trigger:
  - platform: state
    entity_id: binary_sensor.front_door
    to: "on"
action:
  - service: light.turn_on
    target:
      entity_id: light.hallway_new
"""
    resp = await client.put(
        "/v1/config/automation?id=automation.door_light&dry_run=false",
        data=new_body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "applied"

    # Verify backup
    backups = list(config_dir.glob("automations.yaml.bak.*"))
    assert len(backups) >= 1

    # Verify updated
    resp2 = await client.get("/v1/config/automation?id=automation.door_light", headers=auth_headers)
    data2 = await resp2.json()
    assert "Updated" in data2["content"]


async def test_create_automation(client: TestClient, auth_headers: dict[str, str]) -> None:
    """POST should create a new automation."""
    new_body = """id: automation.new_test
alias: New Test Automation
trigger:
  - platform: time
    at: "12:00:00"
action:
  - service: notify.mobile_app
    data:
      message: "Noon!"
"""
    resp = await client.post(
        "/v1/config/automation",
        data=new_body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 201
    data = await resp.json()
    assert data["id"] == "automation.new_test"


async def test_create_automation_duplicate(client: TestClient, auth_headers: dict[str, str]) -> None:
    body = """id: automation.door_light
alias: Duplicate
trigger: []
action: []
"""
    resp = await client.post(
        "/v1/config/automation",
        data=body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 409


async def test_create_automation_missing_id(client: TestClient, auth_headers: dict[str, str]) -> None:
    body = """alias: No ID
trigger: []
action: []
"""
    resp = await client.post(
        "/v1/config/automation",
        data=body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 400


async def test_delete_automation(client: TestClient, auth_headers: dict[str, str]) -> None:
    """DELETE should remove the automation."""
    resp = await client.delete("/v1/config/automation?id=automation.sunset_lights", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "deleted"

    resp2 = await client.get("/v1/config/automation?id=automation.sunset_lights", headers=auth_headers)
    assert resp2.status == 404


async def test_delete_automation_not_found(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.delete("/v1/config/automation?id=nonexistent", headers=auth_headers)
    assert resp.status == 404
