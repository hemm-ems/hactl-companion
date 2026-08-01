"""Live-fire #105 — reading the helpers HA reads, however they are wired.

The read routes resolved through the CREATE path's question: "which single file
may I safely append to?". For an inline domain, a directory include, or a
package that question has no answer, so the read fell back to the conventional
`<domain>.yaml` — which on such an instance does not exist. A domain holding
three helpers listed as empty and every lookup in it 404'd, under a message
naming files the helper is not in.

Three of the four ways a domain reaches Home Assistant were invisible. The
reference instance uses two of them; its own rig fixture carries all four.

The write half deliberately does NOT follow the read half everywhere: an inline
domain and a package keep their helpers under a key, and `surgical` splices an
entry into a document whose root IS the mapping. Widening the read without
holding the write back would have pointed DELETE at configuration.yaml.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient

from companion.wiring import UNRESOLVED_DIR_TAGS, readable_domain_files

# Every wiring, and where each one puts `outdoor_temp`.
INLINE = """\
homeassistant:
  name: Test
input_number:
  outdoor_temp:
    name: Outdoor Temp
    min: -20
    max: 45
"""

MERGE_DIR = """\
homeassistant:
  name: Test
input_number: !include_dir_merge_named numbers
"""

PACKAGE = """\
homeassistant:
  name: Test
  packages: !include_dir_named packages
"""

WIRED = """\
homeassistant:
  name: Test
input_number: !include input_number.yaml
"""

HELPER = "  outdoor_temp:\n    name: Outdoor Temp\n    min: -20\n    max: 45\n"


def _layout(base: Path, config: str) -> Path:
    """Write `config` plus whatever file its wiring implies the helper lives in."""
    (base / "configuration.yaml").write_text(config, encoding="utf-8")
    if config is MERGE_DIR:
        (base / "numbers").mkdir(exist_ok=True)
        (base / "numbers" / "outdoor.yaml").write_text(HELPER[2:], encoding="utf-8")
    elif config is PACKAGE:
        (base / "packages").mkdir(exist_ok=True)
        (base / "packages" / "climate.yaml").write_text("input_number:\n" + HELPER, encoding="utf-8")
    elif config is WIRED:
        (base / "input_number.yaml").write_text(HELPER[2:], encoding="utf-8")
    return base


LAYOUTS = {"wired": WIRED, "inline": INLINE, "merge-dir": MERGE_DIR, "package": PACKAGE}


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_every_wiring_resolves_to_the_file_that_holds_the_helper(tmp_path: Path, layout: str) -> None:
    """The resolver's half: all four wirings name a file that exists and has the entry."""
    base = _layout(tmp_path, LAYOUTS[layout])
    files = readable_domain_files(base, "input_number", "input_number.yaml")

    assert files, f"{layout}: nothing resolved"
    for entry in files:
        assert entry.path.is_file(), f"{layout}: resolved {entry.path.name}, which does not exist"


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
async def test_every_wiring_is_listed_and_readable(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, layout: str
) -> None:
    """The defect, once per wiring: three of these four listed nothing and 404'd."""
    _layout(config_dir, LAYOUTS[layout])

    listing = await client.get("/v1/config/helpers?domain=input_number", headers=auth_headers)
    assert listing.status == 200, await listing.text()
    ids = [helper["id"] for helper in (await listing.json())["helpers"]]
    assert "outdoor_temp" in ids, f"{layout}: `helper ls` shows {ids}"

    shown = await client.get("/v1/config/helper?id=outdoor_temp", headers=auth_headers)
    assert shown.status == 200, f"{layout}: {await shown.text()}"
    body = await shown.json()
    assert body["domain"] == "input_number"
    assert body["source"] == "yaml"
    assert "Outdoor Temp" in body["content"]


@pytest.mark.parametrize("layout", ["inline", "package"])
async def test_a_write_to_a_nested_definition_is_refused_by_naming_the_file(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, layout: str
) -> None:
    """The read reaches further than the write can follow, and says so.

    409 rather than 404: the helper exists — the read half just returned it —
    and a bare "not found" from the write half of the same family is the
    read/write contradiction #104 was, the other way round. The message has to
    name the file, because the caller's next move is to open it.
    """
    _layout(config_dir, LAYOUTS[layout])
    body = "outdoor_temp:\n  name: Renamed\n  min: 0\n  max: 10\n"

    for method, kwargs in (("put", {"data": body}), ("delete", {})):
        resp = await getattr(client, method)(
            "/v1/config/helper?id=outdoor_temp&domain=input_number", headers=auth_headers, **kwargs
        )
        assert resp.status == 409, f"{layout} {method}: {resp.status} {await resp.text()}"
        text = await resp.text()
        expected_file = "configuration.yaml" if layout == "inline" else "climate.yaml"
        assert expected_file in text, f"{layout} {method}: refusal does not name {expected_file}: {text}"
        assert "PUT /v1/config/file" in text, "the refusal has to name the route that CAN do it"


