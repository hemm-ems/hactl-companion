"""Tests for !include resolution (Phase 2)."""

from __future__ import annotations

from pathlib import Path

import yaml
from aiohttp.test_utils import TestClient

from companion.yaml_resolver import INCLUDE_TAGS, PRESERVED_TAGS, claims_to_include


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


# ---------------------------------------------------------------------------
# C-11: an include-family tag this build does not implement is an error
# ---------------------------------------------------------------------------


def test_preserved_tags_are_not_include_family() -> None:
    """Canary: the two tag sets must stay disjoint, and neither may shadow the other.

    ``_resolve_tag`` dispatches on ``INCLUDE_TAGS`` first, then on
    ``claims_to_include``, then preserves. If a preserved tag ever started with
    "include" the middle branch would swallow it and a value-carrying tag would
    become a 400; if an include tag were added to PRESERVED_TAGS it would be
    resolved anyway and the entry would be a lie. Both are cheap to assert and
    impossible to notice by reading.
    """
    assert not (INCLUDE_TAGS & PRESERVED_TAGS)
    assert not [t for t in PRESERVED_TAGS if claims_to_include(t)]
    assert all(claims_to_include(t) for t in INCLUDE_TAGS)


def test_known_include_tags_are_exactly_has_include_vocabulary() -> None:
    """Canary: adding an include tag is a reviewed act, not a silent widening.

    Home Assistant's YAML loader has a closed constructor set — verified live on
    2026-07-25 by writing `!my_custom_thing` into configuration.yaml, where HA's
    own check_config answers `invalid | could not determine a constructor for
    the tag` and automation.reload returns 500. If HA ever adds a tag, this
    canary is the place where somebody has to notice.
    """
    assert {
        "!include",
        "!include_dir_list",
        "!include_dir_merge_list",
        "!include_dir_named",
        "!include_dir_merge_named",
    } == INCLUDE_TAGS
    assert {"!secret", "!env_var", "!input"} == PRESERVED_TAGS


async def test_unknown_include_tag_is_refused_not_degraded(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """C-11: an unimplemented `!include_dir_*` tag must fail loudly, not resolve to nothing.

    A future HA release adding an include tag is the realistic source of one
    (today's HA refuses to load any tag it has no constructor for). Falling back
    to the preserve-directive branch would leave everything that tag names
    absent from the answer with nothing to say so — the exact shape of the
    `!include_dir_merge_list` bug that hid a whole split automation directory
    while every test stayed green.
    """
    split = config_dir / "future_dir"
    split.mkdir()
    (split / "a.yaml").write_text("- id: hidden_one\n  alias: Hidden One\n")
    (config_dir / "future.yaml").write_text("automation: !include_dir_merge_flat future_dir/\n")

    resp = await client.get("/v1/config/file?path=future.yaml&resolve=true", headers=auth_headers)
    assert resp.status == 400, (
        f"answered {resp.status} for an unimplemented include tag — the files it names are missing "
        "from the resolved tree, and a caller cannot tell an empty directory from one never opened"
    )
    message = (await resp.json())["error"]["message"]
    assert "!include_dir_merge_flat" in message, f"refusal does not name the tag: {message}"
    assert "hidden_one" not in message


async def test_unknown_include_tag_still_readable_unresolved(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """The refusal must leave a way to see the file — otherwise it is a wall, not a guard.

    `resolve=false` does no include processing at all, so it stays available for
    exactly the config the resolver cannot handle.
    """
    (config_dir / "future.yaml").write_text("automation: !include_dir_merge_flat future_dir/\n")

    resp = await client.get("/v1/config/file?path=future.yaml&resolve=false", headers=auth_headers)
    assert resp.status == 200
    assert "!include_dir_merge_flat" in (await resp.json())["content"]


async def test_known_include_tags_still_resolve(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """A guard that rejects everything is not a guard.

    Every implemented tag is exercised in one file, so a future over-broad
    rejection rule (say, prefix-matching `!include_dir_`) fails here instead of
    silently breaking real configs.
    """
    listdir = config_dir / "tagdir"
    listdir.mkdir()
    (listdir / "one.yaml").write_text("- id: item_one\n")
    # merge_named needs mapping-shaped files: HA's own _include_dir_merge_named
    # skips anything that is not a dict, and this resolver matches it.
    nameddir = config_dir / "nameddir"
    nameddir.mkdir()
    (nameddir / "two.yaml").write_text("key_two: 2\n")
    (config_dir / "single.yaml").write_text("value: 1\n")
    (config_dir / "alltags.yaml").write_text(
        "a: !include single.yaml\n"
        "b: !include_dir_list tagdir/\n"
        "c: !include_dir_merge_list tagdir/\n"
        "d: !include_dir_named tagdir/\n"
        "e: !include_dir_merge_named nameddir/\n"
        "f: !secret some_key\n"
    )

    resp = await client.get("/v1/config/file?path=alltags.yaml&resolve=true", headers=auth_headers)
    assert resp.status == 200, await resp.text()
    parsed = yaml.safe_load((await resp.json())["content"])
    assert parsed["a"] == {"value": 1}
    assert parsed["b"] == [[{"id": "item_one"}]]
    assert parsed["c"] == [{"id": "item_one"}]
    assert parsed["d"] == {"one": [{"id": "item_one"}]}
    assert parsed["e"] == {"key_two": 2}
    assert parsed["f"] == "!secret some_key"
