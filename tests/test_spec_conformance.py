"""Field-granular OpenAPI conformance for the **whole** route table (C-12).

``test_openapi.py`` proves every route has a spec entry — path-and-method
presence. TC-5 says that is not a contract: a contract is field-level, in both
directions. D45 is the proof it is not theoretical — hactl's Go structs silently
dropped a ``reloaded`` field that the companion sent and the spec documented,
across four copies of the contract, because every "contract" test checked paths.

So this module enforces, for every ``ENDPOINT_META`` entry:

1. **Producer → spec.** The route is driven against a real handler and its
   response must validate against the spec schema *and* carry no undocumented
   field (recursively). A handler that grows a response key fails here.
2. **Spec → producer.** Every field the spec documents must actually be produced
   by one of that route's probes, or appear in ``UNOBSERVED_FIELDS`` with a
   written reason. A spec that grows a field nothing emits fails here. This is
   also what stops a probe from passing vacuously: an empty ``{}`` response
   satisfies direction 1 and fails direction 2.
3. **Whole-table quantification.** ``test_every_endpoint_is_conformance_classified``
   is the canary: a newly added route is either probed or explicitly exempted,
   and until someone classifies it the suite is red. A hand-maintained list of
   *covered* routes would drift silently (TC-7); a hand-maintained list of
   *exemptions* cannot, because the canary derives the covered set from
   ``ENDPOINT_META`` itself.

Every query param a handler reads must also be declared in the spec (bottom of
the file) — the check that would have caught the P2-2/-3/-4 drift.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from aiohttp.test_utils import TestClient

from companion import core_api, logbuffer, surgical, wg, wg_monitor
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
    wiring,
)
from tests.conftest import FIXTURES_DIR
from tests.related_fixture import SOURCE_ENTITY_ID, seed_related_fixture
from tests.storage_fixture import seed_storage_helpers


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


def _schema_field_paths(schema: Any, prefix: str = "$") -> set[str]:
    """Every field path the spec documents, e.g. ``$.peers[].public_key``.

    Free-form objects (``additionalProperties`` with no ``properties``) end the
    walk — their keys are data, not documented fields.
    """
    out: set[str] = set()
    if not isinstance(schema, dict):
        return out
    if schema.get("type") == "object":
        for key, sub in (schema.get("properties") or {}).items():
            child = f"{prefix}.{key}"
            out.add(child)
            out |= _schema_field_paths(sub, child)
    elif schema.get("type") == "array":
        out |= _schema_field_paths(schema.get("items") or {}, prefix + "[]")
    return out


def _payload_field_paths(payload: Any, prefix: str = "$") -> set[str]:
    """Every field path actually present in a response, in the same grammar."""
    out: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            child = f"{prefix}.{key}"
            out.add(child)
            out |= _payload_field_paths(value, child)
    elif isinstance(payload, list):
        for value in payload:
            out |= _payload_field_paths(value, prefix + "[]")
    return out


_TEXT = "text/plain"


async def _json(resp: Any) -> Any:
    assert resp.status < 400, f"unexpected {resp.status}: {await resp.text()}"
    return await resp.json()


# ---------------------------------------------------------------------------
# The probe table — one entry per route in ENDPOINT_META
# ---------------------------------------------------------------------------

#: A setup runs before its probe's request and may return a teardown callable.
Setup = Callable[[Path, pytest.MonkeyPatch], Callable[[], None] | None]


@dataclass(frozen=True)
class Probe:
    """A request that provably reaches a route's success path.

    ``expect`` carries the behavioural assertions that must survive alongside the
    structural ones — the fields whose absence was a real drift defect.
    """

    url: str
    label: str = ""
    data: str | None = None
    json_body: Any = None
    setup: Setup | None = None
    expect: Mapping[str, Any] = field(default_factory=dict)


def _seed_ref_target(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (config_dir / "configuration.yaml").write_text("sensor:\n  value: sensor.gone\n", encoding="utf-8")


#: What the ``skipped`` probes below expect back, spelled once. Every ref route
#: walks the same graph, so a route that reported a different location or reason
#: for the same broken config would be disagreeing with its siblings.
SKIPPED_PROBE_EXPECT = [{"location": "packages/renamed.yaml", "reason": "missing"}]


def _seed_ref_skipped(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A config graph with one `!include` naming a file that is not there.

    ``skipped[]`` exists only on this branch, so without a probe that reaches it
    the spec→producer direction could not see the field at all: it would be
    documented, produced by nothing the suite drives, and free to be dropped by
    the next hand that touches a ref route — the D45 shape. The target still
    resolves in the file that *is* readable, so the probe also proves the walk
    keeps going rather than abandoning the scan.
    """
    (config_dir / "configuration.yaml").write_text(
        "automation: !include packages/renamed.yaml\nsensor:\n  value: sensor.gone\n",
        encoding="utf-8",
    )


