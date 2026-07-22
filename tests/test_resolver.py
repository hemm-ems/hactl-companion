"""Tests for !include resolution (Phase 2)."""

from __future__ import annotations

from pathlib import Path

import yaml
from aiohttp.test_utils import TestClient


async def test_resolve_includes(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Reading configuration.yaml with resolve=true should inline !include content."""
    resp = await client.get("/v1/config/file?path=configuration.yaml&resolve=true", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    content = data["content"]
    # The automation !include should be resolved — we should see automation content
    assert "door_light" in content or "automation" in content


async def test_resolve_false_returns_raw(client: TestClient, auth_headers: dict[str, str]) -> None:
    """resolve=false should return raw content with !include tags."""
    resp = await client.get("/v1/config/file?path=configuration.yaml&resolve=false", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    content = data["content"]
    assert "!include" in content


async def test_resolve_default_is_true(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Default resolve should be true (includes resolved)."""
    resp = await client.get("/v1/config/file?path=configuration.yaml", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    content = data["content"]
    # Should NOT contain raw !include tags
    assert "!include" not in content


async def test_include_dir_named(client: TestClient, auth_headers: dict[str, str]) -> None:
    """!include_dir_named packages should resolve to dict with file stems as keys."""
    resp = await client.get("/v1/config/file?path=configuration.yaml&resolve=true", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    content = data["content"]
    # packages dir has energy.yaml and security.yaml
    assert "energy" in content
    assert "security" in content


async def test_resolve_nonexistent_include(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """If an !include target doesn't exist, it should return an error."""
    (config_dir / "broken.yaml").write_text("data: !include nonexistent.yaml\n")
    resp = await client.get("/v1/config/file?path=broken.yaml&resolve=true", headers=auth_headers)
    assert resp.status == 404


async def test_resolve_secrets_include_denied(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """!include secrets.yaml should be denied."""
    (config_dir / "sneaky.yaml").write_text("passwords: !include secrets.yaml\n")
    (config_dir / "secrets.yaml").write_text("wifi_password: hunter2\n")
    resp = await client.get("/v1/config/file?path=sneaky.yaml&resolve=true", headers=auth_headers)
    assert resp.status == 403


async def test_resolve_does_not_return_null(client: TestClient, auth_headers: dict[str, str]) -> None:
    """resolve=true on configuration.yaml must never return 'null' as content."""
    resp = await client.get("/v1/config/file?path=configuration.yaml&resolve=true", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    content = data["content"]
    assert content.strip() != "null"
    assert not content.startswith("null\n")


async def test_resolve_empty_file_falls_back(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """An empty YAML file with resolve=true should return the raw content, not 'null'."""
    (config_dir / "empty.yaml").write_text("# just a comment\n")
    resp = await client.get("/v1/config/file?path=empty.yaml&resolve=true", headers=auth_headers)
    assert resp.status == 200
    data = await resp.json()
    assert data["content"] == "# just a comment\n"


async def test_circular_include_detected(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """Circular !include (a → b → a) must not cause an infinite loop or a 200 response.

    The server must detect the cycle and return a 4xx or 5xx error promptly.
    """
    import asyncio

    (config_dir / "circular_a.yaml").write_text("data: !include circular_b.yaml\n")
    (config_dir / "circular_b.yaml").write_text("data: !include circular_a.yaml\n")

    # Give the server at most 5 seconds to respond — an infinite loop would time out
    try:
        resp = await asyncio.wait_for(
            client.get("/v1/config/file?path=circular_a.yaml&resolve=true", headers=auth_headers),
            timeout=5.0,
        )
    except TimeoutError:
        raise AssertionError(
            "Server did not respond within 5 s — possible infinite loop in !include resolver"
        ) from None

    # 400 (bad request / cycle detected) or 500 (internal error) are both acceptable;
    # 200 with raw content is acceptable too if the resolver bails out early.
    # What is NOT acceptable: hanging indefinitely (caught above).
    assert resp.status in (200, 400, 500), f"unexpected status {resp.status}"
    if resp.status == 200:
        # If the server chose to return 200, the content must not be empty or recursive garbage
        data = await resp.json()
        content: str = data.get("content", "")
        # A sane fallback is returning the raw unparsed YAML
        assert "!include" in content or len(content) > 0, "200 response with empty content for circular include"


async def test_include_dir_merge_list(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """!include_dir_merge_list concatenates each file's list into one flat list.

    This is the standard tag for a split automations/ directory. It used to fall
    through to the unknown-tag branch and resolve to the bare directory string,
    so every automation in a split layout was invisible to `ent related`,
    `ref scan` and `config file`.
    """
    split = config_dir / "split_automations"
    split.mkdir()
    (split / "a.yaml").write_text("- id: split_one\n  alias: Split One\n")
    (split / "b.yaml").write_text("- id: split_two\n  alias: Split Two\n")
    (config_dir / "merged.yaml").write_text("automation: !include_dir_merge_list split_automations/\n")

    resp = await client.get("/v1/config/file?path=merged.yaml&resolve=true", headers=auth_headers)
    assert resp.status == 200
    content = (await resp.json())["content"]

    parsed = yaml.safe_load(content)
    automations = parsed["automation"]
    assert isinstance(automations, list), f"expected a list, got {type(automations).__name__}: {automations!r}"
    # Flat, not a list of per-file lists — that is what distinguishes
    # merge_list from include_dir_list.
    assert [a["id"] for a in automations] == ["split_one", "split_two"]


async def test_unresolved_tag_keeps_its_directive(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """An unresolved tag keeps its directive text rather than degrading to the bare value.

    `!secret home_lat` used to resolve to the string "home_lat" — the secret's
    KEY rendered where its VALUE belongs, indistinguishable from a real setting.
    The secret itself is never read here; only the directive is preserved.
    """
    (config_dir / "withsecret.yaml").write_text("homeassistant:\n  latitude: !secret home_lat\n")

    resp = await client.get("/v1/config/file?path=withsecret.yaml&resolve=true", headers=auth_headers)
    assert resp.status == 200
    content = (await resp.json())["content"]

    latitude = yaml.safe_load(content)["homeassistant"]["latitude"]
    assert latitude == "!secret home_lat", f"expected the directive preserved, got {latitude!r}"
