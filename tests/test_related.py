"""Tests for the generic related-entity graph endpoint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient

from companion.routes.related import RelatedItem
from tests.related_fixture import (
    EMBEDDED_ENTITY_ID,
    GENERATED_CONFIG_ENTRY_ID,
    GENERATED_ENTITY_ID,
    SOURCE_ENTITY_ID,
    UNKNOWN_ENTITY_ID,
    YAML_PEER_ENTITY_ID,
    seed_related_fixture,
)

# Realistic automation YAML, in the modern (2024.10+) triggers/conditions/actions
# dialect a UI-authored automations.yaml actually uses. Deliberately shaped so
# each reference kind is isolated:
#   * "Pumpe steuern" references sensor.fuellstand ONLY from inside a Jinja
#     template — an exact-string matcher can never see it;
#   * both automations reference switch.pumpe structurally, via target.entity_id
#     — which an entity<->entity co-occurrence graph can see but cannot attribute
#     to the automation that does the referencing.
# The first automation is in the entity registry (unique_id == its yaml `id`),
# the second is not, so the fallback naming path is exercised too.
REALISTIC_AUTOMATIONS = """\
- id: '1699999999999'
  alias: Pumpe steuern
  description: ''
  triggers:
    - trigger: time_pattern
      minutes: /5
  conditions:
    - condition: template
      value_template: "{{ states('sensor.fuellstand') | float(0) < 20 }}"
  actions:
    - action: switch.turn_on
      target:
        entity_id: switch.pumpe
  mode: single

- id: '1688888888888'
  alias: Nur Schalter
  triggers:
    - trigger: state
      entity_id: binary_sensor.regen
      to: 'on'
  actions:
    - action: switch.turn_off
      target:
        entity_id: switch.pumpe
  mode: single