def _seed_storage_helpers(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A helper created in HA's UI — the only kind a normally-configured instance has."""
    seed_storage_helpers(config_dir, ["input_boolean"])


def _seed_unwired_script(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the `script:` include, so the probe answers with a reason instead of a file."""
    config = config_dir / "configuration.yaml"
    kept = [
        line for line in config.read_text(encoding="utf-8").splitlines(keepends=True) if not line.startswith("script")
    ]
    config.write_text("".join(kept), encoding="utf-8")


def _seed_related(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_related_fixture(config_dir)


def _seed_related_stale(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale entity that is still *referenced*, so ``stale_refs[]`` is non-empty.

    Querying an unreferenced ghost returns ``stale_refs: []`` — structurally
    valid, but it never exercises the item fields, which is precisely the vacuous
    pass the spec→producer direction exists to catch.
    """
    seed_related_fixture(config_dir)
    (config_dir / "configuration.yaml").write_text("sensor:\n  value: sensor.gone\n", encoding="utf-8")


def _seed_logbuffer(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Install the ring buffer and emit one record, so ``entries[]`` is non-empty.

    Without this the endpoint answers ``{"entries": []}``, which satisfies the
    no-undocumented-field direction vacuously — the spec→producer direction is
    what makes an empty probe a failure rather than a pass.
    """
    handler = logbuffer.install()
    logging.getLogger("companion.wg_monitor").warning("conformance probe record")

    def _teardown() -> None:
        logging.getLogger().removeHandler(handler)
        logbuffer._handler = None

    return _teardown


_WG_CONF = "[Interface]\nPrivateKey = X\nAddress = 10.0.0.1/24\n[Peer]\nPublicKey = Y\nAllowedIPs = 0/0\n"

# `wg show <tunnel> dump`: interface row (private-key, public-key, listen-port,
# fwmark), then one peer row (public-key, preshared-key, endpoint, allowed-ips,
# latest-handshake, rx, tx, persistent-keepalive). Byte-identical in shape to the
# dump `_parse_wg_dump`'s own unit test asserts values against — a probe fixture
# with the wrong column count would still show every documented field present,
# just carrying the wrong data, which is the failure mode this suite exists to
# prevent rather than commit.
_WG_DUMP = "PRIV\tAAAA\t51820\toff\nBBBB\t(none)\t1.2.3.4:51820\t10.0.0.0/24\t1894\t1260\t4669\t25\n"


def _wg_dirs(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the persistent and runtime WireGuard dirs into the sandbox."""
    persist = config_dir / "wg-persist"
    monkeypatch.setattr(wg, "_PERSIST_DIR", persist)
    monkeypatch.setattr(wg, "_WG_CONFIG_DIR", config_dir / "wg-runtime")
    return persist


async def _wg_cmd_ok(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    return (0, "", "")


def _iface_up(value: bool) -> Callable[..., Any]:
    """``_is_interface_up`` is a coroutine function — a plain lambda 500s the route."""

    async def _stub(*args: Any, **kwargs: Any) -> bool:
        return value

    return _stub


def _seed_wg_config(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _wg_dirs(config_dir, monkeypatch)


def _seed_wg_start(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    persist = _wg_dirs(config_dir, monkeypatch)
    persist.mkdir(parents=True, exist_ok=True)
    (persist / "wg0.conf").write_text(_WG_CONF, encoding="utf-8")

    async def _no_hostnames(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(wireguard, "_is_interface_up", _iface_up(False))
    monkeypatch.setattr(wireguard, "_run_wg_cmd", _wg_cmd_ok)
    monkeypatch.setattr(wireguard, "_resolve_endpoint_hostnames", _no_hostnames)
    monkeypatch.setattr(wireguard.wg_monitor, "start_monitor", lambda *a, **k: None)


def _seed_wg_stop(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _wg_dirs(config_dir, monkeypatch)
    monkeypatch.setattr(wireguard, "_is_interface_up", _iface_up(True))
    monkeypatch.setattr(wireguard, "_run_wg_cmd", _wg_cmd_ok)
    monkeypatch.setattr(wireguard.wg_monitor, "stop_monitor", lambda *a, **k: None)


def _seed_wg_status(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Active tunnel with a *running* monitor, so every documented field appears.

    The monitor state is injected rather than started: ``status()`` reads the
    registry, and a real task would make the response time-dependent.
    """
    _wg_dirs(config_dir, monkeypatch)

    async def _dump(*args: str, timeout: int = 30) -> tuple[int, str, str]:
        return (0, _WG_DUMP, "")

    monkeypatch.setattr(wireguard, "_is_interface_up", _iface_up(True))
    monkeypatch.setattr(wireguard, "_run_wg_cmd", _dump)
    now = time.time()
    state = wg_monitor._MonitorState(
        tunnel="wg0",
        hostnames=["vpn.example.com"],
        last_check_ts=now - 5,
        last_reresolve_ts=now - 90,
        resolved={"vpn.example.com": "1.2.3.4"},
        in_backoff=True,
        attempt=2,
        next_retry_ts=now + 30,
        last_error="temporary failure in name resolution",
    )
    monkeypatch.setitem(wg_monitor._monitors, "wg0", state)


def _reload_fails(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-seed the fixtures, then make Home Assistant refuse the reload.

    Re-seeding is what lets the derived probe repeat a create or delete the
    preceding probe already consumed, with a byte-identical request — so the
    success branch and the failure branch cannot drift into being different
    requests.
    """
    for src in FIXTURES_DIR.iterdir():
        if src.is_file() and src.suffix in (".yaml", ".yml"):
            shutil.copy2(src, config_dir / src.name)
        elif src.is_dir():
            shutil.copytree(src, config_dir / src.name, dirs_exist_ok=True)

    async def _refused(domain: str, service: str, data: object = None) -> core_api.ServiceResult:
        return core_api.ServiceResult(False, "HTTP 400: Service not found")

    monkeypatch.setattr(core_api, "call_service", _refused)


def _with_reload_failure(probes: tuple[Probe, ...]) -> tuple[Probe, ...]:
    """``probes``, plus a repeat of the last one with HA refusing the reload.

    ``reload_error`` exists only on that branch, so without this probe the
    spec→producer direction could not see it and the twelve routes that have to
    propagate the reason would be trusted rather than checked — the field would
    be documented, emitted by nothing the suite drives, and free to be forgotten
    by route #N. It goes last on purpose: the patch it installs lives until the
    test ends.
    """
    last = probes[-1]
    assert last.setup is None, f"{last.url}: the derived probe would drop the source probe's setup"
    derived = replace(
        last,
        label=f"{last.label or 'apply'}+reload-fails",
        setup=_reload_fails,
        expect={**last.expect, "reloaded": False},
    )
    return (*probes, derived)


def _with_whole_file_rewrite(probes: tuple[Probe, ...], filename: str) -> tuple[Probe, ...]:
    """``probes``, plus a repeat of the last one with the single-entry splice refused.

    ``reformatted`` exists only on the branch where the splice cannot be used and
    the file is re-serialized whole. Nothing this suite drives reaches it
    otherwise — the fixtures all splice cleanly — so without this probe the field
    would be documented, emitted by nothing, and free to be dropped by route #N
    (D45's shape exactly).

    The branch is forced by refusing the splice outright, the same way
    :func:`_reload_fails` forces ``reload_error`` by refusing HA's reload. That
    keeps this file's job to the *field contract*; whether real inputs can reach
    the fallback at all is a behavioural question, answered against real files
    (flow-style top level, an anchor spanning two entries) in
    ``tests/test_surgical.py``. ``filename`` names the file the route writes, so
    a route wired to the wrong probe file fails the assertion below rather than
    passing on someone else's fixture.

    It wraps *outside* :func:`_with_reload_failure` so that helper still sees a
    setup-free probe to derive from; the two setups compose.
    """
    source = probes[-1]

    def setup(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        if source.setup is not None:
            source.setup(config_dir, monkeypatch)
        assert (config_dir / filename).is_file(), f"{filename} is not in the fixture set — wrong file for this route?"
        monkeypatch.setattr(surgical, "_splice", lambda *args, **kwargs: None)

    derived = replace(
        source,
        label=f"{source.label or 'apply'}+whole-file",
        setup=setup,
        expect={**source.expect, "reformatted": True},
    )
    return (*probes, derived)


RESPONSE_PROBES: dict[tuple[str, str], tuple[Probe, ...]] = {
    ("GET", "/v1/health"): (Probe("/v1/health"),),
    ("GET", "/v1/status"): (Probe("/v1/status"),),
    ("GET", "/v1/config/files"): (Probe("/v1/config/files"),),
    ("GET", "/v1/config/file"): (Probe("/v1/config/file?path=automations.yaml"),),
    # The apply probe targets an *existing* file on purpose: `backup` is only
    # emitted when there was prior content to preserve, so a write to a new path
    # never reaches that field.
    ("PUT", "/v1/config/file"): (
        Probe(
            "/v1/config/file?path=automations.yaml&dry_run=true",
            label="dry",
            data="conformance_probe:\n  value: 1\n",
        ),
        Probe(
            "/v1/config/file?path=automations.yaml&dry_run=false",
            label="apply",
            data="conformance_probe:\n  value: 1\n",
            # P2-4: the spec omitted `validated` while the handler returned it.
            expect={"validated": True},
        ),
    ),
    ("GET", "/v1/config/block"): (
        Probe("/v1/config/block?path=automations.yaml&id=automation.door_light"),
        # The index form — bracketed exactly as `ref scan` prints it.
        Probe("/v1/config/block?path=template.yaml&id=%5B0%5D"),
    ),
    ("GET", "/v1/related/entity"): (
        Probe(
            f"/v1/related/entity?entity_id={SOURCE_ENTITY_ID}",
            label="live",
            setup=_seed_related,
            expect={"stale": False},
        ),
        # P2-2: stale/stale_refs only appear on this branch.
        Probe(
            "/v1/related/entity?entity_id=sensor.gone&stale=true",
            label="stale",
            setup=_seed_related_stale,
            expect={"stale": True},
        ),
    ),
    # Second probe on each ref route: `skipped[]` is emitted only when the walk
    # could not read the whole config graph, and all three routes must report it.
    ("GET", "/v1/ref/scan"): (
        Probe("/v1/ref/scan?target=sensor.gone", setup=_seed_ref_target),
        Probe(
            "/v1/ref/scan?target=sensor.gone",
            label="skipped",
            setup=_seed_ref_skipped,
            expect={"skipped": SKIPPED_PROBE_EXPECT},
        ),
    ),
    ("GET", "/v1/ref/entities"): (
        Probe("/v1/ref/entities"),
        Probe("/v1/ref/entities", label="skipped", setup=_seed_ref_skipped, expect={"skipped": SKIPPED_PROBE_EXPECT}),
    ),
    ("POST", "/v1/ref/replace"): (
        Probe(
            "/v1/ref/replace",
            setup=_seed_ref_target,
            json_body={"old": "sensor.gone", "new": "sensor.new", "dry_run": True},
        ),
        Probe(
            "/v1/ref/replace",
            label="skipped",
            setup=_seed_ref_skipped,
            json_body={"old": "sensor.gone", "new": "sensor.new", "dry_run": True},
            expect={"skipped": SKIPPED_PROBE_EXPECT},
        ),
    ),
    ("GET", "/v1/config/templates"): (Probe("/v1/config/templates"),),
    ("GET", "/v1/config/template"): (Probe("/v1/config/template?id=tpl_energie_zaehler"),),
    # `reloaded` — the exact field D45 lost — is only emitted on apply, so the
    # dry-run probe alone would leave it unobserved on all three PUT routes.
    ("PUT", "/v1/config/template"): _with_whole_file_rewrite(
        _with_reload_failure(
            (
                Probe(
                    "/v1/config/template?id=tpl_energie_zaehler&dry_run=true",
                    label="dry",
                    data='name: "Updated"\nunique_id: tpl_energie_zaehler\nstate: "{{ 1 }}"\n',
                ),
                Probe(
                    "/v1/config/template?id=tpl_energie_zaehler&dry_run=false",
                    label="apply",
                    data='name: "Updated"\nunique_id: tpl_energie_zaehler\nstate: "{{ 1 }}"\n',
                ),
            )
        ),
        "template.yaml",
    ),
    ("POST", "/v1/config/template"): _with_whole_file_rewrite(
        _with_reload_failure(
            (
                Probe(
                    "/v1/config/template?domain=sensor",
                    data='name: "New"\nunique_id: tpl_probe\nstate: "{{ 1 }}"\n',
                ),
            )
        ),
        "template.yaml",
    ),
    ("DELETE", "/v1/config/template"): _with_whole_file_rewrite(
        _with_reload_failure((Probe("/v1/config/template?id=tpl_energie_zaehler"),)),
        "template.yaml",
    ),
    ("GET", "/v1/config/scripts"): (Probe("/v1/config/scripts"),),
    ("GET", "/v1/config/script"): (Probe("/v1/config/script?id=welcome_home"),),
    ("PUT", "/v1/config/script"): _with_whole_file_rewrite(
        _with_reload_failure(
            (
                Probe(
                    "/v1/config/script?id=welcome_home&dry_run=true",
                    label="dry",
                    data="alias: Updated\nsequence:\n  - service: light.turn_on\n",
                ),
                Probe(
                    "/v1/config/script?id=welcome_home&dry_run=false",
                    label="apply",
                    data="alias: Updated\nsequence:\n  - service: light.turn_on\n",
                ),
            )
        ),
        "scripts.yaml",
    ),
    ("POST", "/v1/config/script"): _with_whole_file_rewrite(
        _with_reload_failure(
            (
                Probe(
                    "/v1/config/script",
                    data="probe_script:\n  alias: Probe\n  sequence:\n    - service: light.turn_off\n",
                ),
            )
        ),
        "scripts.yaml",
    ),
    ("DELETE", "/v1/config/script"): _with_whole_file_rewrite(
        _with_reload_failure((Probe("/v1/config/script?id=welcome_home"),)),
        "scripts.yaml",
    ),
    ("GET", "/v1/config/automations"): (Probe("/v1/config/automations"),),
    ("GET", "/v1/config/automation"): (Probe("/v1/config/automation?id=automation.door_light"),),
    ("PUT", "/v1/config/automation"): _with_whole_file_rewrite(
        _with_reload_failure(
            (
                Probe(
                    "/v1/config/automation?id=automation.door_light&dry_run=true",
                    label="dry",
                    data="id: automation.door_light\nalias: Updated\ntrigger: []\naction: []\n",
                ),
                Probe(
                    "/v1/config/automation?id=automation.door_light&dry_run=false",
                    label="apply",
                    data="id: automation.door_light\nalias: Updated\ntrigger: []\naction: []\n",
                ),
            )
        ),
        "automations.yaml",
    ),
    ("POST", "/v1/config/automation"): _with_whole_file_rewrite(
        _with_reload_failure(
            (Probe("/v1/config/automation", data="id: automation.probe\nalias: Probe\ntrigger: []\naction: []\n"),)
        ),
        "automations.yaml",
    ),
    ("DELETE", "/v1/config/automation"): _with_whole_file_rewrite(
        _with_reload_failure((Probe("/v1/config/automation?id=automation.door_light"),)),
        "automations.yaml",
    ),
    ("GET", "/v1/config/helpers"): (Probe("/v1/config/helpers"),),
    # Both sources, because they are two branches of one route and only the
    # second one is the shape a UI-managed instance actually has.
    ("GET", "/v1/config/helper"): (
        Probe("/v1/config/helper?id=guest_mode", label="yaml", expect={"source": "yaml"}),
        Probe(
            "/v1/config/helper?id=input_boolean.probe_bool",
            label="storage",
            setup=_seed_storage_helpers,
            expect={"source": "storage"},
        ),
    ),
    # P2-3: entity_id/reloaded/entity_created were produced but undocumented.
    ("POST", "/v1/config/helper"): _with_whole_file_rewrite(
        _with_reload_failure((Probe("/v1/config/helper?domain=input_boolean", data="probe_helper:\n  name: Probe\n"),)),
        "input_boolean.yaml",
    ),
    ("PUT", "/v1/config/helper"): _with_whole_file_rewrite(
        _with_reload_failure((Probe("/v1/config/helper?id=guest_mode", data="name: Probe 2\n"),)),
        "input_boolean.yaml",
    ),
    ("DELETE", "/v1/config/helper"): _with_whole_file_rewrite(
        _with_reload_failure((Probe("/v1/config/helper?id=guest_mode"),)),
        "input_boolean.yaml",
    ),
    # `file` and `reason` are mutually exclusive branches: one probe alone would
    # leave the other documented and produced by nothing (the D45 shape).
    ("GET", "/v1/config/wiring"): (
        Probe("/v1/config/wiring?domain=script", label="wired", expect={"wired": True, "file": "scripts.yaml"}),
        Probe(
            "/v1/config/wiring?domain=script",
            label="unwired",
            setup=_seed_unwired_script,
            expect={"wired": False},
        ),
    ),
    ("POST", "/v1/ha/reload/{domain}"): (Probe("/v1/ha/reload/automation"),),
    ("POST", "/v1/ha/check-config"): (Probe("/v1/ha/check-config"),),
    ("GET", "/v1/logs"): (Probe("/v1/logs?component=wireguard", setup=_seed_logbuffer),),
    ("POST", "/v1/wireguard/config"): (Probe("/v1/wireguard/config?tunnel=wg0", data=_WG_CONF, setup=_seed_wg_config),),
    ("POST", "/v1/wireguard/start"): (Probe("/v1/wireguard/start?tunnel=wg0", setup=_seed_wg_start),),
    ("POST", "/v1/wireguard/stop"): (Probe("/v1/wireguard/stop?tunnel=wg0", setup=_seed_wg_stop),),
    ("GET", "/v1/wireguard/status"): (
        Probe("/v1/wireguard/status?tunnel=wg0", label="active", setup=_seed_wg_status, expect={"state": "active"}),
    ),
}

#: Routes that cannot be driven here, each with the reason and where the field
#: contract is covered instead. **Must stay loud and enumerated** — an
#: unexplained gap is how field drift survives. Empty is the goal, and is
#: currently achieved: every route in ENDPOINT_META has a probe.
UNDRIVEN: dict[tuple[str, str], str] = {}

#: Documented fields no probe can produce, each with the reason it is
#: unreachable. Kept honest from both sides by
#: ``test_route_response_conformance``: an entry naming a field that is no longer
#: in the spec, or one the probes *do* produce, fails as a stale exemption.
UNOBSERVED_FIELDS: dict[tuple[str, str], dict[str, str]] = {}


def _route_ids(keys: Any) -> list[Any]:
    return [pytest.param(method, path, id=f"{method} {path}") for method, path in sorted(keys, key=lambda k: k[::-1])]


def test_every_endpoint_is_conformance_classified() -> None:
    """Canary (C-12): a new route must be probed or explicitly exempted.

    This is the property that makes the sweep survive: the covered set is derived
    from ``ENDPOINT_META``, never hand-listed, so adding a route to the table
    without a probe turns the suite red instead of silently widening the gap.
    """
    table = set(ENDPOINT_META)
    classified = RESPONSE_PROBES.keys() | UNDRIVEN.keys()
    assert table == classified, (
        f"unclassified endpoints: {sorted(table - classified)} — add each to RESPONSE_PROBES (with a request that "
        f"reaches its success path) or to UNDRIVEN (with the reason it cannot be driven and where it is covered "
        f"instead). Stale entries: {sorted(classified - table)}"
    )
    overlap = RESPONSE_PROBES.keys() & UNDRIVEN.keys()
    assert not overlap, f"endpoints both probed and exempted: {sorted(overlap)}"
    for key, reason in UNDRIVEN.items():
        assert reason.strip(), f"{key}: exemption must state why the route cannot be driven"


def test_unobserved_field_exemptions_are_for_known_routes() -> None:
    unknown = UNOBSERVED_FIELDS.keys() - set(ENDPOINT_META)
    assert not unknown, f"UNOBSERVED_FIELDS names routes that are not in ENDPOINT_META: {sorted(unknown)}"
    for key, fields in UNOBSERVED_FIELDS.items():
        for path, reason in fields.items():
            assert reason.strip(), f"{key} {path}: exemption must state why the field cannot be produced"


@pytest.mark.parametrize(("method", "path"), _route_ids(RESPONSE_PROBES))
async def test_route_response_conformance(
    client: TestClient,
    auth_headers: dict[str, str],
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
) -> None:
    """C-12: both directions of the field contract, for every route in the table.

    Producer→spec: no response field may be undocumented, and the payload must
    validate. Spec→producer: every documented field must be produced by one of
    this route's probes or carry a written exemption — which is also what stops a
    probe from passing vacuously on an empty response.
    """
    schema = ENDPOINT_META[(method, path)]["response_schema"]
    assert isinstance(schema, dict)
    observed: set[str] = set()

    for probe in RESPONSE_PROBES[(method, path)]:
        teardown: Callable[[], None] | None = None
        if probe.setup is not None:
            teardown = probe.setup(config_dir, monkeypatch)
        try:
            headers = dict(auth_headers)
            kwargs: dict[str, Any] = {}
            if probe.data is not None:
                kwargs["data"] = probe.data
                headers["Content-Type"] = _TEXT
            if probe.json_body is not None:
                kwargs["json"] = probe.json_body
            resp = await client.request(method, probe.url, headers=headers, **kwargs)
            payload = await _json(resp)
        finally:
            if teardown is not None:
                teardown()

        where = f"{method} {path}" + (f" [{probe.label}]" if probe.label else "")
        assert isinstance(payload, dict), f"{where}: expected a JSON object, got {type(payload).__name__}"
        assert_conforms(method, path, payload)
        for key, value in probe.expect.items():
            assert payload.get(key) == value, f"{where}: expected {key}={value!r}, got {payload.get(key)!r}"
        observed |= _payload_field_paths(payload)

    documented = _schema_field_paths(schema)
    assert documented, f"{method} {path}: response_schema documents no fields — is it still an object schema?"
    exempt = UNOBSERVED_FIELDS.get((method, path), {})

    missing = documented - observed - exempt.keys()
    assert not missing, (
        f"{method} {path}: spec documents fields no probe produces: {sorted(missing)} — either the handler stopped "
        f"emitting them (drift, the D45 direction), the probes do not reach the branch that emits them (add a "
        f"probe), or the spec documents a field that does not exist (add to UNOBSERVED_FIELDS with a reason)"
    )
    stale_names = exempt.keys() - documented
    assert not stale_names, (
        f"{method} {path}: UNOBSERVED_FIELDS names fields the spec no longer has: {sorted(stale_names)}"
    )
    now_observed = exempt.keys() & observed
    assert not now_observed, (
        f"{method} {path}: UNOBSERVED_FIELDS still exempts fields the probes now produce: {sorted(now_observed)} — "
        f"drop the exemption"
    )


# ---------------------------------------------------------------------------
# Static: every query param a handler reads must be declared in the spec
# ---------------------------------------------------------------------------

_QUERY_READ_RE = re.compile(r"request\.query(?:\.get\(|\[)\s*[\"']([a-zA-Z_]+)[\"']")

_ROUTE_MODULES = [config, related, refscan, templates, scripts, automations, helpers, status, logs, wireguard, wiring]


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
