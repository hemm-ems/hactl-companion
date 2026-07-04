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


async def test_ref_scan_missing_target_is_400(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    resp = await client.get("/v1/ref/scan", headers=auth_headers)
    assert resp.status == 400


async def test_ref_scan_requires_auth(client: TestClient, config_dir: Path) -> None:
    resp = await client.get("/v1/ref/scan?target=sensor.gone")
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