async def test_a_refused_write_changes_nothing_on_disk(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """A refusal that has already edited the file is not a refusal."""
    _layout(config_dir, INLINE)
    before = (config_dir / "configuration.yaml").read_text(encoding="utf-8")

    await client.delete("/v1/config/helper?id=outdoor_temp&domain=input_number", headers=auth_headers)

    assert (config_dir / "configuration.yaml").read_text(encoding="utf-8") == before


async def test_a_helper_in_a_merge_dir_member_is_still_writable(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """The boundary is the SHAPE, not the wiring.

    A member of `!include_dir_merge_named` has the domain's mapping at its root,
    exactly like an `!include` target, so splicing one entry is as safe there.
    Refusing it would be the create path's constraint leaking again — the whole
    of #105 — just one file further out.
    """
    _layout(config_dir, MERGE_DIR)

    resp = await client.put(
        "/v1/config/helper?id=outdoor_temp&domain=input_number&dry_run=false",
        headers=auth_headers,
        data="outdoor_temp:\n  name: Renamed\n  min: -20\n  max: 45\n",
    )
    assert resp.status == 200, await resp.text()
    assert "Renamed" in (config_dir / "numbers" / "outdoor.yaml").read_text(encoding="utf-8")


def test_the_unresolved_directory_includes_are_declared_not_silent(tmp_path: Path) -> None:
    """`!include_dir_named` and `!include_dir_list` take the entry's identity from
    the FILE — its name, or its position — not from anything in the document.

    That is a third entry model, and inventing an id from a filename is exactly
    what #104 was. So they are not resolved, they are RECORDED as not resolved,
    with the reason; the domain reads as unwired rather than as wrong.
    """
    assert set(UNRESOLVED_DIR_TAGS) == {"!include_dir_named", "!include_dir_list"}
    for tag, reason in UNRESOLVED_DIR_TAGS.items():
        assert len(reason) > 25, f"{tag}: {reason!r} is not a reason"

    (tmp_path / "configuration.yaml").write_text("input_number: !include_dir_named numbers\n", encoding="utf-8")
    (tmp_path / "numbers").mkdir()
    (tmp_path / "numbers" / "outdoor_temp.yaml").write_text("name: Outdoor Temp\n", encoding="utf-8")

    files = readable_domain_files(tmp_path, "input_number", "input_number.yaml")
    assert [entry.path.name for entry in files] == ["input_number.yaml"], (
        "an unresolved directory include must fall back to the conventional name, not guess entry ids from file names"
    )


def test_a_domain_wired_twice_is_not_counted_twice(tmp_path: Path) -> None:
    """HA matches `<domain> <label>` as well as the bare key, so the same file can
    be named by two keys. Reading it twice would list every helper in it twice.
    """
    (tmp_path / "configuration.yaml").write_text(
        "input_number: !include shared.yaml\ninput_number legacy: !include shared.yaml\n", encoding="utf-8"
    )
    (tmp_path / "shared.yaml").write_text(HELPER[2:], encoding="utf-8")

    files = readable_domain_files(tmp_path, "input_number", "input_number.yaml")
    assert len(files) == 1, f"resolved {[e.path.name for e in files]}"


def test_a_directory_include_is_walked_recursively(tmp_path: Path) -> None:
    """HA's `_find_files` uses `os.walk`, so a nested file is real config.

    Read from `annotatedyaml.loader` in the image the rig pulls rather than
    assumed — the same source also settles that dot-prefixed names and
    `secrets.yaml` are skipped, which the two assertions below pin.
    """
    (tmp_path / "configuration.yaml").write_text("input_number: !include_dir_merge_named numbers\n", encoding="utf-8")
    (tmp_path / "numbers" / "nested").mkdir(parents=True)
    (tmp_path / "numbers" / "nested" / "deep.yaml").write_text(HELPER[2:], encoding="utf-8")
    (tmp_path / "numbers" / "secrets.yaml").write_text("secret: value\n", encoding="utf-8")
    (tmp_path / "numbers" / ".hidden.yaml").write_text("hidden: true\n", encoding="utf-8")

    names = [entry.path.name for entry in readable_domain_files(tmp_path, "input_number", "input_number.yaml")]
    assert names == ["deep.yaml"], f"resolved {names}"
