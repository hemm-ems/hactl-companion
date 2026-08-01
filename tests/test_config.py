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


async def test_read_block_by_bare_index(client: TestClient, auth_headers: dict[str, str]) -> None:
    """A list-rooted file is addressable by zero-based position."""
    resp = await client.get(
        "/v1/config/block?path=automations.yaml&id=0",
        headers=auth_headers,
    )
    assert resp.status == 200
    data = await resp.json()
    assert "Door Light" in data["content"]


async def test_read_block_by_bracketed_index_addresses_template_yaml(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """`[0]` — the exact prefix `ref scan` prints — pastes back as an address.

    This is the acceptance case of hemm-ems/hactl-companion#84: template.yaml
    blocks carry neither `id:` nor `alias:`, so before the index form NO input
    could address them and every call 404ed.
    """
    resp = await client.get(
        "/v1/config/block?path=template.yaml&id=%5B0%5D",  # url-encoded [0]
        headers=auth_headers,
    )
    assert resp.status == 200
    data = await resp.json()
    assert "tpl_energie_zaehler" in data["content"]


async def test_read_block_index_out_of_range_names_the_range(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.get(
        "/v1/config/block?path=automations.yaml&id=99",
        headers=auth_headers,
    )
    assert resp.status == 404
    body = await resp.text()
    assert "index out of range" in body
    assert "0.." in body


async def test_read_block_index_on_mapping_rooted_file_is_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Positions only exist in list-rooted files; scripts.yaml is a mapping."""
    resp = await client.get(
        "/v1/config/block?path=scripts.yaml&id=0",
        headers=auth_headers,
    )
    assert resp.status == 404


async def test_read_block_numeric_id_wins_over_index(
    client: TestClient, config_dir: Path, auth_headers: dict[str, str]
) -> None:
    """HA's UI mints purely numeric automation ids (millisecond timestamps);
    a bare number that matches an existing id must keep resolving as that id
    (H-17 over in hactl: printed identifiers resolve), never as a position.
    The bracketed form stays available for unambiguous positional access."""
    (config_dir / "numeric_ids.yaml").write_text(
        "- id: '1'\n  alias: First By Position\n- id: '0'\n  alias: Zero By Id\n",
        encoding="utf-8",
    )
    resp = await client.get(
        "/v1/config/block?path=numeric_ids.yaml&id=0",
        headers=auth_headers,
    )
    assert resp.status == 200
    data = await resp.json()
    assert "Zero By Id" in data["content"]

    resp = await client.get(
        "/v1/config/block?path=numeric_ids.yaml&id=%5B0%5D",
        headers=auth_headers,
    )
    assert resp.status == 200
    data = await resp.json()
    assert "First By Position" in data["content"]


async def test_path_traversal_rejected(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.get("/v1/config/file?path=../etc/passwd", headers=auth_headers)
    assert resp.status == 400


async def test_secrets_yaml_denied(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """Direct access to secrets.yaml must be forbidden."""
    (config_dir / "secrets.yaml").write_text("wifi_password: hunter2\n")
    resp = await client.get("/v1/config/file?path=secrets.yaml", headers=auth_headers)
    assert resp.status == 403


async def test_storage_directory_denied_unresolved(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """`.storage` holds cleartext credentials (`core.config_entries`), the full
    auth store, and a full state snapshot — none of it reachable through this
    route, exactly like `secrets.yaml`. Verified live 2026-08-01: before this
    fix, `GET .../v1/config/file?path=.storage/core.config_entries&resolve=false`
    returned 213 config entries with cleartext credentials for 38 integrations.
    """
    storage = config_dir / ".storage"
    storage.mkdir()
    (storage / "core.config_entries").write_text(
        '{"data": {"entries": [{"domain": "mqtt", "data": {"password": "hunter2"}}]}}', encoding="utf-8"
    )
    resp = await client.get("/v1/config/file?path=.storage/core.config_entries&resolve=false", headers=auth_headers)
    assert resp.status == 403


async def test_storage_directory_denied_resolved(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """The same refusal holds with `resolve=true` (the default)."""
    storage = config_dir / ".storage"
    storage.mkdir()
    (storage / "core.config_entries").write_text('{"data": {"entries": []}}', encoding="utf-8")
    resp = await client.get("/v1/config/file?path=.storage/core.config_entries", headers=auth_headers)
    assert resp.status == 403


async def test_storage_directory_denied_via_block_route(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """`config/block` is a sibling read route and must refuse `.storage` identically."""
    storage = config_dir / ".storage"
    storage.mkdir()
    (storage / "core.config_entries").write_text('{"data": {"entries": []}}', encoding="utf-8")
    resp = await client.get("/v1/config/block?path=.storage/core.config_entries&id=0", headers=auth_headers)
    assert resp.status == 403


async def test_storage_directory_never_listed(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """The listing route already hides `.storage` (hidden-dir skip) — guard the regression."""
    storage = config_dir / ".storage"
    storage.mkdir()
    (storage / "core.config_entries").write_text('{"data": {}}', encoding="utf-8")
    resp = await client.get("/v1/config/files", headers=auth_headers)
    data = await resp.json()
    assert not any(".storage" in f for f in data["files"])
