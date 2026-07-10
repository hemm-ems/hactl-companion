"""Field-granular OpenAPI conformance tests.

The operation-level checks in ``test_openapi.py`` prove every route has a spec
entry, but nothing there validated *fields*. These tests close that gap two ways:

1. Every driven response is validated against its ``ENDPOINT_META`` response
   schema, and asserted to carry **no undocumented fields** — so a handler that
   grows a response key without updating the spec fails here.
2. Every query param a handler actually reads must be declared in the spec for
   one of that route module's endpoints.

Together these would have caught the P2-2/-3/-4 drift mechanically.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from aiohttp.test_utils import TestClient

from companion.openapi import ENDPOINT_META
from companion.routes import (
    automations,
    config,
    helpers,
    logs,
    refscan,
    related,
    scripts,
    status,
    templates,
    wireguard,
)
from tests.related_fixture import SOURCE_ENTITY_ID, seed_related_fixture


def _to_jsonschema(schema: Any) -> Any:
    """Convert an OpenAPI 3.0 schema to a plain JSON Schema jsonschema can validate.

    The only OpenAPI-ism we use is ``nullable: true`` — map it to a union type.
    """
    if isinstance(schema, dict):
        nullable = schema.get("nullable") is True
        out = {k: _to_jsonschema(v) for k, v in schema.items() if k != "nullable"}
        if nullable and isinstance(out.get("type"), str):
            out["type"] = [out["type"], "null"]
        return out
    if isinstance(schema, list):
        return [_to_jsonschema(v) for v in schema]
    return schema


def _assert_no_undocumented(payload: Any, schema: dict[str, Any], loc: str = "$") -> None:
    """Recursively assert every key present in ``payload`` is declared in ``schema``."""
    if isinstance(payload, dict) and schema.get("type") == "object":
        props = schema.get("properties")
        if props:
            for key, value in payload.items():
                assert key in props, f"{loc}: undocumented field '{key}' (spec has {sorted(props)})"
                _assert_no_undocumented(value, props[key], f"{loc}.{key}")
    elif isinstance(payload, list) and schema.get("type") == "array":
        items = schema.get("items", {})
        for i, value in enumerate(payload):
            _assert_no_undocumented(value, items, f"{loc}[{i}]")


def assert_conforms(method: str, path: str, payload: Any) -> None:
    """Validate a response payload against its spec schema, both directions."""
    schema = ENDPOINT_META[(method, path)]["response_schema"]
    assert isinstance(schema, dict)
    jsonschema.validate(payload, _to_jsonschema(schema))
    _assert_no_undocumented(payload, schema)


_TEXT = "text/plain"


async def _json(resp: Any) -> Any:
    assert resp.status < 400, f"unexpected {resp.status}: {await resp.text()}"
    return await resp.json()


# ---------------------------------------------------------------------------
# Response conformance — driven against real handlers
# ---------------------------------------------------------------------------


async def test_conformance_health_and_status(client: TestClient, auth_headers: dict[str, str]) -> None:
    assert_conforms("GET", "/v1/health", await _json(await client.get("/v1/health")))
    assert_conforms("GET", "/v1/status", await _json(await client.get("/v1/status")))


async def test_conformance_config_reads(client: TestClient, auth_headers: dict[str, str]) -> None:
    assert_conforms("GET", "/v1/config/files", await _json(await client.get("/v1/config/files", headers=auth_headers)))
    resp = await client.get("/v1/config/file?path=automations.yaml", headers=auth_headers)
    assert_conforms("GET", "/v1/config/file", await _json(resp))


async def test_conformance_config_write_dry_and_apply(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    body = "conformance_probe:\n  value: 1\n"
    dry = await client.put(
        "/v1/config/file?path=probe.yaml&dry_run=true", data=body, headers={**auth_headers, "Content-Type": _TEXT}
    )
    assert_conforms("PUT", "/v1/config/file", await _json(dry))

    apply = await client.put(
        "/v1/config/file?path=probe.yaml&dry_run=false", data=body, headers={**auth_headers, "Content-Type": _TEXT}
    )
    payload = await _json(apply)
    assert_conforms("PUT", "/v1/config/file", payload)
    assert payload["validated"] is True  # the field the spec previously omitted (P2-4)


async def test_conformance_related_normal_and_stale(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path
) -> None:
    seed_related_fixture(config_dir)
    normal = await client.get(f"/v1/related/entity?entity_id={SOURCE_ENTITY_ID}", headers=auth_headers)
    payload = await _json(normal)
    assert_conforms("GET", "/v1/related/entity", payload)
    assert payload["stale"] is False

    stale = await client.get("/v1/related/entity?entity_id=sensor.does_not_exist&stale=true", headers=auth_headers)
    stale_payload = await _json(stale)
    assert_conforms("GET", "/v1/related/entity", stale_payload)  # stale/stale_refs (P2-2)
    assert stale_payload["stale"] is True


async def test_conformance_helpers_crud(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    assert_conforms(
        "GET", "/v1/config/helpers", await _json(await client.get("/v1/config/helpers", headers=auth_headers))
    )
    assert_conforms(
        "GET",
        "/v1/config/helper",
        await _json(await client.get("/v1/config/helper?id=guest_mode", headers=auth_headers)),
    )
    created = await client.post(
        "/v1/config/helper?domain=input_boolean",
        data="probe_helper:\n  name: Probe\n",
        headers={**auth_headers, "Content-Type": _TEXT},
    )
    assert_conforms("POST", "/v1/config/helper", await _json(created))  # entity_id/reloaded/entity_created (P2-3)
    updated = await client.put(
        "/v1/config/helper?id=probe_helper",
        data="name: Probe 2\n",
        headers={**auth_headers, "Content-Type": _TEXT},
    )
    assert_conforms("PUT", "/v1/config/helper", await _json(updated))
    deleted = await client.delete("/v1/config/helper?id=probe_helper", headers=auth_headers)
    assert_conforms("DELETE", "/v1/config/helper", await _json(deleted))


async def test_conformance_template_write(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    put = await client.put(
        "/v1/config/template?id=tpl_energie_zaehler&dry_run=true",
        data='name: "Updated"\nunique_id: tpl_energie_zaehler\nstate: "{{ 1 }}"\n',
        headers={**auth_headers, "Content-Type": _TEXT},
    )
    assert_conforms("PUT", "/v1/config/template", await _json(put))
    post = await client.post(
        "/v1/config/template?domain=sensor",
        data='name: "New"\nunique_id: tpl_probe\nstate: "{{ 1 }}"\n',
        headers={**auth_headers, "Content-Type": _TEXT},
    )
    assert_conforms("POST", "/v1/config/template", await _json(post))
    delete = await client.delete("/v1/config/template?id=tpl_probe", headers=auth_headers)
    assert_conforms("DELETE", "/v1/config/template", await _json(delete))


async def test_conformance_script_write(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    put = await client.put(
        "/v1/config/script?id=welcome_home&dry_run=true",
        data="alias: Updated\nsequence:\n  - service: light.turn_on\n",
        headers={**auth_headers, "Content-Type": _TEXT},
    )
    assert_conforms("PUT", "/v1/config/script", await _json(put))
    post = await client.post(
        "/v1/config/script",
        data="probe_script:\n  alias: Probe\n  sequence:\n    - service: light.turn_off\n",
        headers={**auth_headers, "Content-Type": _TEXT},
    )
    assert_conforms("POST", "/v1/config/script", await _json(post))
    delete = await client.delete("/v1/config/script?id=probe_script", headers=auth_headers)
    assert_conforms("DELETE", "/v1/config/script", await _json(delete))


async def test_conformance_automation_write(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    put = await client.put(
        "/v1/config/automation?id=automation.door_light&dry_run=true",
        data="id: automation.door_light\nalias: Updated\ntrigger: []\naction: []\n",
        headers={**auth_headers, "Content-Type": _TEXT},
    )
    assert_conforms("PUT", "/v1/config/automation", await _json(put))
    post = await client.post(
        "/v1/config/automation",
        data="id: automation.probe\nalias: Probe\ntrigger: []\naction: []\n",
        headers={**auth_headers, "Content-Type": _TEXT},
    )
    assert_conforms("POST", "/v1/config/automation", await _json(post))  # entity_id nullable (P2-4)
    delete = await client.delete("/v1/config/automation?id=automation.probe", headers=auth_headers)
    assert_conforms("DELETE", "/v1/config/automation", await _json(delete))


async def test_conformance_ref_endpoints(client: TestClient, auth_headers: dict[str, str], config_dir: Path) -> None:
    (config_dir / "configuration.yaml").write_text("sensor:\n  value: sensor.gone\n", encoding="utf-8")
    assert_conforms(
        "GET", "/v1/ref/scan", await _json(await client.get("/v1/ref/scan?target=sensor.gone", headers=auth_headers))
    )
    assert_conforms("GET", "/v1/ref/entities", await _json(await client.get("/v1/ref/entities", headers=auth_headers)))
    replace = await client.post(
        "/v1/ref/replace",
        json={"old": "sensor.gone", "new": "sensor.new", "dry_run": True},
        headers=auth_headers,
    )
    assert_conforms("POST", "/v1/ref/replace", await _json(replace))


# ---------------------------------------------------------------------------
# Static: every query param a handler reads must be declared in the spec
# ---------------------------------------------------------------------------

_QUERY_READ_RE = re.compile(r"request\.query(?:\.get\(|\[)\s*[\"']([a-zA-Z_]+)[\"']")

_ROUTE_MODULES = [config, related, refscan, templates, scripts, automations, helpers, status, logs, wireguard]


@pytest.mark.parametrize("module", _ROUTE_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_handler_query_params_are_specced(module: Any) -> None:
    """Every request.query key a route module reads is declared for one of its endpoints."""
    src = Path(module.__file__).read_text(encoding="utf-8")
    read_params = set(_QUERY_READ_RE.findall(src))

    specced: set[str] = set()
    for route_def in module.routes:
        meta = ENDPOINT_META.get((route_def.method, route_def.path), {})
        for param in meta.get("parameters", []):
            if param.get("in") == "query":
                specced.add(param["name"])

    missing = read_params - specced
    assert not missing, f"{module.__name__} reads query params not in its spec: {sorted(missing)}"
