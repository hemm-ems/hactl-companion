"""Tests for the /v1/ref/scan and /v1/ref/replace endpoints."""

from __future__ import annotations

from pathlib import Path

from aiohttp.test_utils import TestClient


async def test_ref_scan_returns_hits(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    (config_dir / "configuration.yaml").write_text("sensor:\n  value: sensor.gone\n", encoding="utf-8")

    resp = await client.get("/v1/ref/scan?target=sensor.gone", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert data["target"] == "sensor.gone"
    assert data["hits"] == [{"location": "configuration.yaml", "path": "sensor.value", "matched_value": "sensor.gone"}]


async def test_ref_scan_finds_target_embedded_in_jinja_template(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    (config_dir / "configuration.yaml").write_text(
        "sensor:\n  value_template: \"{{ states('sensor.zisterne_liter') }}\"\n", encoding="utf-8"
    )

    resp = await client.get("/v1/ref/scan?target=sensor.zisterne_liter", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert data["hits"] == [
        {"location": "configuration.yaml", "path": "sensor.value_template", "matched_value": "sensor.zisterne_liter"}
    ]


async def test_ref_scan_boundary_rejects_longer_token(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    (config_dir / "configuration.yaml").write_text("a: sensor.foo_bar\nb: asensor.foo\n", encoding="utf-8")

    resp = await client.get("/v1/ref/scan?target=sensor.foo", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert data["hits"] == []


async def test_ref_scan_missing_target_is_400(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    resp = await client.get("/v1/ref/scan", headers=auth_headers)
    assert resp.status == 400


async def test_ref_scan_requires_auth(client: TestClient, config_dir: Path) -> None:
    resp = await client.get("/v1/ref/scan?target=sensor.gone")
    assert resp.status == 401


async def test_ref_entities_enumerates_shaped_leaves(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    (config_dir / "configuration.yaml").write_text("note: hello\nsensor:\n  value: light.kitchen\n", encoding="utf-8")

    resp = await client.get("/v1/ref/entities", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert {
        "location": "configuration.yaml",
        "path": "sensor.value",
        "key": "value",
        "matched_value": "light.kitchen",
    } in data["entities"]
    # The non-entity-shaped scalar "hello" is not enumerated.
    assert all(e["matched_value"] != "hello" for e in data["entities"])


async def test_ref_entities_finds_token_embedded_in_jinja_template(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    (config_dir / "configuration.yaml").write_text(
        "sensor:\n  value_template: \"{{ states('sensor.zisterne_liter') }}\"\n", encoding="utf-8"
    )

    resp = await client.get("/v1/ref/entities", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert {
        "location": "configuration.yaml",
        "path": "sensor.value_template",
        "key": "value_template",
        "matched_value": "sensor.zisterne_liter",
    } in data["entities"]


async def test_ref_entities_requires_auth(client: TestClient, config_dir: Path) -> None:
    resp = await client.get("/v1/ref/entities")
    assert resp.status == 401


async def test_ref_replace_dry_run_reports_but_writes_nothing(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    cfg = config_dir / "configuration.yaml"
    original = "sensor:\n  value: sensor.gone\n"
    cfg.write_text(original, encoding="utf-8")

    resp = await client.post(
        "/v1/ref/replace",
        json={"old": "sensor.gone", "new": "sensor.new", "dry_run": True},
        headers=auth_headers,
    )

    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "dry_run"
    assert data["changes"] == [
        {"location": "configuration.yaml", "path": "sensor.value", "before": "sensor.gone", "after": "sensor.new"}
    ]
    assert cfg.read_text(encoding="utf-8") == original


async def test_ref_replace_apply_rewrites_file(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    cfg = config_dir / "configuration.yaml"
    cfg.write_text("sensor:\n  value: sensor.gone\n", encoding="utf-8")

    resp = await client.post(
        "/v1/ref/replace",
        json={"old": "sensor.gone", "new": "sensor.new", "dry_run": False},
        headers=auth_headers,
    )

    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "applied"
    text = cfg.read_text(encoding="utf-8")
    assert "sensor.new" in text
    assert "sensor.gone" not in text


async def test_ref_replace_rewrites_token_embedded_in_jinja_template(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    cfg = config_dir / "configuration.yaml"
    cfg.write_text("sensor:\n  value_template: \"{{ states('sensor.zisterne_liter') }} plus 1\"\n", encoding="utf-8")

    resp = await client.post(
        "/v1/ref/replace",
        json={"old": "sensor.zisterne_liter", "new": "sensor.zisterne_neu", "dry_run": False},
        headers=auth_headers,
    )

    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "applied"
    assert data["changes"] == [
        {
            "location": "configuration.yaml",
            "path": "sensor.value_template",
            "before": "sensor.zisterne_liter",
            "after": "sensor.zisterne_neu",
        }
    ]
    text = cfg.read_text(encoding="utf-8")
    assert "{{ states('sensor.zisterne_neu') }} plus 1" in text
    assert "sensor.zisterne_liter" not in text


async def test_ref_replace_boundary_leaves_longer_token_untouched(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    cfg = config_dir / "configuration.yaml"
    original = "a: sensor.foo_bar\nb: asensor.foo\n"
    cfg.write_text(original, encoding="utf-8")

    resp = await client.post(
        "/v1/ref/replace",
        json={"old": "sensor.foo", "new": "sensor.new", "dry_run": False},
        headers=auth_headers,
    )

    assert resp.status == 200
    data = await resp.json()
    assert data["changes"] == []
    assert cfg.read_text(encoding="utf-8") == original


async def test_ref_replace_defaults_to_dry_run(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    cfg = config_dir / "configuration.yaml"
    original = "sensor:\n  value: sensor.gone\n"
    cfg.write_text(original, encoding="utf-8")

    # No dry_run in body → safe default (report only, no write).
    resp = await client.post(
        "/v1/ref/replace",
        json={"old": "sensor.gone", "new": "sensor.new"},
        headers=auth_headers,
    )

    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "dry_run"
    assert cfg.read_text(encoding="utf-8") == original


async def test_ref_replace_missing_old_is_400(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    resp = await client.post("/v1/ref/replace", json={"new": "sensor.new"}, headers=auth_headers)
    assert resp.status == 400


async def test_ref_replace_requires_auth(client: TestClient, config_dir: Path) -> None:
    resp = await client.post("/v1/ref/replace", json={"old": "sensor.gone", "new": "sensor.new"})
    assert resp.status == 401
