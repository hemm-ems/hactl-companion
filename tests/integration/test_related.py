"""Docker integration tests for related-entity graph endpoints."""

from __future__ import annotations

import time
from collections.abc import Callable

import requests

from tests.related_fixture import (
    EMBEDDED_ENTITY_ID,
    GENERATED_CONFIG_ENTRY_ID,
    GENERATED_ENTITY_ID,
    SOURCE_ENTITY_ID,
    UNKNOWN_ENTITY_ID,
    YAML_PEER_ENTITY_ID,
)


class TestRelatedEntity:
    """Fixture-shape coverage of the config-entry / yaml-reference graph + live auth.

    This test seeds a *hand-authored* ``.storage`` snapshot (``related_fixture``)
    and asserts the companion reproduces it. It is deliberately NOT an HA oracle:
    HA never validates those synthetic config entries, so it exercises the
    *shape* of the config-entry-reference / yaml-reference relationship types
    (which real HA config entries are impractical to build in a test) plus the
    live 401/wrong-token/spoofed-ingress auth path against the running companion.

    The HA-derived oracle for the defect-prone *automation* path — where the
    expected answer is computed from HA's own ``search/related`` at test time
    (TC-1 / invariant C-9) — is ``TestRelatedEntityHAOracle`` below. That is the
    gate; this remains for the config-entry input shape it uniquely covers.
    """

    def test_related_entity_auth_and_graph(
        self,
        companion_url: str,
        auth_headers: dict[str, str],
        related_fixture_seeded: None,
    ) -> None:
        missing = requests.get(
            f"{companion_url}/v1/related/entity",
            params={"entity_id": SOURCE_ENTITY_ID},
            timeout=10,
        )
        assert missing.status_code == 401

        wrong = requests.get(
            f"{companion_url}/v1/related/entity",
            params={"entity_id": SOURCE_ENTITY_ID},
            headers={"Authorization": "Bearer wrong-token"},
            timeout=10,
        )
        assert wrong.status_code == 401

        # A spoofed ingress header from outside the trusted proxy must not bypass auth.
        ingress = requests.get(
            f"{companion_url}/v1/related/entity",
            params={"entity_id": SOURCE_ENTITY_ID},
            headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"},
            timeout=10,
        )
        assert ingress.status_code == 401

        r = requests.get(
            f"{companion_url}/v1/related/entity",
            params={"entity_id": SOURCE_ENTITY_ID},
            headers=auth_headers,
            timeout=10,
        )
        assert r.status_code == 200
        related = r.json()["related"]
        assert {
            "entity_id": GENERATED_ENTITY_ID,
            "relationship": "config-entry-reference",
            "detail": f"config_entry={GENERATED_CONFIG_ENTRY_ID}",
        } in related
        assert {
            "entity_id": YAML_PEER_ENTITY_ID,
            "relationship": "yaml-reference",
            "detail": "configuration.yaml",
        } in related

        related_ids = {item["entity_id"] for item in related}
        assert EMBEDDED_ENTITY_ID not in related_ids
        assert UNKNOWN_ENTITY_ID not in related_ids

        reverse = requests.get(
            f"{companion_url}/v1/related/entity",
            params={"entity_id": GENERATED_ENTITY_ID},
            headers=auth_headers,
            timeout=10,
        )
        assert reverse.status_code == 200
        assert {
            "entity_id": SOURCE_ENTITY_ID,
            "relationship": "referenced-entity",
            "detail": f"config_entry={GENERATED_CONFIG_ENTRY_ID}",
        } in reverse.json()["related"]

        unknown = requests.get(
            f"{companion_url}/v1/related/entity",
            params={"entity_id": UNKNOWN_ENTITY_ID},
            headers=auth_headers,
            timeout=10,
        )
        assert unknown.status_code == 404


# ---------------------------------------------------------------------------
# HA-oracle test (invariant C-9): the expected answer is computed from HA's own
# `search/related` at test time, over relationships HA actually materialized —
# never from a hand-authored fixture. Real helpers, a real automation loaded by
# real HA, HA as the source of truth for "what is related".
# ---------------------------------------------------------------------------

