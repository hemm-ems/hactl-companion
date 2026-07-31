"""Tests for `companion.wiring` — proving HA reads a file before writing to it (C-10)."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient

from companion.wiring import NotWiredError, domain_keys, wired_target, wired_target_or_default


def _cfg(tmp_path: Path, text: str) -> Path:
    (tmp_path / "configuration.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def test_bare_key_resolves_to_the_included_file(tmp_path: Path) -> None:
    _cfg(tmp_path, "template: !include template.yaml\n")
    assert wired_target(tmp_path, "template", "template.yaml") == (tmp_path / "template.yaml").resolve()


def test_follows_the_include_rather_than_the_conventional_name(tmp_path: Path) -> None:
    """`template: !include my_templates.yaml` means HA reads *that* file.

    Writing to the conventional `template.yaml` because it is the usual name
    would produce a file HA never reads — D46 again, one level down. The whole
    route family resolves through here so a create and the list that follows it
    cannot disagree about which file is real.
    """
    _cfg(tmp_path, "template: !include my_templates.yaml\n")
    assert wired_target(tmp_path, "template", "template.yaml") == (tmp_path / "my_templates.yaml").resolve()


def test_labelled_domain_key_counts_as_wiring(tmp_path: Path) -> None:
    """HA's extract_domain_configs matches `<domain> <label>`, so this is real config.

    Verified against a live instance in
    tests/integration/test_live.py::TestIncludeWiring::test_labelled_domain_key_is_live_config.
    A guard that only accepted the bare key would refuse a working setup, and a
    guard that refuses working setups gets deleted.
    """
    _cfg(tmp_path, "automation ui: !include automations.yaml\n")
    assert domain_keys({"automation ui": 1, "automation": 2, "automations": 3}, "automation") == [
        "automation ui",
        "automation",
    ]
    assert wired_target(tmp_path, "automation", "automations.yaml") == (tmp_path / "automations.yaml").resolve()


def test_similarly_named_domain_is_not_wiring(tmp_path: Path) -> None:
    """`automations:` (plural) is a different key and must not satisfy the guard."""
    _cfg(tmp_path, "automations: !include automations.yaml\n")
    with pytest.raises(NotWiredError, match="no top-level 'automation:' key"):
        wired_target(tmp_path, "automation", "automations.yaml")


def test_missing_key_is_refused(tmp_path: Path) -> None:
    _cfg(tmp_path, "automation: !include automations.yaml\n")
    with pytest.raises(NotWiredError, match=r"never reads template\.yaml"):
        wired_target(tmp_path, "template", "template.yaml")


def test_inline_definition_is_refused(tmp_path: Path) -> None:
    _cfg(tmp_path, "template:\n  - sensor:\n      - name: Inline\n")
    with pytest.raises(NotWiredError, match="inline"):
        wired_target(tmp_path, "template", "template.yaml")


def test_include_dir_is_refused_with_the_tag_named(tmp_path: Path) -> None:
    _cfg(tmp_path, "automation: !include_dir_merge_list automations/\n")
    with pytest.raises(NotWiredError, match="!include_dir_merge_list"):
        wired_target(tmp_path, "automation", "automations.yaml")


def test_several_includes_are_refused_as_ambiguous(tmp_path: Path) -> None:
    """Two labelled keys, neither the conventional file: guessing is not allowed.

    Picking one would put the entry in a file the user did not choose. The
    refusal names both so the caller can write the one they meant with
    PUT /v1/config/file.
    """
    _cfg(tmp_path, "automation ui: !include ui_autos.yaml\nautomation manual: !include manual_autos.yaml\n")
    with pytest.raises(NotWiredError, match="ambiguous"):
        wired_target(tmp_path, "automation", "automations.yaml")


def test_conventional_file_wins_when_several_are_wired(tmp_path: Path) -> None:
    """Ambiguity only when there is nothing to prefer — the conventional name is a tie-break."""
    _cfg(tmp_path, "automation ui: !include automations.yaml\nautomation manual: !include manual_autos.yaml\n")
    assert wired_target(tmp_path, "automation", "automations.yaml") == (tmp_path / "automations.yaml").resolve()


def test_include_outside_the_config_dir_is_refused(tmp_path: Path) -> None:
    """The include target is user-authored input and gets the same containment check as a query param (C-3)."""
    _cfg(tmp_path, "template: !include ../escape.yaml\n")
    with pytest.raises(NotWiredError, match="outside the config directory"):
        wired_target(tmp_path, "template", "template.yaml")


def test_include_of_a_denied_file_is_refused(tmp_path: Path) -> None:
    _cfg(tmp_path, "template: !include secrets.yaml\n")
    with pytest.raises(NotWiredError, match="denied"):
        wired_target(tmp_path, "template", "template.yaml")


def test_packages_mechanism_is_named_in_the_refusal(tmp_path: Path) -> None:
    """The blind spot must be stated, not silently mis-advised.

    A domain configured from inside a package file has no top-level key, so the
    plain refusal would tell the user to add an `!include` they may not want.
    """
    _cfg(tmp_path, "homeassistant:\n  packages: !include_dir_named packages\n")
    with pytest.raises(NotWiredError, match="packages"):
        wired_target(tmp_path, "template", "template.yaml")


def test_unparseable_configuration_is_refused_not_raised_raw(tmp_path: Path) -> None:
    """A broken configuration.yaml must not escape as a YAML exception.

    It reaches this module from *read* routes too, and a raw ParserError there
    is a 500 on `GET /v1/config/scripts` — a route that never had to read
    configuration.yaml at all before the guard existed.
    """
    _cfg(tmp_path, "template: [unclosed\n")
    with pytest.raises(NotWiredError, match="could not be read as YAML"):
        wired_target(tmp_path, "template", "template.yaml")


def test_missing_configuration_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NotWiredError, match="not found"):
        wired_target(tmp_path, "template", "template.yaml")


@pytest.mark.parametrize(
    "config_text",
    [
        "template: [unclosed\n",
        "automation: !include automations.yaml\n",
        "",
    ],
    ids=["unparseable", "domain-absent", "empty"],
)
def test_read_paths_fall_back_instead_of_failing(tmp_path: Path, config_text: str) -> None:
    """Read/update/delete must keep working on a config the create path refuses.

    Refusing them would strand a user who needs to inspect or clean up a file HA
    ignores — and the fallback has to be the conventional name, which is the
    file such an entry is actually in.
    """
    _cfg(tmp_path, config_text)
    assert wired_target_or_default(tmp_path, "template", "template.yaml") == (tmp_path / "template.yaml").resolve()


async def test_broken_configuration_does_not_break_reading_scripts(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """Regression: routing reads through the guard must not add a new failure mode.

    Before the guard, `GET /v1/config/scripts` never opened configuration.yaml.
    Routing it through wiring resolution made a malformed configuration.yaml
    raise ruamel's ParserError out of the handler — a 500 on a read that used to
    work.
    """
    (config_dir / "configuration.yaml").write_text("script: [unclosed\n", encoding="utf-8")

    resp = await client.get("/v1/config/scripts", headers=auth_headers)
    assert resp.status == 200, f"answered {resp.status}: {await resp.text()}"
    assert (await resp.json())["scripts"], "fell back to the wrong file — scripts.yaml has entries"


async def test_create_refusal_reaches_the_caller_as_a_json_envelope(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """C-8: the refusal is an error envelope, not a bare text body."""
    (config_dir / "configuration.yaml").write_text("automation: !include automations.yaml\n", encoding="utf-8")

    resp = await client.post(
        "/v1/config/template?domain=sensor",
        data='name: "New"\nunique_id: tpl_probe\nstate: "{{ 1 }}"\n',
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 400
    assert resp.content_type == "application/json"
    body = await resp.json()
    assert body["error"]["code"] == 400
    assert "template: !include template.yaml" in body["error"]["message"]


def test_conventional_fallback_is_contained_too(tmp_path: Path) -> None:
    """C-3 holds for the fallback name, not only for the `!include` target.

    The include branch was guarded from the start; the conventional-name branch
    was not, because at the call sites people look at it is a literal
    (`automations.yaml`). The helper routes build theirs from the `?domain=`
    query parameter, so "it is a literal" was true of some callers and not of
    the class — the shape of every C-3 hole this project has had.
    """
    (tmp_path / "configuration.yaml").write_text("homeassistant:\n  name: Home\n", encoding="utf-8")

    with pytest.raises(NotWiredError):
        wired_target_or_default(tmp_path, "automation", "../outside.yaml")
    with pytest.raises(NotWiredError):
        wired_target_or_default(tmp_path, "automation", "secrets.yaml")

    # The guard still lets an ordinary name through — a check that refuses
    # everything is not a check.
    assert wired_target_or_default(tmp_path, "automation", "automations.yaml") == (tmp_path / "automations.yaml")
