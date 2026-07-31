"""Reading the helpers Home Assistant's UI creates (`source: storage`).

`GET /v1/config/helper` searched the YAML helper files and nothing else, so on
any instance whose helpers were made in the UI — the normal way, and on the
instance that produced this defect *all 220 of them* — it answered 404 for
every helper `GET /v1/config/helpers` and hactl's `helper ls` happily listed.
A read surface that contradicts the listing beside it is worse than a missing
feature: "Helper not found" reads as "that entity does not exist".

The write half stays YAML-only (there is no YAML definition to edit), but it
must say *that*, not repeat the 404 the read no longer gives.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient
from ruamel.yaml import YAML

from companion.routes.helpers import ALLOWED_DOMAINS, STORAGE_DOMAINS
from tests.storage_fixture import STORAGE_ITEMS, rename_in_registry, seed_storage_helpers

yaml = YAML(typ="safe")


def test_every_readable_domain_has_a_fixture() -> None:
    """Canary: a new helper domain must arrive with the storage shape HA writes for it."""
    assert set(STORAGE_ITEMS) == STORAGE_DOMAINS, (
        f"storage fixture covers {sorted(STORAGE_ITEMS)} but the read surface is {sorted(STORAGE_DOMAINS)} — "
        "add the domain's collection item (copied from a live instance) or narrow STORAGE_DOMAINS"
    )
    assert STORAGE_DOMAINS > ALLOWED_DOMAINS, (
        "the readable set must be wider than the writable one — input_button has no YAML form and would "
        "otherwise be listed by `helper ls` and unreadable here"
    )


@pytest.mark.parametrize("domain", sorted(STORAGE_ITEMS))
async def test_storage_helper_is_readable_by_entity_id(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, domain: str
) -> None:
    """The defect, one case per domain: every one of these used to be a 404."""
    item = seed_storage_helpers(config_dir)[domain]
    entity_id = f"{domain}.{item['id']}"

    resp = await client.get(f"/v1/config/helper?id={entity_id}", headers=auth_headers)
    assert resp.status == 200, f"{entity_id}: {await resp.text()}"
    body = await resp.json()
    assert body["id"] == entity_id
    assert body["domain"] == domain
    assert body["source"] == "storage"

    parsed = yaml.load(body["content"])
    assert parsed == {item["id"]: {k: v for k, v in item.items() if k != "id"}}, (
        "the rendered definition must be the collection item, keyed like a YAML helper"
    )


@pytest.mark.parametrize("domain", sorted(STORAGE_ITEMS))
async def test_storage_helper_is_readable_by_collection_id(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, domain: str
) -> None:
    """The bare id is what the equivalent YAML helper's key would be — it must resolve too."""
    item = seed_storage_helpers(config_dir, [domain])
    resp = await client.get(f"/v1/config/helper?id={item[domain]['id']}", headers=auth_headers)
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["source"] == "storage"