# Structural reference (trigger + action target) — HA's static search finds this.
STRUCT_ENTITY = "input_boolean.hactl_oracle_struct"
# Referenced ONLY inside a Jinja template — HA's static search does NOT find this.
TMPL_ENTITY = "input_boolean.hactl_oracle_tmpl"
# Referenced by nothing — negative control.
LONELY_ENTITY = "input_boolean.hactl_oracle_lonely"

_INPUT_BOOLEAN_YAML = (
    "hactl_oracle_struct:\n  name: Hactl Oracle Struct\n"
    "hactl_oracle_tmpl:\n  name: Hactl Oracle Tmpl\n"
    "hactl_oracle_lonely:\n  name: Hactl Oracle Lonely\n"
)

# Structural refs to STRUCT_ENTITY (trigger + target); TMPL_ENTITY only ever
# appears embedded in a Jinja string, which an exact-literal matcher — and HA's
# own static search — cannot see structurally.
_AUTOMATION_YAML = (
    "- id: hactl_oracle_auto\n"
    "  alias: Hactl Oracle Automation\n"
    "  trigger:\n"
    "    - platform: state\n"
    f"      entity_id: {STRUCT_ENTITY}\n"
    "  condition:\n"
    "    - condition: template\n"
    f"      value_template: \"{{{{ is_state('{TMPL_ENTITY}', 'on') }}}}\"\n"
    "  action:\n"
    "    - service: input_boolean.toggle\n"
    "      target:\n"
    f"        entity_id: {STRUCT_ENTITY}\n"
)


def _put_file(companion_url: str, headers: dict[str, str], path: str, content: str) -> None:
    r = requests.put(
        f"{companion_url}/v1/config/file",
        params={"path": path, "dry_run": "false"},
        data=content,
        headers=headers,
        timeout=15,
    )
    assert r.status_code == 200, r.text


def _read_file(companion_url: str, headers: dict[str, str], path: str) -> str:
    r = requests.get(
        f"{companion_url}/v1/config/file",
        params={"path": path, "resolve": "false"},
        headers=headers,
        timeout=10,
    )
    assert r.status_code == 200, r.text
    return r.json()["content"]


def _reload(companion_url: str, headers: dict[str, str], domain: str) -> None:
    r = requests.post(f"{companion_url}/v1/ha/reload/{domain}", headers=headers, timeout=15)
    assert r.status_code == 200, r.text


def _companion_related(companion_url: str, headers: dict[str, str], entity_id: str) -> dict:
    r = requests.get(
        f"{companion_url}/v1/related/entity",
        params={"entity_id": entity_id},
        headers=headers,
        timeout=10,
    )
    assert r.status_code == 200, f"{entity_id}: {r.status_code} {r.text}"
    return r.json()


def _automation_relations(payload: dict) -> set[str]:
    return {item["entity_id"] for item in payload["related"] if item["relationship"] == "automation-reference"}


def _ha_related_automations(ha_ws_command: Callable[..., object], entity_id: str) -> set[str]:
    """HA's own answer: the automations `search/related` links to `entity_id`."""
    result = ha_ws_command("search/related", item_type="entity", item_id=entity_id)
    assert isinstance(result, dict), f"unexpected search/related shape: {result!r}"
    return set(result.get("automation", []))


