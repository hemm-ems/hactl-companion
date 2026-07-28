"""A failed reload says why, on the wire (`reload_error`).

The defect this file pins down was observed in a hactl E2E run: the CLI printed

    created template "h12_tpl_state" (domain=sensor)
    warning: template written but HA did not confirm reload (is `template: !include
    template.yaml` in configuration.yaml?)

— a rhetorical question, because the reason had been thrown away here.
``core_api.call_service`` read HA's status and body, logged them into the add-on
log (which nobody debugging the CLI is reading) and returned a bare ``False``;
``reload_domain`` forwarded the bool; every route put ``{"reloaded": false}`` on
the wire. So the reason existed and never reached the operator or the test.

Two properties are load-bearing and are asserted separately:

* on failure ``reload_error`` is **present** and names HA's status (or the
  transport error class) — the diagnosis;
* on success it is **absent** — not empty, not null — so the successful response
  is byte-identical to the one this service sent before the field existed and no
  existing consumer can see a difference.

The failure path of *every* route that reports ``reloaded`` is swept by
``test_spec_conformance.py`` (the ``reload-fails`` probes: C-12's spec→producer
direction cannot pass unless each of the twelve emits the field). What this
module adds is the content of the reason, and the two ends of the real stack:
one route driven against an HA that refuses the service call, one against an HA
that cannot be reached at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from companion import core_api

# Bound at import time, before the autouse ``core_api_calls`` fixture replaces
# the module attribute: these tests need the *real* client, driven against a
# fake HA of their own, so that the whole stack — route, reload_domain,
# call_service, transport — is what produces the field.
_REAL_CALL_SERVICE = core_api.call_service

_TEXT = {"Content-Type": "text/plain"}


def _refusing_ha(status: int, body: str) -> web.Application:
    """A core API that refuses every service call, the way HA refuses an unknown one."""
    app = web.Application()

    async def handle_service(request: web.Request) -> web.Response:
        return web.Response(text=body, status=status, content_type="application/json")

    app.router.add_post("/services/{domain}/{service}", handle_service)
    return app


def _use_real_core_api(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """Undo the autouse fake for one test and point the real client at ``url``."""
    monkeypatch.setattr(core_api, "call_service", _REAL_CALL_SERVICE)
    monkeypatch.setenv("CORE_API_URL", url)


async def test_create_template_reports_the_status_ha_refused_with(
    client: TestClient,
    auth_headers: dict[str, str],
    aiohttp_server: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /v1/config/template, HA answers 400: the reason reaches the caller.

    This is the exact route the observed failure came from (`hactl tpl create`).
    The write itself succeeded, so the response stays 201 with ``reloaded:
    false`` — only now it also says *why* the reload did not happen.
    """
    ha = await aiohttp_server(_refusing_ha(400, '{"message": "Service not found"}'))
    _use_real_core_api(monkeypatch, str(ha.make_url("")))

    resp = await client.post(
        "/v1/config/template?domain=sensor",
        headers={**auth_headers, **_TEXT},
        data='name: "Probe"\nunique_id: tpl_reload_error\nstate: "{{ 1 }}"\n',
    )
    assert resp.status == 201
    data = await resp.json()
    assert data["status"] == "created"
    assert data["unique_id"] == "tpl_reload_error"
    assert data["reloaded"] is False
    assert "HTTP 400" in data["reload_error"]
    assert "Service not found" in data["reload_error"]


async def test_update_script_reports_a_transport_failure(
    client: TestClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PUT /v1/config/script with no HA listening: the reason names the transport error.

    There is no HTTP status to report here, which is precisely the case a
    status-only message would render as an empty reason.
    """
    _use_real_core_api(monkeypatch, "http://127.0.0.1:1")

    resp = await client.put(
        "/v1/config/script?id=welcome_home&dry_run=false",
        headers={**auth_headers, **_TEXT},
        data="alias: Updated\nsequence:\n  - service: light.turn_on\n",
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "applied"
    assert data["reloaded"] is False
    assert "ClientConnectorError" in data["reload_error"]
    assert "127.0.0.1:1" in data["reload_error"]


async def test_successful_reload_response_is_unchanged(client: TestClient, auth_headers: dict[str, str]) -> None:
    """A successful reload sends exactly what it sent before ``reload_error`` existed.

    Asserted on the raw bytes, not the decoded dict: the claim is that a consumer
    written against the old response cannot tell this build from the previous
    one, and an ``"reload_error": null`` or an empty string would break it.
    """
    resp = await client.put(
        "/v1/config/script?id=welcome_home&dry_run=false",
        headers={**auth_headers, **_TEXT},
        data="alias: Updated\nsequence:\n  - service: light.turn_on\n",
    )
    assert resp.status == 200
    assert await resp.text() == '{"status": "applied", "reloaded": true}'


async def test_reload_error_is_absent_not_null_on_every_created_field(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """The same, for a create whose response carries several other fields.

    ``POST /v1/config/helper`` is the widest of the reload-reporting responses
    (id, entity_id, reloaded, entity_created), so it is where an inserted field
    would be most likely to displace or shadow another.
    """
    resp = await client.post(
        "/v1/config/helper?domain=input_boolean",
        headers={**auth_headers, **_TEXT},
        data="reload_probe:\n  name: Reload Probe\n",
    )
    assert resp.status == 201
    assert await resp.json() == {
        "status": "created",
        "id": "reload_probe",
        "entity_id": "input_boolean.reload_probe",
        "reloaded": True,
        "entity_created": True,
    }


def test_reload_fields_never_omits_the_reason_on_failure() -> None:
    """``reloaded: false`` without a reason is the shape this change exists to remove."""
    assert core_api.reload_fields(core_api.ServiceResult(True)) == {"reloaded": True}

    failed = core_api.reload_fields(core_api.ServiceResult(False, "HTTP 400: Service not found"))
    assert failed == {"reloaded": False, "reload_error": "HTTP 400: Service not found"}

    # A result constructed without a reason (nothing in this repo does, but the
    # field is optional) still must not answer a bare false.
    assert core_api.reload_fields(core_api.ServiceResult(False))["reload_error"]
