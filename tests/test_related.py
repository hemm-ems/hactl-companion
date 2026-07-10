"""Tests for the generic related-entity graph endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient

from tests.related_fixture import (
    EMBEDDED_ENTITY_ID,
    GENERATED_CONFIG_ENTRY_ID,
    GENERATED_ENTITY_ID,
    SOURCE_ENTITY_ID,
    UNKNOWN_ENTITY_ID,
    YAML_PEER_ENTITY_ID,
    seed_related_fixture,
)


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