async def test_storage_content_carries_the_read_only_marker(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """`helper cat` prints content verbatim, so the marker has to be inside it.

    A comment is valid YAML: the output still parses and can still be piped into
    a file, but nothing reading it can mistake a UI-managed helper for one this
    service can edit.
    """
    seed_storage_helpers(config_dir, ["input_boolean"])
    resp = await client.get("/v1/config/helper?id=input_boolean.probe_bool", headers=auth_headers)
    content = (await resp.json())["content"]

    assert content.startswith("# source: storage"), content
    assert yaml.load(content) == {"probe_bool": {"name": "Probe Bool", "icon": "mdi:toggle-switch"}}


async def test_yaml_helper_still_reads_as_yaml_and_verbatim(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """The other half: a file-backed helper keeps its old answer, plus an honest source.

    Its content is the file's own bytes — adding the storage marker there would
    misrepresent what is on disk.
    """
    seed_storage_helpers(config_dir)
    resp = await client.get("/v1/config/helper?id=guest_mode", headers=auth_headers)
    assert resp.status == 200
    body = await resp.json()
    assert body["id"] == "guest_mode"
    assert body["domain"] == "input_boolean"
    assert body["source"] == "yaml"
    assert "# source:" not in body["content"]


async def test_entity_id_comes_from_the_registry_not_from_the_item_id(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """A renamed helper is still one helper, under the name HA now gives it.

    `<domain>.<item id>` is a guess that a rename invalidates; the registry is
    the fact. Both names resolve — hactl printed the entity_id, and an
    identifier printed is an identifier accepted (H-17) — and the answer reports
    the live one.
    """
    seed_storage_helpers(config_dir, ["input_boolean"])
    rename_in_registry(config_dir, "input_boolean", "probe_bool", "input_boolean.renamed_by_user")

    for reference in ("input_boolean.renamed_by_user", "input_boolean.probe_bool", "probe_bool"):
        resp = await client.get(f"/v1/config/helper?id={reference}", headers=auth_headers)
        assert resp.status == 200, f"{reference}: {await resp.text()}"
        assert (await resp.json())["id"] == "input_boolean.renamed_by_user", f"{reference} reported a stale name"


async def test_ambiguous_bare_id_across_storage_domains_is_refused(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """Same rule the YAML branch already follows: name the candidates, act on neither."""
    seed_storage_helpers(config_dir, ["input_boolean", "counter"])
    (config_dir / ".storage" / "input_boolean").write_text(
        '{"version":1,"key":"input_boolean","data":{"items":[{"id":"kitchen","name":"K"}]}}', encoding="utf-8"
    )
    (config_dir / ".storage" / "counter").write_text(
        '{"version":1,"key":"counter","data":{"items":[{"id":"kitchen","initial":0}]}}', encoding="utf-8"
    )

    resp = await client.get("/v1/config/helper?id=kitchen", headers=auth_headers)
    assert resp.status == 409, await resp.text()
    message = (await resp.json())["error"]["message"]
    assert "counter" in message and "input_boolean" in message


async def test_a_genuinely_absent_helper_says_where_it_looked(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """The negative control — and the 404 must not read as 'that entity does not exist'."""
    seed_storage_helpers(config_dir)
    resp = await client.get("/v1/config/helper?id=input_boolean.nothing_like_this", headers=auth_headers)
    assert resp.status == 404
    message = (await resp.json())["error"]["message"]
    assert ".storage" in message and "input_boolean.yaml" in message


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
async def test_write_routes_refuse_a_storage_helper_by_naming_the_mechanism(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, method: str
) -> None:
    """409, not 404: the read resolves it, so the write must explain, not deny.

    This is the half a fix like this normally forgets. hactl's `helper delete`
    preview resolves its target through GET — teaching GET to find storage
    helpers without teaching DELETE to refuse them would have turned a correct
    "not found" preview into a plan for a delete that cannot happen (H-2).
    """
    seed_storage_helpers(config_dir, ["input_boolean"])
    kwargs: dict[str, object] = {"headers": dict(auth_headers)}
    if method == "PUT":
        kwargs["data"] = "name: Renamed\n"
        kwargs["headers"] = {**auth_headers, "Content-Type": "text/plain"}

    resp = await client.request(method, "/v1/config/helper?id=input_boolean.probe_bool", **kwargs)
    assert resp.status == 409, await resp.text()
    message = (await resp.json())["error"]["message"]
    assert "storage" in message and "UI" in message

    stored = (config_dir / ".storage" / "input_boolean").read_text(encoding="utf-8")
    assert "Probe Bool" in stored, "refused the write but modified HA's own storage anyway"


async def test_input_button_is_readable_but_not_creatable(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """The read/write reach differ on purpose, and each says so in its own voice."""
    seed_storage_helpers(config_dir, ["input_button"])

    resp = await client.get("/v1/config/helper?id=input_button.probe_button", headers=auth_headers)
    assert resp.status == 200, await resp.text()

    resp = await client.post(
        "/v1/config/helper?domain=input_button",
        data="new_button:\n  name: New\n",
        headers={**auth_headers, "Content-Type": "text/plain"},
    )
    assert resp.status == 400
    assert "Invalid helper domain" in (await resp.json())["error"]["message"]


async def test_unreadable_storage_degrades_to_no_storage_helpers(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    """`.storage` belongs to HA: a half-written or future-schema file is not a 500.

    The read routes reached this code for the first time in this change; a
    crash here would break lookups that never touched `.storage` at all.
    """
    storage = config_dir / ".storage"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "input_boolean").write_text("{ not json", encoding="utf-8")
    (storage / "core.entity_registry").write_text('{"data": {"entities": "not a list"}}', encoding="utf-8")

    resp = await client.get("/v1/config/helper?id=input_boolean.probe_bool", headers=auth_headers)
    assert resp.status == 404, await resp.text()

    resp = await client.get("/v1/config/helper?id=guest_mode", headers=auth_headers)
    assert resp.status == 200, "a broken .storage must not hide the YAML helpers"