"""

PUMPE_AUTOMATION_ENTITY_ID = "automation.pumpe_steuern"
# Not in the registry snapshot: named from its alias, the way HA itself derives
# an automation entity_id when the automation is first loaded.
SCHALTER_AUTOMATION_ENTITY_ID = "automation.nur_schalter"


def _write_registry(config_dir: Path, entities: list[dict[str, Any]]) -> None:
    """Write a minimal core.entity_registry containing exactly ``entities``."""
    storage = config_dir / ".storage"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "core.entity_registry").write_text(
        json.dumps({"version": 1, "minor_version": 1, "key": "core.entity_registry", "data": {"entities": entities}}),
        encoding="utf-8",
    )


def _seed_realistic_automations(config_dir: Path, *, split_dir: bool = False) -> None:
    """Seed a realistic automation config plus the registry entries it needs."""
    if split_dir:
        (config_dir / "automations.yaml").unlink(missing_ok=True)
        autos = config_dir / "automations"
        autos.mkdir(exist_ok=True)
        blocks = REALISTIC_AUTOMATIONS.split("\n\n")
        (autos / "pumpe.yaml").write_text(blocks[0] + "\n", encoding="utf-8")
        (autos / "schalter.yaml").write_text(blocks[1], encoding="utf-8")
        config = (config_dir / "configuration.yaml").read_text(encoding="utf-8")
        (config_dir / "configuration.yaml").write_text(
            config.replace("automation: !include automations.yaml", "automation: !include_dir_merge_list automations"),
            encoding="utf-8",
        )
    else:
        (config_dir / "automations.yaml").write_text(REALISTIC_AUTOMATIONS, encoding="utf-8")

    _write_registry(
        config_dir,
        [
            {"entity_id": "sensor.fuellstand", "platform": "mqtt", "unique_id": "fuellstand"},
            {"entity_id": "switch.pumpe", "platform": "mqtt", "unique_id": "pumpe"},
            {"entity_id": "binary_sensor.regen", "platform": "mqtt", "unique_id": "regen"},
            {"entity_id": "binary_sensor.motion_backyard", "platform": "mqtt", "unique_id": "motion"},
            {"entity_id": "sensor.energy_total", "platform": "mqtt", "unique_id": "energy"},
            {
                "entity_id": PUMPE_AUTOMATION_ENTITY_ID,
                "platform": "automation",
                "unique_id": "1699999999999",
            },
        ],
    )


def _relations(payload: dict[str, Any], relationship: str) -> set[tuple[str, str]]:
    return {(item["entity_id"], item["detail"]) for item in payload["related"] if item["relationship"] == relationship}


async def test_related_entity_from_config_entry_reference(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
) -> None:
    seed_related_fixture(config_dir)

    resp = await client.get(f"/v1/related/entity?entity_id={SOURCE_ENTITY_ID}", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    related = data["related"]
    assert {
        "entity_id": GENERATED_ENTITY_ID,
        "relationship": "config-entry-reference",
        "detail": f"config_entry={GENERATED_CONFIG_ENTRY_ID}",
    } in related


async def test_related_entity_reverse_includes_source_and_config_entry_detail(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
) -> None:
    seed_related_fixture(config_dir)

    resp = await client.get(f"/v1/related/entity?entity_id={GENERATED_ENTITY_ID}", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    related = data["related"]
    assert {
        "entity_id": SOURCE_ENTITY_ID,
        "relationship": "referenced-entity",
        "detail": f"config_entry={GENERATED_CONFIG_ENTRY_ID}",
    } in related


async def test_related_entity_yaml_exact_value_relation(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
) -> None:
    seed_related_fixture(config_dir)

    resp = await client.get(f"/v1/related/entity?entity_id={SOURCE_ENTITY_ID}", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert {
        "entity_id": YAML_PEER_ENTITY_ID,
        "relationship": "yaml-reference",
        "detail": "configuration.yaml",
    } in data["related"]


async def test_related_entity_does_not_match_embedded_strings_or_unknown_ids(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
) -> None:
    seed_related_fixture(config_dir)

    resp = await client.get(f"/v1/related/entity?entity_id={SOURCE_ENTITY_ID}", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    related_ids = {item["entity_id"] for item in data["related"]}
    assert EMBEDDED_ENTITY_ID not in related_ids
    assert UNKNOWN_ENTITY_ID not in related_ids


async def test_related_entity_auth_missing_token(client: TestClient, config_dir: Path) -> None:
    seed_related_fixture(config_dir)

    resp = await client.get(f"/v1/related/entity?entity_id={SOURCE_ENTITY_ID}")

    assert resp.status == 401


async def test_related_entity_auth_invalid_token(client: TestClient, config_dir: Path) -> None:
    seed_related_fixture(config_dir)

    resp = await client.get(
        f"/v1/related/entity?entity_id={SOURCE_ENTITY_ID}",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert resp.status == 401


async def test_related_entity_ingress_header_from_untrusted_source_rejected(
    client: TestClient, config_dir: Path
) -> None:
    """A spoofed ingress header from an untrusted source must not bypass auth."""
    seed_related_fixture(config_dir)

    resp = await client.get(
        f"/v1/related/entity?entity_id={SOURCE_ENTITY_ID}",
        headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"},
    )

    assert resp.status == 401


async def test_related_entity_ingress_from_trusted_source_ok(
    client: TestClient, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trusted ingress source may reach the endpoint without a bearer token."""
    seed_related_fixture(config_dir)
    monkeypatch.setenv("INGRESS_PROXY_IPS", "127.0.0.1")

    resp = await client.get(
        f"/v1/related/entity?entity_id={SOURCE_ENTITY_ID}",
        headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"},
    )

    assert resp.status == 200


async def test_related_entity_rejects_unknown_entity_id(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
) -> None:
    seed_related_fixture(config_dir)

    resp = await client.get(f"/v1/related/entity?entity_id={UNKNOWN_ENTITY_ID}", headers=auth_headers)

    assert resp.status == 404


