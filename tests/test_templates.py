"""Tests for template sensor CRUD endpoints (Phase 3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient

from companion import core_api


def _fake_states(monkeypatch: pytest.MonkeyPatch, states: list[dict[str, Any]]) -> None:
    """Make `core_api.get_states()` answer with `states` — the shape
    `post_template`'s entity verification polls (matched by
    ``(domain, attributes.friendly_name)``; see `_poll_template_entity`).
    """

    async def _fake_get_states() -> list[dict[str, Any]]:
        return states

    monkeypatch.setattr(core_api, "get_states", _fake_get_states)


def _live_state(entity_id: str, name: str) -> dict[str, Any]:
    return {"entity_id": entity_id, "state": "steady", "attributes": {"friendly_name": name}}


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
    # Unlike helper/script, a template entity_id is derived from `name`, not
    # from `unique_id` (this file's own fixture proves it: unique_id
    # tpl_energie_zaehler names a sensor called "Energie Zählerstand Flur") —
    # so it cannot be predicted, only found among live states after the fact
    # (see `_poll_template_entity`). Without a matching live state (none faked
    # here — the default fixture answers `get_states()` with `[]`) the entity
    # cannot be confirmed, and the response has to say so honestly rather than
    # assume success because the write and the reload both answered 2xx.
    assert data["entities"] == [
        {"unique_id": "tpl_new_sensor", "domain": "sensor", "entity_id": None, "created": False}
    ]


async def test_create_template_entity_confirmed_via_live_state(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once a live state matches `(domain, name)`, the create reports the entity
    as actually created — this is the positive case `test_create_template`
    deliberately leaves unresolved."""
    _fake_states(monkeypatch, [_live_state("sensor.tpl_state_confirmed", "Confirm Me")])
    new_body = 'name: "Confirm Me"\nunique_id: tpl_confirm\nstate: "{{ 1 }}"\n'
    resp = await client.post(
        "/v1/config/template?domain=sensor",
        data=new_body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 201
    data = await resp.json()
    assert data["entities"] == [
        {
            "unique_id": "tpl_confirm",
            "domain": "sensor",
            "entity_id": "sensor.tpl_state_confirmed",
            "created": True,
        }
    ]


async def test_create_template_entity_ambiguous_name_is_not_confirmed(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """HA does not enforce `friendly_name` uniqueness — two live states matching
    `(domain, name)` must not be guessed between, only refused as unresolvable."""
    _fake_states(
        monkeypatch,
        [
            _live_state("sensor.tpl_ambiguous_one", "Ambiguous"),
            _live_state("sensor.tpl_ambiguous_two", "Ambiguous"),
        ],
    )
    new_body = 'name: "Ambiguous"\nunique_id: tpl_ambiguous\nstate: "{{ 1 }}"\n'
    resp = await client.post(
        "/v1/config/template?domain=sensor",
        data=new_body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 201
    data = await resp.json()
    assert data["entities"] == [{"unique_id": "tpl_ambiguous", "domain": "sensor", "entity_id": None, "created": False}]


async def test_create_template_block_reports_every_declared_entity(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full block may declare several unique_ids across several domains sharing
    one trigger — every one of them gets its own entry, not a single collapsed
    boolean."""
    _fake_states(
        monkeypatch,
        [
            _live_state("sensor.block_a", "Block A"),
            _live_state("binary_sensor.block_b", "Block B"),
        ],
    )
    block_body = """triggers:
  - trigger: state
    entity_id: sensor.gone
sensor:
  - name: "Block A"
    unique_id: tpl_block_a
    state: "{{ 1 }}"
binary_sensor:
  - name: "Block B"
    unique_id: tpl_block_b
    state: "{{ true }}"
"""
    resp = await client.post(
        "/v1/config/template",
        data=block_body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 201
    data = await resp.json()
    entities = {e["unique_id"]: e for e in data["entities"]}
    assert entities.keys() == {"tpl_block_a", "tpl_block_b"}
    assert entities["tpl_block_a"] == {
        "unique_id": "tpl_block_a",
        "domain": "sensor",
        "entity_id": "sensor.block_a",
        "created": True,
    }
    assert entities["tpl_block_b"] == {
        "unique_id": "tpl_block_b",
        "domain": "binary_sensor",
        "entity_id": "binary_sensor.block_b",
        "created": True,
    }


async def test_create_template_block_partial_confirmation_is_reported_per_entity(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-entity shape does not assume every declared entity rises or falls
    together — if live states (or a future HA) disagree between two entities
    from the same create, that disagreement must survive to the response."""
    _fake_states(
        monkeypatch,
        [_live_state("sensor.partial_up", "Partial Up")],
        # "Partial Down" deliberately gets no matching live state.
    )
    block_body = """sensor:
  - name: "Partial Up"
    unique_id: tpl_partial_up
    state: "{{ 1 }}"
  - name: "Partial Down"
    unique_id: tpl_partial_down
    state: "{{ 2 }}"
"""
    resp = await client.post(
        "/v1/config/template",
        data=block_body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 201
    data = await resp.json()
    entities = {e["unique_id"]: e for e in data["entities"]}
    assert entities["tpl_partial_up"]["created"] is True
    assert entities["tpl_partial_down"] == {
        "unique_id": "tpl_partial_down",
        "domain": "sensor",
        "entity_id": None,
        "created": False,
    }


async def test_create_template_entities_false_when_reload_fails(
    client: TestClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """HA refusing the reload must not be papered over by a live state left
    behind from a previous, unrelated create sharing the same name."""
    _fake_states(monkeypatch, [_live_state("sensor.tpl_reload_fails", "Reload Fails")])

    async def _refused(domain: str, service: str, data: object = None) -> core_api.ServiceResult:
        return core_api.ServiceResult(False, "HTTP 400: Service not found")

    monkeypatch.setattr(core_api, "call_service", _refused)

    new_body = 'name: "Reload Fails"\nunique_id: tpl_reload_fails\nstate: "{{ 1 }}"\n'
    resp = await client.post(
        "/v1/config/template?domain=sensor",
        data=new_body,
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 201
    data = await resp.json()
    assert data["reloaded"] is False
    assert data["entities"] == [
        {"unique_id": "tpl_reload_fails", "domain": "sensor", "entity_id": None, "created": False}
    ]


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


async def test_create_number_block_full_crud(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """A non-sensor domain (number) supports the full create -> get -> list -> delete cycle.

    The block carries a ``set_value`` action template, which is appended verbatim
    (hactl never models per-domain action templates).
    """
    body = (
        "number:\n"
        "  - name: N\n"
        "    unique_id: tpl_num\n"
        '    state: "{{ 1 }}"\n'
        "    set_value:\n"
        "      - action: input_number.set_value\n"
        "        target: {entity_id: input_number.backing}\n"
        '        data: {value: "{{ value }}"}\n'
    )
    resp = await _post(client, auth_headers, body)
    assert resp.status == 201
    assert (await resp.json())["unique_id"] == "tpl_num"

    # Written verbatim, action template preserved.
    data = _read_template_yaml(config_dir)
    block = next(b for b in data if "number" in b)
    assert block["number"][0]["unique_id"] == "tpl_num"
    assert "set_value" in block["number"][0]

    # Visible to get/list with the right domain ...
    got = await client.get("/v1/config/template?id=tpl_num", headers=auth_headers)
    assert got.status == 200
    assert "set_value" in (await got.json())["content"]
    listed = await (await client.get("/v1/config/templates", headers=auth_headers)).json()
    assert {"tpl_num": "number"}.items() <= {t["unique_id"]: t["domain"] for t in listed["templates"]}.items()

    # ... and deletable.
    deleted = await client.delete("/v1/config/template?id=tpl_num", headers=auth_headers)
    assert deleted.status == 200
    assert not any("number" in b for b in _read_template_yaml(config_dir))


async def test_create_select_and_button_blocks(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Other action-driven domains (select, button) create and are readable by unique_id."""
    select_block = (
        "select:\n"
        "  - name: Mode\n"
        "    unique_id: tpl_mode\n"
        "    state: \"{{ 'a' }}\"\n"
        "    options: \"{{ ['a', 'b'] }}\"\n"
        "    select_option:\n"
        "      - action: input_select.select_option\n"
        "        target: {entity_id: input_select.backing}\n"
        '        data: {option: "{{ option }}"}\n'
    )
    button_block = (
        "button:\n"
        "  - name: Ping\n"
        "    unique_id: tpl_ping\n"
        "    press:\n"
        "      - action: homeassistant.update_entity\n"
        "        target: {entity_id: sensor.x}\n"
    )
    for body, uid in ((select_block, "tpl_mode"), (button_block, "tpl_ping")):
        resp = await _post(client, auth_headers, body)
        assert resp.status == 201, await resp.text()
        assert (await resp.json())["unique_id"] == uid
        got = await client.get(f"/v1/config/template?id={uid}", headers=auth_headers)
        assert got.status == 200
        assert (await got.json())["trigger"] is False


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


# --- C-13: a create never joins a block it did not write --------------------
#
# HA's unit of rejection is the whole top-level list item: one invalid entity
# takes its valid siblings out of the state machine (`unavailable`,
# `restored: true`) — measured against a live HA, see `_create_bare_item`. So
# every entry gets its own item, and a pre-existing block must come back
# byte-identical.

PLAIN_SENSOR = 'unique_id: tpl_isolated\nname: Isolated\nstate: "{{ 1 }}"\n'


def _template_bytes(config_dir: Path) -> bytes:
    return (config_dir / "template.yaml").read_bytes()


def _added_lines(before: bytes, after: bytes) -> list[str]:
    """The lines `after` adds — assert-fails if it changed or dropped any."""
    import difflib

    diff = list(
        difflib.unified_diff(
            before.decode().splitlines(keepends=True),
            after.decode().splitlines(keepends=True),
            n=0,
        )
    )
    removed = [ln for ln in diff if ln.startswith("-") and not ln.startswith("---")]
    assert not removed, f"pre-existing bytes were rewritten, not just appended to:\n{''.join(diff)}"
    return [ln[1:] for ln in diff if ln.startswith("+") and not ln.startswith("+++")]


async def test_bare_item_gets_its_own_block(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """The new entity is alone in a new top-level item; the user's block is unchanged."""
    before = _read_template_yaml(config_dir)
    first_block_uids = [s["unique_id"] for s in before[0]["sensor"]]
    assert len(first_block_uids) == 2, "fixture must have a multi-entity first sensor block for this to mean anything"

    resp = await _post(client, auth_headers, PLAIN_SENSOR, "?domain=sensor")
    assert resp.status == 201

    after = _read_template_yaml(config_dir)
    assert [s["unique_id"] for s in after[0]["sensor"]] == first_block_uids, (
        "the new entity was merged into the user's existing sensor block — one bad entry there "
        "would dark every entity beside it"
    )
    owning = [b for b in after if "tpl_isolated" in [s.get("unique_id") for s in b.get("sensor", [])]]
    assert len(owning) == 1
    assert owning[0] == {"sensor": [{"unique_id": "tpl_isolated", "name": "Isolated", "state": "{{ 1 }}"}]}, (
        f"the new block carries something besides the new entity: {owning[0]}"
    )
    assert len(after) == len(before) + 1


async def test_bare_item_create_only_appends_bytes(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """C-5's sibling: everything the caller did not write comes back byte-identical."""
    before = _template_bytes(config_dir)
    resp = await _post(client, auth_headers, PLAIN_SENSOR, "?domain=sensor")
    assert resp.status == 201
    added = "".join(_added_lines(before, _template_bytes(config_dir)))
    assert "tpl_isolated" in added
    assert "tpl_energie_zaehler" not in added, "an untouched entity was re-emitted"


async def test_two_bare_items_of_one_domain_do_not_share_a_block(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """Not even two hactl-created entries share an item — the blast radius stays 1."""
    assert (await _post(client, auth_headers, PLAIN_SENSOR, "?domain=sensor")).status == 201
    second = 'unique_id: tpl_isolated2\nname: Isolated 2\nstate: "{{ 2 }}"\n'
    assert (await _post(client, auth_headers, second, "?domain=sensor")).status == 201

    data = _read_template_yaml(config_dir)
    owners = {
        uid: [i for i, b in enumerate(data) if uid in [s.get("unique_id") for s in b.get("sensor", [])]]
        for uid in ("tpl_isolated", "tpl_isolated2")
    }
    assert all(len(idx) == 1 for idx in owners.values()), owners
    assert owners["tpl_isolated"] != owners["tpl_isolated2"], f"both entries landed in one block: {owners}"


async def test_full_block_create_only_appends_bytes(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """The block shape appends too — the create path re-emits nothing it did not write."""
    before = _template_bytes(config_dir)
    assert (await _post(client, auth_headers, TRIGGER_BLOCK)).status == 201
    added = "".join(_added_lines(before, _template_bytes(config_dir)))
    assert "tpl_trig" in added
    assert "tpl_wohnzimmer_motion" not in added, "an untouched entity was re-emitted"


async def test_create_falls_back_to_a_whole_file_write_on_a_layout_the_splice_cannot_cover(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """A flow-style top-level list has no entry span to splice — it must still create.

    C-13 (whose block) and C-14 (how few bytes) meet here: the entry still goes
    into its own item, and the write falls back to the whole-file dump, which
    C-14 requires to announce itself rather than pass for a surgical write.
    """
    (config_dir / "template.yaml").write_text(
        '[{sensor: [{unique_id: tpl_flow, name: Flow, state: "{{ 1 }}"}]}]\n', encoding="utf-8"
    )
    resp = await _post(client, auth_headers, PLAIN_SENSOR, "?domain=sensor")
    assert resp.status == 201, await resp.text()
    assert (await resp.json()).get("reformatted") is True, "the fallback must not pass for a surgical write"

    data = _read_template_yaml(config_dir)
    assert [next(iter(b["sensor"]))["unique_id"] for b in data] == ["tpl_flow", "tpl_isolated"]


async def test_bare_item_leaves_a_trigger_block_byte_identical(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """The pre-existing corruption trap, now closed structurally rather than by a skip rule."""
    assert (await _post(client, auth_headers, TRIGGER_BLOCK)).status == 201
    before = _template_bytes(config_dir)

    assert (await _post(client, auth_headers, PLAIN_SENSOR, "?domain=sensor")).status == 201

    added = "".join(_added_lines(before, _template_bytes(config_dir)))
    assert "tpl_isolated" in added
    assert "trigger" not in added, "the plain entity was written next to (or under) a trigger"
    trig_block = next(b for b in _read_template_yaml(config_dir) if "triggers" in b)
    assert [s["unique_id"] for s in trig_block["sensor"]] == ["tpl_trig"]