class TestRelatedEntityHAOracle:
    """Reconcile companion's related-graph against HA's own `search/related`.

    Builds real relationships on the live HA (three registry-backed helpers and
    an automation that references one of them structurally and another via a
    Jinja template), reloads so HA materializes everything, then asks HA which
    automations it considers related and asserts the companion reproduces every
    one of them (superset), reports the template-only reference HA's static
    search misses, and invents nothing for an unreferenced entity.
    """

    def test_related_reconciles_with_ha_search_related(
        self,
        companion_url: str,
        auth_headers: dict[str, str],
        ha_ws_command: Callable[..., object],
        _ha_ready: None,
    ) -> None:
        h = auth_headers

        # 1. Real, registry-backed helpers (YAML input_boolean entities DO land
        #    in HA's entity registry) + a real automation referencing them.
        _put_file(companion_url, h, "input_boolean.yaml", _INPUT_BOOLEAN_YAML)
        config = _read_file(companion_url, h, "configuration.yaml")
        additions = ""
        if "input_boolean:" not in config:
            additions += "input_boolean: !include input_boolean.yaml\n"
        if "automation:" not in config:
            additions += "automation: !include automations.yaml\n"
        if additions:
            _put_file(companion_url, h, "configuration.yaml", config.rstrip("\n") + "\n" + additions)
        _reload(companion_url, h, "input_boolean")

        _put_file(companion_url, h, "automations.yaml", _AUTOMATION_YAML)
        _reload(companion_url, h, "automation")

        # 2. companion reads the on-disk .storage snapshot, which HA flushes on a
        #    debounced ~10s delay (SAVE_DELAY). Wait until the freshly-registered
        #    entity is visible on disk before reconciling — a 404 here would mean
        #    companion cannot see an entity HA has registered at all, so the
        #    timeout failure is itself a real finding, not a flake we mask.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            probe = requests.get(
                f"{companion_url}/v1/related/entity",
                params={"entity_id": STRUCT_ENTITY},
                headers=h,
                timeout=10,
            )
            if probe.status_code == 200:
                break
            time.sleep(1)
        else:
            raise AssertionError(
                f"companion never saw HA-registered {STRUCT_ENTITY} in the on-disk registry within 90s"
            )

        # 3. HA is the oracle. What automations does HA itself relate to the
        #    structurally-referenced entity? This is computed from HA at test
        #    time; it must be non-empty or the superset check below is vacuous.
        ha_struct = _ha_related_automations(ha_ws_command, STRUCT_ENTITY)
        assert ha_struct, "HA's search/related reported no automation for the structural entity"

        comp_struct = _automation_relations(_companion_related(companion_url, h, STRUCT_ENTITY))
        # The gate: companion must reproduce every automation HA links — nothing
        # HA reports may be silently missing.
        assert ha_struct <= comp_struct, (
            f"companion missed automations HA relates to {STRUCT_ENTITY}: "
            f"HA={sorted(ha_struct)} companion={sorted(comp_struct)}"
        )

        # 4. Template-only reference. HA's static search cannot see an entity that
        #    appears only inside a Jinja string, so it reports fewer (typically
        #    zero) automations here — companion's boundary-aware matcher is
        #    designed to catch exactly this (issues #74/#81). The reconciliation
        #    still holds (companion ⊇ HA), AND companion must positively report
        #    the same automation HA linked to the structural entity — the two
        #    references live in one automation.
        ha_tmpl = _ha_related_automations(ha_ws_command, TMPL_ENTITY)
        comp_tmpl = _automation_relations(_companion_related(companion_url, h, TMPL_ENTITY))
        assert ha_tmpl <= comp_tmpl, (
            f"companion missed automations HA relates to {TMPL_ENTITY}: "
            f"HA={sorted(ha_tmpl)} companion={sorted(comp_tmpl)}"
        )
        assert ha_struct <= comp_tmpl, (
            "companion did not report the Jinja-template reference that its "
            f"boundary-aware matcher exists to catch: expected {sorted(ha_struct)} "
            f"in companion's answer for {TMPL_ENTITY}, got {sorted(comp_tmpl)}"
        )
        # Documented divergence (asserted, not silently absorbed): HA's static
        # search does not resolve the template-only entity, so companion reports
        # strictly more than HA here. If a future HA starts resolving templates,
        # ha_tmpl grows and the superset checks above still hold.
        if not (ha_struct <= ha_tmpl):
            assert ha_struct - ha_tmpl, "expected HA to miss the Jinja-only reference"

        # 5. Negative control: an entity referenced by nothing. HA relates it to
        #    no automation, and companion must not fabricate one.
        ha_lonely = _ha_related_automations(ha_ws_command, LONELY_ENTITY)
        assert ha_struct.isdisjoint(ha_lonely), "sanity: HA linked the automation to the unreferenced entity"
        comp_lonely = _companion_related(companion_url, h, LONELY_ENTITY)
        assert _automation_relations(comp_lonely) == set(), (
            f"companion fabricated automation relations for the unreferenced {LONELY_ENTITY}: {comp_lonely['related']}"
        )
