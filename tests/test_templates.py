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
    backups = list((config_dir / ".hactl_backups").glob("template.yaml.bak.*"))
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


async def test_delete_template(
    client: TestClient, auth_headers: dict[str, str], core_api_calls: list[tuple[str, str]]
) -> None:
    """DELETE should remove the template and trigger a reload."""
    resp = await client.delete("/v1/config/template?id=tpl_avg_temperature", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "deleted"
    assert data["reloaded"] is True
    assert ("template", "reload") in core_api_calls

    # Verify it's gone
    resp2 = await client.get("/v1/config/template?id=tpl_avg_temperature", headers=auth_headers)
    assert resp2.status == 404


async def test_delete_template_not_found(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.delete("/v1/config/template?id=nonexistent", headers=auth_headers)
    assert resp.status == 404


# --- Trigger-based / block-aware create & delete (Option B) ---


def _read_template_yaml(config_dir: Path) -> list:
    from ruamel.yaml import YAML

    y = YAML()
    with open(config_dir / "template.yaml", encoding="utf-8") as f:
        return y.load(f)


async def _post(client: TestClient, auth_headers: dict[str, str], body: str, query: str = ""):
    return await client.post(
        f"/v1/config/template{query}",
        data=body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )


TRIGGER_BLOCK = """\
triggers:
  - trigger: state
    entity_id: sensor.source
sensor:
  - name: "Trig Sensor"
    unique_id: tpl_trig
    state: "{{ trigger.to_state.state }}"
"""


async def test_create_trigger_block(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """A full trigger-based block is appended as its own list item, trigger at block level."""
    resp = await _post(client, auth_headers, TRIGGER_BLOCK)
    assert resp.status == 201
    assert (await resp.json())["unique_id"] == "tpl_trig"

    data = _read_template_yaml(config_dir)
    block = next(b for b in data if "tpl_trig" in [s.get("unique_id") for s in b.get("sensor", [])])
    # trigger lives at the block level ...
    assert "triggers" in block
    # ... and NOT nested inside the entity item (that would be invalid HA config).
    item = block["sensor"][0]
    assert "trigger" not in item and "triggers" not in item


async def test_get_trigger_template_shows_block(client: TestClient, auth_headers: dict[str, str]) -> None:
    """GET on a trigger-based entry returns the whole block and marks trigger=true."""
    await _post(client, auth_headers, TRIGGER_BLOCK)
    resp = await client.get("/v1/config/template?id=tpl_trig", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    assert data["trigger"] is True
    assert "triggers" in data["content"]


async def test_bare_item_not_absorbed_into_trigger_block(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Mode B: creating a plain sensor must not land in an existing trigger block."""
    await _post(client, auth_headers, TRIGGER_BLOCK)
    plain = 'unique_id: tpl_plain\nname: Plain\nstate: "{{ 1 }}"\n'
    resp = await _post(client, auth_headers, plain, "?domain=sensor")
    assert resp.status == 201

    # The new plain sensor must be state-based, not bound to the trigger.
    got = await client.get("/v1/config/template?id=tpl_plain", headers=auth_headers)
    assert (await got.json())["trigger"] is False


async def test_bare_item_with_stray_trigger_rejected(client: TestClient, auth_headers: dict[str, str]) -> None:
    """A bare item carrying a block-level trigger key is rejected, not corrupted."""
    body = 'unique_id: tpl_bad\nstate: "{{ 1 }}"\ntrigger:\n  - platform: state\n    entity_id: sensor.x\n'
    resp = await _post(client, auth_headers, body, "?domain=sensor")
    assert resp.status == 400
    assert "block level" in (await resp.text())


async def test_create_multi_domain_block(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """A block may declare sensor and binary_sensor sharing one trigger set."""
    body = """\
triggers:
  - trigger: webhook
    webhook_id: my_hook
sensor:
  - name: Lat
    unique_id: tpl_lat
    state: "{{ trigger.json.lat }}"
binary_sensor:
  - name: Active
    unique_id: tpl_active
    state: "{{ trigger.json.speed > 0 }}"
"""
    resp = await _post(client, auth_headers, body)
    assert resp.status == 201
    data = _read_template_yaml(config_dir)
    block = next(b for b in data if "tpl_lat" in [s.get("unique_id") for s in b.get("sensor", [])])
    assert "triggers" in block
    assert [s.get("unique_id") for s in block["binary_sensor"]] == ["tpl_active"]


async def test_create_block_unsupported_domain_rejected(client: TestClient, auth_headers: dict[str, str]) -> None:
    """A block declaring a not-yet-supported domain (e.g. number) is refused, not written."""
    body = "number:\n  - name: N\n    unique_id: tpl_num\n    state: '{{ 1 }}'\n    set_value: []\n"
    resp = await _post(client, auth_headers, body)
    assert resp.status == 400
    assert "not yet supported" in (await resp.text())


async def test_create_block_duplicate_unique_id(client: TestClient, auth_headers: dict[str, str]) -> None:
    """A block whose entity collides with an existing unique_id is a 409."""
    body = 'sensor:\n  - name: Dup\n    unique_id: tpl_energie_zaehler\n    state: "{{ 1 }}"\n'
    resp = await _post(client, auth_headers, body)
    assert resp.status == 409


async def test_delete_trigger_block_removes_orphan_trigger(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """Deleting a trigger block's only entity drops the whole block, not an orphan trigger."""
    await _post(client, auth_headers, TRIGGER_BLOCK)
    resp = await client.delete("/v1/config/template?id=tpl_trig", headers=auth_headers)
    assert resp.status == 200
    data = _read_template_yaml(config_dir)
    # No leftover block carrying a trigger with no entities.
    assert not any("triggers" in b or "trigger" in b for b in data)


async def test_put_rejects_full_block(client: TestClient, auth_headers: dict[str, str]) -> None:
    """PUT replaces a single entity; a full block would corrupt it and is rejected."""
    resp = await client.put(
        "/v1/config/template?id=tpl_energie_zaehler&dry_run=false",
        data=TRIGGER_BLOCK,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 400
