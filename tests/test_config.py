"""Tests for YAML config read endpoints (Phase 2)."""

from pathlib import Path

from aiohttp.test_utils import TestClient


async def test_list_files(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Should list all YAML files in config dir."""
    resp = await client.get("/v1/config/files", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    files = data["files"]
    assert "automations.yaml" in files
    assert "configuration.yaml" in files
    assert "scripts.yaml" in files


async def test_list_files_excludes_secrets(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """secrets.yaml should never appear in the file list."""
    # Create a secrets.yaml in the config dir
    (config_dir / "secrets.yaml").write_text("wifi_password: hunter2\n")
    resp = await client.get("/v1/config/files", headers=auth_headers)
    data = await resp.json()
    assert "secrets.yaml" not in data["files"]


async def test_read_file(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Should return the full content of a YAML file."""
    resp = await client.get("/v1/config/file?path=automations.yaml", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    assert data["path"] == "automations.yaml"
    assert "door_light" in data["content"]


async def test_read_file_not_found(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.get("/v1/config/file?path=nonexistent.yaml", headers=auth_headers)
    assert resp.status == 404


async def test_read_block_by_id(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Should return only the matching block from a list-type YAML file."""
    resp = await client.get(
        "/v1/config/block?path=automations.yaml&id=automation.door_light",
        headers=auth_headers,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["id"] == "automation.door_light"
    assert "Door Light" in data["content"]


async def test_read_block_from_dict(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Should return a named block from a dict-type YAML file."""
    resp = await client.get(
        "/v1/config/block?path=scripts.yaml&id=welcome_home",
        headers=auth_headers,
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["id"] == "welcome_home"
    assert "Welcome Home" in data["content"]


async def test_read_block_not_found(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.get(
        "/v1/config/block?path=automations.yaml&id=nonexistent",
        headers=auth_headers,
    )
    assert resp.status == 404


async def test_path_traversal_rejected(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.get("/v1/config/file?path=../etc/passwd", headers=auth_headers)
    assert resp.status == 400


async def test_secrets_yaml_denied(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """Direct access to secrets.yaml must be forbidden."""
    (config_dir / "secrets.yaml").write_text("wifi_password: hunter2\n")
    resp = await client.get("/v1/config/file?path=secrets.yaml", headers=auth_headers)
    assert resp.status == 403
