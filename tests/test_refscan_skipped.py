"""A config walk says which files it did not read (`skipped`).

The ref routes walk the `!include` graph and step over any file they cannot
read. That is the right behaviour and stays: the commonest such file is
`secrets.yaml`, which the path guard refuses on a perfectly healthy instance,
and aborting there would make the endpoints useless. What was missing is the
*record*. A caller got `200 OK` and a complete-looking answer, so:

* `hactl ref validate --exit-code` — whose whole job is to certify that a config
  has no dangling references — could exit 0 over a config half it never saw. It
  was just hardened to refuse when a dashboard, the entity registry or the live
  states could not be read; the config-file half was the one source that had no
  way to tell it anything had gone unread.
* `hactl ref replace --confirm` is worse still: an unread file keeps the old
  entity id while `status: applied` and a non-empty `changes` list report the
  rename as done — a dangling pointer behind a success message.

Three properties are load-bearing and are asserted separately:

* a skipped file is **named**, with a reason, on every route that walks;
* on the write path the caller can tell the rename was **partial** — changes and
  skips in the same response;
* when nothing was skipped the field is **absent** — not empty, not null — so a
  complete scan's response is byte-identical to the one this service sent before
  the field existed, and no existing consumer sees a change. Asserted on raw
  bytes, following `test_reload_error.py`.

The structural half (every ref route documents the field, and a probe reaches
the branch that emits it) is swept by `test_spec_conformance.py`'s `skipped`
probes. What this module adds is the content of the record and the behaviour
around it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient
from ruamel.yaml.error import YAMLError

from companion.refscan import (
    SKIP_MISSING,
    SKIP_UNREADABLE,
    SkipLog,
    replace_yaml_literal,
    scan_yaml_for_entities,
    scan_yaml_for_literal,
    skipped_fields,
)

_JSON = {"Content-Type": "application/json"}


def _write(config_dir: Path, name: str, text: str) -> Path:
    path = config_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _only_config(config_dir: Path, text: str) -> None:
    """Reduce the fixture tree to one file, so a response can be asserted whole.

    The other fixtures stay on disk but leave the config graph: nothing is
    reachable from `configuration.yaml` any more, and reachability is what the
    walk follows.
    """
    _write(config_dir, "configuration.yaml", text)


# ---------------------------------------------------------------------------
# (a) an `!include` naming a file that is not there
# ---------------------------------------------------------------------------


async def test_scan_names_the_include_target_it_could_not_read(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """The renamed-`!include` case: the hit list is short, and the response says why.

    `configuration.yaml` still yields its hit, which is the point — the walk is
    not abandoned, it is annotated.
    """
    _only_config(config_dir, "automation: !include packages/renamed.yaml\nsensor:\n  value: sensor.gone\n")

    resp = await client.get("/v1/ref/scan?target=sensor.gone", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert data["hits"] == [{"location": "configuration.yaml", "path": "sensor.value", "matched_value": "sensor.gone"}]
    assert data["skipped"] == [{"location": "packages/renamed.yaml", "reason": "missing"}]


async def test_entities_names_the_include_target_it_could_not_read(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """The route `ref validate` builds its verdict on.

    An entity referenced only from the unread file is absent from `entities`,
    and absence is exactly what a dangling-reference check reads as "fine".
    """
    _only_config(config_dir, "automation: !include packages/renamed.yaml\nsensor:\n  value: sensor.gone\n")

    resp = await client.get("/v1/ref/entities", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert [ref["location"] for ref in data["entities"]] == ["configuration.yaml"]
    assert data["skipped"] == [{"location": "packages/renamed.yaml", "reason": "missing"}]


async def test_scan_names_the_include_directory_that_is_not_there(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """An `!include_dir_*` whose directory was renamed away.

    Worth its own case because it is invisible one level earlier than a missing
    file: `include_dir_files` answers with an empty list, which is
    indistinguishable from a directory that really is empty — C-11's confusion,
    one level down from the tag. A whole split automation directory can leave
    the config graph this way.
    """
    _only_config(config_dir, "automation: !include_dir_merge_list autos_renamed\nsensor:\n  value: sensor.gone\n")

    resp = await client.get("/v1/ref/scan?target=sensor.gone", headers=auth_headers)

    assert resp.status == 200
    assert (await resp.json())["skipped"] == [{"location": "autos_renamed", "reason": "missing"}]


# ---------------------------------------------------------------------------
# (b) a file the walk is refused
# ---------------------------------------------------------------------------


async def test_entities_names_a_file_the_path_guard_refuses(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """`!include secrets.yaml` — the case that fires on a healthy instance.

    C-3 forbids ever reading it, so the skip itself is correct and permanent.
    Reporting it is what lets a caller distinguish "your config is fully
    scanned" from "your config is fully scanned except the file I am never
    allowed to open".
    """
    _only_config(config_dir, "leak: !include secrets.yaml\nsensor:\n  value: sensor.gone\n")
    _write(config_dir, "secrets.yaml", "token: sensor.hidden\n")

    resp = await client.get("/v1/ref/entities", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert data["skipped"] == [{"location": "secrets.yaml", "reason": "unreadable"}]
    # And the refusal is real: nothing from the file leaked into the answer.
    assert all(ref["matched_value"] != "sensor.hidden" for ref in data["entities"])


async def test_scan_names_an_include_that_leaves_the_config_directory(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """`!include ../outside.yaml` is refused by containment (C-3) — and now said so."""
    _only_config(config_dir, "sensor: !include ../outside.yaml\n")
    (config_dir.parent / "outside.yaml").write_text("x: sensor.gone\n", encoding="utf-8")

    resp = await client.get("/v1/ref/scan?target=sensor.gone", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert data["hits"] == []
    assert data["skipped"] == [{"location": "outside.yaml", "reason": "unreadable"}]


# ---------------------------------------------------------------------------
# The write path: a partial rename must be visible as partial
# ---------------------------------------------------------------------------


async def test_replace_reports_the_file_it_could_not_rewrite(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """`applied` plus changes plus skips: the rename went through *where it could*.

    Without the last part this response is a success message over a config that
    still holds the old entity id in a file nobody looked at.
    """
    _only_config(
        config_dir,
        "automation: !include automations.yaml\npackages: !include packages/renamed.yaml\n",
    )
    autos = _write(config_dir, "automations.yaml", "- trigger:\n    - entity_id: sensor.gone\n")

    resp = await client.post(
        "/v1/ref/replace",
        headers={**auth_headers, **_JSON},
        data=json.dumps({"old": "sensor.gone", "new": "sensor.new", "dry_run": False}),
    )

    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "applied"
    assert [change["location"] for change in data["changes"]] == ["automations.yaml"]
    assert data["skipped"] == [{"location": "packages/renamed.yaml", "reason": "missing"}]
    # The rewrite that *was* possible still happened.
    assert "sensor.new" in autos.read_text(encoding="utf-8")


def test_replace_never_writes_a_file_it_recorded_as_skipped(config_dir: Path) -> None:
    """The record is not a licence to touch the file: `secrets.yaml` is untouched."""
    _only_config(config_dir, "leak: !include secrets.yaml\nsensor:\n  value: sensor.gone\n")
    secrets = _write(config_dir, "secrets.yaml", "token: sensor.gone\n")

    skipped = SkipLog()
    changes = replace_yaml_literal(config_dir, "sensor.gone", "sensor.new", dry_run=False, skipped=skipped)

    assert [change["location"] for change in changes] == ["configuration.yaml"]
    assert [(f.location, f.reason) for f in skipped.files()] == [("secrets.yaml", SKIP_UNREADABLE)]
    assert secrets.read_text(encoding="utf-8") == "token: sensor.gone\n"


# ---------------------------------------------------------------------------
# (c) a normal tree — the field must be ABSENT, asserted on raw bytes
# ---------------------------------------------------------------------------


async def test_scan_response_is_unchanged_when_nothing_was_skipped(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """Asserted on the raw bytes, not the decoded dict.

    The claim is that a consumer written against the previous release cannot
    tell this build from it, and a `"skipped": []` or a `"skipped": null` would
    break that — both are what an implementation reaches for by default.
    """
    _only_config(config_dir, "sensor:\n  value: sensor.gone\n")

    resp = await client.get("/v1/ref/scan?target=sensor.gone", headers=auth_headers)

    assert resp.status == 200
    assert await resp.text() == (
        '{"target": "sensor.gone", "hits": [{"location": "configuration.yaml", '
        '"path": "sensor.value", "matched_value": "sensor.gone"}]}'
    )


async def test_entities_response_is_unchanged_when_nothing_was_skipped(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """The same, for the route `ref validate` reads."""
    _only_config(config_dir, "sensor:\n  value: sensor.gone\n")

    resp = await client.get("/v1/ref/entities", headers=auth_headers)

    assert resp.status == 200
    assert await resp.text() == (
        '{"entities": [{"location": "configuration.yaml", "path": "sensor.value", '
        '"key": "value", "matched_value": "sensor.gone"}]}'
    )


async def test_replace_response_is_unchanged_when_nothing_was_skipped(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """The same, on the write path."""
    _only_config(config_dir, "sensor:\n  value: sensor.gone\n")

    resp = await client.post(
        "/v1/ref/replace",
        headers={**auth_headers, **_JSON},
        data=json.dumps({"old": "sensor.gone", "new": "sensor.new", "dry_run": True}),
    )

    assert resp.status == 200
    assert await resp.text() == (
        '{"status": "dry_run", "changes": [{"location": "configuration.yaml", '
        '"path": "sensor.value", "before": "sensor.gone", "after": "sensor.new"}]}'
    )


async def test_the_whole_fixture_config_is_scanned_without_a_skip(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """A realistic multi-file config — `!include` and `!include_dir_named` — skips nothing.

    The absent-field tests above each reduce the graph to one file. This one
    keeps the full fixture tree, so a walk that started reporting phantom skips
    on ordinary configs (which would make every consumer refuse to certify
    anything) fails here rather than in the field.
    """
    resp = await client.get("/v1/ref/entities", headers=auth_headers)

    assert resp.status == 200
    data = await resp.json()
    assert "skipped" not in data
    # Non-vacuous: the walk really did cross the include graph, in both forms —
    # a plain `!include` (automations.yaml) and an `!include_dir_named`
    # (packages/), the tag whose missing-directory case is checked above.
    locations = {ref["location"] for ref in data["entities"]}
    assert "automations.yaml" in locations
    assert any(location.startswith("packages/") for location in locations), locations


# ---------------------------------------------------------------------------
# What `skipped` may and may not carry
# ---------------------------------------------------------------------------


async def test_skipped_leaks_neither_file_contents_nor_a_host_path(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """A location and a short reason, nothing else.

    `secrets.yaml` is the reason this matters: the file the walk is most often
    refused is the one whose contents must never travel, and its absolute path
    would name a directory layout the caller has no business learning from an
    error field either. `location` uses the config-relative form every other
    `location` in this API uses.
    """
    _only_config(config_dir, "leak: !include secrets.yaml\n")
    _write(config_dir, "secrets.yaml", "api_key: SUPER_SECRET_VALUE\n")

    resp = await client.get("/v1/ref/entities", headers=auth_headers)

    body = await resp.text()
    entry = (await resp.json())["skipped"][0]
    assert set(entry) == {"location", "reason"}
    assert entry["location"] == "secrets.yaml"
    assert not entry["location"].startswith("/")
    assert "SUPER_SECRET_VALUE" not in body
    assert str(config_dir) not in body


# ---------------------------------------------------------------------------
# The boundary: what is deliberately NOT a skip
# ---------------------------------------------------------------------------


def test_a_yaml_syntax_error_still_refuses_the_whole_scan(config_dir: Path) -> None:
    """Pinned deliberately: a malformed file aborts, it does not become a skip.

    `iter_config_trees` catches `FileNotFoundError`, `PermissionError`,
    `ValueError` and `CircularIncludeError`. ruamel signals a YAML syntax error
    with `YAMLError`, which is **not** a `ValueError` — so, contrary to what the
    docstring used to claim, a malformed file has always propagated out of the
    scan rather than being stepped over.

    That is left exactly as it is, and this test is here so nobody "tidies" it
    into a skip without deciding to. Turning it into one would trade a loud
    refusal for a silent partial answer for every consumer that does not yet
    read `skipped` — and hactl does not: it vendors a pinned copy of this spec.
    On `ref replace` that trade is the failure mode this whole change exists to
    prevent, in reverse: today one malformed file means nothing is rewritten,
    where a skip would mean a partial rewrite reported as complete. Whoever
    changes it must land the hactl side in the same release.
    """
    _only_config(config_dir, "automation: !include automations.yaml\nsensor:\n  value: sensor.gone\n")
    _write(config_dir, "automations.yaml", "- alias: A\n  trigger: [\n")

    skipped = SkipLog()
    with pytest.raises(YAMLError):
        scan_yaml_for_entities(config_dir, skipped=skipped)


def test_a_file_the_prefilter_proves_irrelevant_is_not_a_skip(config_dir: Path) -> None:
    """`contains=` drops files on evidence, not on failure, so it reports nothing.

    A file whose raw text holds neither the target nor an `!include` cannot
    contribute a hit and cannot extend the graph. It was read and ruled out —
    calling that a skip would make `ref scan` report a skip on nearly every
    real config and train callers to ignore the field.
    """
    _only_config(config_dir, "automation: !include automations.yaml\nsensor:\n  value: sensor.gone\n")
    _write(config_dir, "automations.yaml", "- alias: unrelated\n")

    skipped = SkipLog()
    hits = scan_yaml_for_literal(config_dir, "sensor.gone", skipped=skipped)

    assert [hit.location for hit in hits] == ["configuration.yaml"]
    assert skipped.files() == []


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------


def test_skipped_fields_is_absent_on_a_complete_walk() -> None:
    """`{}`, not `{"skipped": []}` — the whole absent-not-empty rule, decided once."""
    assert skipped_fields(None) == {}
    assert skipped_fields(SkipLog()) == {}

    log = SkipLog()
    log.record("packages/renamed.yaml", SKIP_MISSING)
    assert skipped_fields(log) == {"skipped": [{"location": "packages/renamed.yaml", "reason": SKIP_MISSING}]}


def test_the_same_missing_file_is_reported_once_however_often_it_is_reached() -> None:
    """Two includers naming one missing file is one fact about the config, not two.

    The walk does not mark a file it never read as seen, so the raw stream
    repeats — a caller would otherwise get a traversal trace where it asked for
    a list of gaps.
    """
    log = SkipLog()
    log.record("packages/renamed.yaml", SKIP_MISSING)
    log.record("packages/renamed.yaml", SKIP_MISSING)
    log.record("secrets.yaml", SKIP_UNREADABLE)

    assert [(f.location, f.reason) for f in log.files()] == [
        ("packages/renamed.yaml", SKIP_MISSING),
        ("secrets.yaml", SKIP_UNREADABLE),
    ]


def test_two_includers_of_one_missing_file_yield_one_record(config_dir: Path) -> None:
    """The same, driven through the real walk rather than the accumulator alone."""
    _only_config(config_dir, "a: !include one.yaml\nb: !include two.yaml\n")
    _write(config_dir, "one.yaml", "x: !include shared_renamed.yaml\n")
    _write(config_dir, "two.yaml", "y: !include shared_renamed.yaml\n")

    skipped = SkipLog()
    scan_yaml_for_entities(config_dir, skipped=skipped)

    assert [(f.location, f.reason) for f in skipped.files()] == [("shared_renamed.yaml", SKIP_MISSING)]
