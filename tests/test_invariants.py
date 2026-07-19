"""Cross-cutting invariant tests, quantified over the full route table.

``INVARIANTS.md`` at the repo root states the companion's cross-cutting rules;
this module is their enforcement. Every test derives its case list from
``ENDPOINT_META`` — the same table that generates the OpenAPI spec and that
``test_openapi.py`` proves complete against the registered routes — so a newly
added route is covered automatically. A new *mutating* route additionally
fails the classification canary until a human sorts it into ``FILE_WRITES``
(with a request probe) or ``SERVICE_ENDPOINTS`` (with a reason).

The point of quantifying: the 2026-07 review bugs (auth spoof, write-gate
holes) were not misunderstood features but route #N forgetting a rule routes
#1..N-1 followed. Example-based tests cannot catch that; a table-driven sweep
can.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient

from companion.backups import BACKUP_DIRNAME
from companion.openapi import ENDPOINT_META
from companion.server import AUTH_EXEMPT_PATHS

_TEXT = "text/plain"


def _url(path: str) -> str:
    """Substitute a dummy value for path-template segments like ``{domain}``."""
    return re.sub(r"\{[^}]+\}", "probe", path)


def _params(keys: Any) -> list[Any]:
    return [pytest.param(method, path, id=f"{method} {path}") for method, path in sorted(keys)]


# ---------------------------------------------------------------------------
# C-1: auth on every route
# ---------------------------------------------------------------------------

PROTECTED = [(m, p) for (m, p) in ENDPOINT_META if p not in AUTH_EXEMPT_PATHS]


def test_auth_exemptions_are_exactly_health_and_status() -> None:
    """Canary: widening the exemption set must be a conscious, reviewed act."""
    assert {"/v1/health", "/v1/status"} == AUTH_EXEMPT_PATHS, (
        f"AUTH_EXEMPT_PATHS changed to {sorted(AUTH_EXEMPT_PATHS)} — if intentional, update INVARIANTS.md C-1 "
        "and this canary in the same PR"
    )


@pytest.mark.parametrize(("method", "path"), _params(PROTECTED))
async def test_missing_token_rejected(client: TestClient, method: str, path: str) -> None:
    resp = await client.request(method, _url(path))
    assert resp.status == 401, f"{method} {path} answered {resp.status} without any credential"


@pytest.mark.parametrize(("method", "path"), _params(PROTECTED))
async def test_wrong_token_rejected(client: TestClient, method: str, path: str) -> None:
    resp = await client.request(method, _url(path), headers={"Authorization": "Bearer wrong-token"})
    assert resp.status == 401, f"{method} {path} answered {resp.status} with a wrong bearer token"


@pytest.mark.parametrize(("method", "path"), _params(PROTECTED))
async def test_spoofed_ingress_header_rejected(client: TestClient, method: str, path: str) -> None:
    """The client-controlled X-Ingress-Path header must never bypass auth on its own.

    The test client connects from 127.0.0.1, which is not a trusted ingress
    proxy IP — so the header alone has to be worthless on every single route.
    """
    resp = await client.request(method, _url(path), headers={"X-Ingress-Path": "/api/hassio_ingress/abc123"})
    assert resp.status == 401, f"{method} {path} answered {resp.status} for a spoofed ingress header"


# ---------------------------------------------------------------------------
# C-4 / C-5: write gate — dry-run defaults and backup-before-mutate
# ---------------------------------------------------------------------------


def _seed_ref_target(config_dir: Path) -> None:
    (config_dir / "configuration.yaml").write_text("sensor:\n  value: sensor.gone\n", encoding="utf-8")


# Every file-mutating endpoint, with a request that provably reaches its write
# path against the standard fixtures (asserted below — a probe that stops
# writing fails loudly rather than passing vacuously).
#   gated: endpoint declares dry_run (spec cross-checked in
#          test_dry_run_gated_classification_matches_spec)
#   url/data/json: request WITHOUT dry_run — C-4 asserts omission never writes
#   apply: overrides for apply mode (dry_run=false) — C-5 asserts backups
FILE_WRITES: dict[tuple[str, str], dict[str, Any]] = {
    ("PUT", "/v1/config/file"): {
        "url": "/v1/config/file?path=automations.yaml",
        "data": "probe:\n  value: 1\n",
        "gated": True,
        "apply": {"url": "/v1/config/file?path=automations.yaml&dry_run=false"},
    },
    ("PUT", "/v1/config/template"): {
        "url": "/v1/config/template?id=tpl_energie_zaehler",
        "data": 'name: "Updated"\nunique_id: tpl_energie_zaehler\nstate: "{{ 1 }}"\n',
        "gated": True,
        "apply": {"url": "/v1/config/template?id=tpl_energie_zaehler&dry_run=false"},
    },
    ("POST", "/v1/config/template"): {
        "url": "/v1/config/template?domain=sensor",
        "data": 'name: "New"\nunique_id: tpl_probe\nstate: "{{ 1 }}"\n',
        "gated": False,
    },
    ("DELETE", "/v1/config/template"): {
        "url": "/v1/config/template?id=tpl_energie_zaehler",
        "gated": False,
    },
    ("PUT", "/v1/config/script"): {
        "url": "/v1/config/script?id=welcome_home",
        "data": "alias: Updated\nsequence:\n  - service: light.turn_on\n",
        "gated": True,
        "apply": {"url": "/v1/config/script?id=welcome_home&dry_run=false"},
    },
    ("POST", "/v1/config/script"): {
        "url": "/v1/config/script",
        "data": "probe_script:\n  alias: Probe\n  sequence:\n    - service: light.turn_off\n",
        "gated": False,
    },
    ("DELETE", "/v1/config/script"): {
        "url": "/v1/config/script?id=welcome_home",
        "gated": False,
    },
    ("PUT", "/v1/config/automation"): {
        "url": "/v1/config/automation?id=automation.door_light",
        "data": "id: automation.door_light\nalias: Updated\ntrigger: []\naction: []\n",
        "gated": True,
        "apply": {"url": "/v1/config/automation?id=automation.door_light&dry_run=false"},
    },
    ("POST", "/v1/config/automation"): {
        "url": "/v1/config/automation",
        "data": "id: automation.probe\nalias: Probe\ntrigger: []\naction: []\n",
        "gated": False,
    },
    ("DELETE", "/v1/config/automation"): {
        "url": "/v1/config/automation?id=automation.door_light",
        "gated": False,
    },
    ("POST", "/v1/config/helper"): {
        "url": "/v1/config/helper?domain=input_boolean",
        "data": "probe_helper:\n  name: Probe\n",
        "gated": False,
    },
    ("PUT", "/v1/config/helper"): {
        "url": "/v1/config/helper?id=guest_mode",
        "data": "name: Probe 2\n",
        "gated": False,
    },
    ("DELETE", "/v1/config/helper"): {
        "url": "/v1/config/helper?id=guest_mode",
        "gated": False,
    },
    ("POST", "/v1/ref/replace"): {
        "url": "/v1/ref/replace",
        "json": {"old": "sensor.gone", "new": "sensor.new"},
        "gated": True,
        "seed": _seed_ref_target,
        "apply": {"json": {"old": "sensor.gone", "new": "sensor.new", "dry_run": False}},
    },
}

# Mutating endpoints that never touch the /config YAML graph — each with the
# reason it sits outside the file-write gate.
SERVICE_ENDPOINTS: dict[tuple[str, str], str] = {
    ("POST", "/v1/ha/reload/{domain}"): "HA service call; touches no config files",
    ("POST", "/v1/ha/check-config"): "HA service call; touches no config files",
    (
        "POST",
        "/v1/wireguard/config",
    ): "writes WireGuard add-on config via Supervisor and /etc/wireguard, not /config YAML",
    ("POST", "/v1/wireguard/start"): "Supervisor add-on control; no file writes",
    ("POST", "/v1/wireguard/stop"): "Supervisor add-on control; no file writes",
}


def test_every_mutating_endpoint_is_classified() -> None:
    """Canary: a new PUT/POST/DELETE route must be sorted into a bucket."""
    mutating = {key for key in ENDPOINT_META if key[0] in {"PUT", "POST", "DELETE"}}
    classified = FILE_WRITES.keys() | SERVICE_ENDPOINTS.keys()
    assert mutating == classified, (
        f"unclassified mutating endpoints: {sorted(mutating - classified)} — add each to FILE_WRITES (with a "
        f"request probe) or SERVICE_ENDPOINTS (with a reason). Stale entries: {sorted(classified - mutating)}"
    )


def _declared_dry_run_params() -> list[tuple[str, str, object]]:
    """Every dry_run parameter in the spec (query or request-body) with its default."""
    found: list[tuple[str, str, object]] = []
    for (method, path), meta in ENDPOINT_META.items():
        for param in meta.get("parameters", []):  # type: ignore[union-attr]
            if param["name"] == "dry_run":
                found.append((method, path, param["schema"].get("default")))
        body = meta.get("requestBody")
        if isinstance(body, dict):
            for content in body.get("content", {}).values():
                props = content.get("schema", {}).get("properties", {})
                if "dry_run" in props:
                    found.append((method, path, props["dry_run"].get("default")))
    return found


def test_every_declared_dry_run_defaults_true() -> None:
    declared = _declared_dry_run_params()
    assert declared, "no dry_run parameters found in ENDPOINT_META — did the spec change shape?"
    for method, path, default in declared:
        assert default is True, f"{method} {path}: spec default for dry_run must be True, got {default!r}"


def test_dry_run_gated_classification_matches_spec() -> None:
    """The gated flags above must agree with the spec — neither may drift alone."""
    specced = {(method, path) for method, path, _default in _declared_dry_run_params()}
    classified = {key for key, probe in FILE_WRITES.items() if probe["gated"]}
    assert specced == classified, (
        f"spec-declared dry_run endpoints {sorted(specced)} != gated FILE_WRITES entries {sorted(classified)}"
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _request_kwargs(probe: dict[str, Any], auth_headers: dict[str, str], *, apply: bool = False) -> dict[str, Any]:
    overrides = probe.get("apply", {}) if apply else {}
    kwargs: dict[str, Any] = {"headers": dict(auth_headers)}
    data = overrides.get("data", probe.get("data"))
    json_body = overrides.get("json", probe.get("json"))
    if data is not None:
        kwargs["data"] = data
        kwargs["headers"]["Content-Type"] = _TEXT
    if json_body is not None:
        kwargs["json"] = json_body
    return kwargs


def _probe_url(probe: dict[str, Any], *, apply: bool = False) -> str:
    if apply:
        return probe.get("apply", {}).get("url", probe["url"])
    return probe["url"]


@pytest.mark.parametrize(("method", "path"), _params(k for k, v in FILE_WRITES.items() if v["gated"]))
async def test_omitted_dry_run_never_touches_disk(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, method: str, path: str
) -> None:
    """C-4: on every dry-run-gated endpoint, omitting dry_run must not write."""
    probe = FILE_WRITES[(method, path)]
    if seed := probe.get("seed"):
        seed(config_dir)
    before = _snapshot(config_dir)

    resp = await client.request(method, probe["url"], **_request_kwargs(probe, auth_headers))
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    # status == "dry_run" proves the request reached the write path — without
    # this, an early 400 would make the no-write assertion pass vacuously.
    assert body["status"] == "dry_run", body

    assert _snapshot(config_dir) == before, f"{method} {path}: request without dry_run modified files on disk"


@pytest.mark.parametrize(("method", "path"), _params(FILE_WRITES.keys()))
async def test_applied_write_backs_up_every_modified_file(
    client: TestClient, auth_headers: dict[str, str], config_dir: Path, method: str, path: str
) -> None:
    """C-5: any applied change to a pre-existing file leaves its prior content in .hactl_backups/."""
    probe = FILE_WRITES[(method, path)]
    if seed := probe.get("seed"):
        seed(config_dir)
    before = _snapshot(config_dir)

    url = _probe_url(probe, apply=True)
    resp = await client.request(method, url, **_request_kwargs(probe, auth_headers, apply=True))
    assert resp.status < 400, await resp.text()
    after = _snapshot(config_dir)

    changed = {
        name
        for name, content in before.items()
        if BACKUP_DIRNAME not in Path(name).parts and after.get(name, content) != content
    }
    assert changed, f"{method} {path}: apply request modified no pre-existing file — probe no longer exercises a write"

    for name in changed:
        rel = Path(name)
        backup_rel_dir = rel.parent / BACKUP_DIRNAME
        candidates = [
            b for b in after if Path(b).parent == backup_rel_dir and Path(b).name.startswith(rel.name + ".bak.")
        ]
        assert any(after[b] == before[name] for b in candidates), (
            f"{method} {path}: modified {name} without a backup of the prior content in {backup_rel_dir}/"
        )
