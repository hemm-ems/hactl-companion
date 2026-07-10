"""Docker integration tests for related-entity graph endpoints."""

from __future__ import annotations

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
