"""`GET /v1/config/wiring` — the create-time layout check, askable in advance.

`tests/test_wiring.py` proves the *rule*. This proves the rule is reachable
without attempting a write, which is what a client needs to preview one: the
verdict-equality sweep lives in `test_invariants.py` (quantified over every
create route), and these are the shape and edge cases that sweep does not see.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient

from companion.routes.helpers import ALLOWED_DOMAINS
from companion.routes.wiring import CONVENTIONAL_FILES


def test_every_creatable_domain_can_be_probed() -> None:
    """Canary: a create route whose domain is unprobeable leaves a preview blind.

    The helper domains are the ones that made this necessary — eight of them,
    all previewing successfully on an instance where all eight creates failed.
    """
    assert CONVENTIONAL_FILES.keys() >= ALLOWED_DOMAINS, (
        f"helper domains missing from the probe table: {sorted(ALLOWED_DOMAINS - CONVENTIONAL_FILES.keys())}"
    )
    assert {"automation", "script", "template"} <= CONVENTIONAL_FILES.keys()


@pytest.mark.parametrize(
    ("domain", "expected_file"),
    [("automation", "automations.yaml"), ("script", "scripts.yaml"), ("input_boolean", "input_boolean.yaml")],
)
async def test_wired_domain_reports_the_file(
    client: TestClient, auth_headers: dict[str, str], domain: str, expected_file: str
) -> None:
    resp = await client.get(f"/v1/config/wiring?domain={domain}", headers=auth_headers)
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body == {"domain": domain, "wired": True, "file": expected_file}


async def test_it_reports_the_file_the_include_names_not_the_conventional_one(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """Following the include is the whole point — the name is only a default."""
    config = config_dir / "configuration.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "input_boolean: !include input_boolean.yaml", "input_boolean: !include helpers/booleans.yaml"
        ),
        encoding="utf-8",
    )
    resp = await client.get("/v1/config/wiring?domain=input_boolean", headers=auth_headers)
    assert (await resp.json())["file"] == "helpers/booleans.yaml"


async def test_a_container_path_never_reaches_the_caller(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """The path is config-relative: the caller has no idea where this add-on mounts /config."""
    body = await (await client.get("/v1/config/wiring?domain=script", headers=auth_headers)).json()
    assert not body["file"].startswith("/"), body
    assert str(config_dir) not in body["file"], body


@pytest.mark.parametrize(
    ("layout", "needle"),
    [
        ("input_boolean:\n  inline_flag:\n    name: Inline\n", "inline"),
        ("input_boolean: !include_dir_merge_named booleans/\n", "!include_dir_merge_named"),
        ("", "no top-level 'input_boolean:' key"),
    ],
    ids=["inline", "include-dir", "absent"],
)
async def test_unwired_layouts_answer_200_with_the_reason(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, layout: str, needle: str
) -> None:
    """Not-wired is an answer, not an error — a preview must not scrape an error envelope."""
    config = config_dir / "configuration.yaml"
    kept = [
        line
        for line in config.read_text(encoding="utf-8").splitlines(keepends=True)
        if not line.startswith("input_boolean")
    ]
    config.write_text("".join(kept) + layout, encoding="utf-8")

    resp = await client.get("/v1/config/wiring?domain=input_boolean", headers=auth_headers)
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["wired"] is False
    assert needle in body["reason"], body["reason"]
    assert "file" not in body, "a refusal must not name a file a create would not write to"


async def test_unknown_domain_is_a_400_that_lists_what_can_be_asked(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.get("/v1/config/wiring?domain=light", headers=auth_headers)
    assert resp.status == 400
    message = (await resp.json())["error"]["message"]
    assert "input_boolean" in message and "template" in message


async def test_missing_domain_is_a_400(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = await client.get("/v1/config/wiring", headers=auth_headers)
    assert resp.status == 400


async def test_probing_never_writes(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    """It answers the create's question without doing any part of the create."""
    before = {str(p.relative_to(config_dir)): p.read_bytes() for p in config_dir.rglob("*") if p.is_file()}
    for domain in sorted(CONVENTIONAL_FILES):
        assert (await client.get(f"/v1/config/wiring?domain={domain}", headers=auth_headers)).status == 200
    after = {str(p.relative_to(config_dir)): p.read_bytes() for p in config_dir.rglob("*") if p.is_file()}
    assert after == before