async def test_related_entity_stale_param_returns_config_refs(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
) -> None:
    seed_related_fixture(config_dir)

    resp = await client.get(f"/v1/related/entity?entity_id={UNKNOWN_ENTITY_ID}&stale=true", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert data["stale"] is True
    hits = {(h["location"], h["matched_value"]) for h in data["stale_refs"]}
    assert ("configuration.yaml", UNKNOWN_ENTITY_ID) in hits


async def test_related_live_entity_finds_automation_referencing_it_from_jinja(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
) -> None:
    """A live entity used only inside a Jinja template must still name the automation.

    This is the `ent related` delete-safety question ("what breaks if I delete
    this?"). Before the fix the boundary-aware matcher was reachable only for
    *stale* entities, so a live entity referenced from `{{ states('...') }}`
    reported nothing at all.
    """
    _seed_realistic_automations(config_dir)

    resp = await client.get("/v1/related/entity?entity_id=sensor.fuellstand", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert _relations(data, "automation-reference") == {
        (PUMPE_AUTOMATION_ENTITY_ID, "automations.yaml:[0] (Pumpe steuern)")
    }


async def test_related_live_entity_finds_every_automation_targeting_it(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
) -> None:
    """A structural target.entity_id reference is attributed to each automation.

    The co-occurrence graph could at best say "switch.pumpe and binary_sensor.regen
    appear together"; it can never say *which automation* would break.
    """
    _seed_realistic_automations(config_dir)

    resp = await client.get("/v1/related/entity?entity_id=switch.pumpe", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert _relations(data, "automation-reference") == {
        (PUMPE_AUTOMATION_ENTITY_ID, "automations.yaml:[0] (Pumpe steuern)"),
        # Not in the registry snapshot, so its entity_id is derived from the alias.
        (SCHALTER_AUTOMATION_ENTITY_ID, "automations.yaml:[1] (Nur Schalter)"),
    }


async def test_related_live_entity_finds_automations_in_split_directory(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
) -> None:
    """Same references, but authored as !include_dir_merge_list automations/."""
    _seed_realistic_automations(config_dir, split_dir=True)

    jinja = await client.get("/v1/related/entity?entity_id=sensor.fuellstand", headers=auth_headers)
    assert jinja.status == 200
    assert _relations(await jinja.json(), "automation-reference") == {
        (PUMPE_AUTOMATION_ENTITY_ID, "automations/pumpe.yaml:[0] (Pumpe steuern)")
    }

    structural = await client.get("/v1/related/entity?entity_id=switch.pumpe", headers=auth_headers)
    assert structural.status == 200
    assert _relations(await structural.json(), "automation-reference") == {
        (PUMPE_AUTOMATION_ENTITY_ID, "automations/pumpe.yaml:[0] (Pumpe steuern)"),
        (SCHALTER_AUTOMATION_ENTITY_ID, "automations/schalter.yaml:[0] (Nur Schalter)"),
    }


async def test_related_live_entity_finds_automation_inside_a_package(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
) -> None:
    """An automation declared under a package's `automation:` key is attributed too."""
    _seed_realistic_automations(config_dir)

    resp = await client.get("/v1/related/entity?entity_id=binary_sensor.motion_backyard", headers=auth_headers)

    assert resp.status == 200
    assert _relations(await resp.json(), "automation-reference") == {
        ("automation.security_alert", "packages/security.yaml:automation[0] (Security Alert)")
    }


async def test_related_live_entity_without_automation_refs_reports_none(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
) -> None:
    """No automation mentions binary_sensor.regen's peer, so nothing is invented."""
    _seed_realistic_automations(config_dir)

    resp = await client.get("/v1/related/entity?entity_id=binary_sensor.regen", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert _relations(data, "automation-reference") == {
        (SCHALTER_AUTOMATION_ENTITY_ID, "automations.yaml:[1] (Nur Schalter)")
    }
    assert not _relations(data, "automation-reference") & {
        (PUMPE_AUTOMATION_ENTITY_ID, "automations.yaml:[0] (Pumpe steuern)")
    }


async def test_related_reference_outside_an_automation_is_not_attributed_to_one(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
) -> None:
    """packages/energy.yaml holds a template sensor *and* nothing else automation-shaped.

    packages/security.yaml has an `automation:` key; energy.yaml's Jinja reference
    must not be blamed on it, and a hit elsewhere in a file that does contain
    automations must not be blamed on a neighbouring automation either.
    """
    _seed_realistic_automations(config_dir)

    resp = await client.get("/v1/related/entity?entity_id=sensor.energy_total", headers=auth_headers)

    assert resp.status == 200
    assert _relations(await resp.json(), "automation-reference") == set()


async def test_related_live_entity_finds_automations_in_include_dir_list(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
) -> None:
    """!include_dir_list makes each file one whole automation, so the prefix is empty."""
    (config_dir / "automations.yaml").unlink()
    autos = config_dir / "automations"
    autos.mkdir()
    single = REALISTIC_AUTOMATIONS.split("\n\n")[0]
    # !include_dir_list files hold one automation as a mapping, not a list item.
    (autos / "pumpe.yaml").write_text(
        "\n".join(line[2:] if line.startswith("  ") else line.removeprefix("- ") for line in single.splitlines())
        + "\n",
        encoding="utf-8",
    )
    config = (config_dir / "configuration.yaml").read_text(encoding="utf-8")
    (config_dir / "configuration.yaml").write_text(
        config.replace("automation: !include automations.yaml", "automation: !include_dir_list automations"),
        encoding="utf-8",
    )
    _write_registry(
        config_dir,
        [
            {"entity_id": "sensor.fuellstand", "platform": "mqtt", "unique_id": "fuellstand"},
            {"entity_id": PUMPE_AUTOMATION_ENTITY_ID, "platform": "automation", "unique_id": "1699999999999"},
        ],
    )

    resp = await client.get("/v1/related/entity?entity_id=sensor.fuellstand", headers=auth_headers)

    assert resp.status == 200
    assert _relations(await resp.json(), "automation-reference") == {
        (PUMPE_AUTOMATION_ENTITY_ID, "automations/pumpe.yaml (Pumpe steuern)")
    }


async def test_related_automation_without_id_or_alias_is_still_reported(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
) -> None:
    """An anonymous automation cannot be named, but dropping the reference would be worse."""
    _seed_realistic_automations(config_dir)
    with (config_dir / "automations.yaml").open("a", encoding="utf-8") as handle:
        handle.write("\n- triggers:\n    - trigger: state\n      entity_id: sensor.namenlos\n  actions: []\n")
    _write_registry(config_dir, [{"entity_id": "sensor.namenlos", "platform": "mqtt", "unique_id": "namenlos"}])

    resp = await client.get("/v1/related/entity?entity_id=sensor.namenlos", headers=auth_headers)

    assert resp.status == 200
    assert _relations(await resp.json(), "automation-reference") == {("automation.unnamed", "automations.yaml:[2]")}


def test_automation_references_resolve_through_a_symlinked_config_dir(
    config_dir: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A config base that is a symlink must not make every include look out-of-tree.

    The include-graph walk resolves symlinks internally, so an unresolved base
    would fail the "target is inside base" check for every included file and
    silently attribute nothing.
    """
    from companion.routes.related import RelatedGraph  # local: the HTTP tests never need it

    _seed_realistic_automations(config_dir)
    link = tmp_path_factory.mktemp("link") / "config"
    link.symlink_to(config_dir, target_is_directory=True)

    graph = RelatedGraph(link)
    graph.load()

    assert graph.automation_references("sensor.fuellstand") == {
        RelatedItem(PUMPE_AUTOMATION_ENTITY_ID, "automation-reference", "automations.yaml:[0] (Pumpe steuern)")
    }


async def test_related_automation_entity_is_not_related_to_itself(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
) -> None:
    """Asking about the automation entity itself must not report a self-reference."""
    _seed_realistic_automations(config_dir)

    resp = await client.get(f"/v1/related/entity?entity_id={PUMPE_AUTOMATION_ENTITY_ID}", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert PUMPE_AUTOMATION_ENTITY_ID not in {item["entity_id"] for item in data["related"]}


async def test_related_entity_stale_param_live_entity_reports_not_stale(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
) -> None:
    seed_related_fixture(config_dir)

    resp = await client.get(f"/v1/related/entity?entity_id={SOURCE_ENTITY_ID}&stale=true", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert data["stale"] is False
    assert data["stale_refs"] == []
    # live relations are still reported when ?stale=true is passed for a live entity
    assert any(item["entity_id"] == GENERATED_ENTITY_ID for item in data["related"])
