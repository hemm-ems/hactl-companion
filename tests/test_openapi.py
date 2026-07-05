"""Tests for OpenAPI spec generation and conformance."""

from __future__ import annotations

from pathlib import Path

import pytest

from companion import __version__
from companion.openapi import ENDPOINT_META, generate_spec, write_spec
from companion.server import create_app


def test_spec_is_valid_openapi() -> None:
    """Generated spec should pass OpenAPI validation."""
    from openapi_spec_validator import validate

    spec = generate_spec()
    validate(spec)  # Raises on invalid spec


def test_spec_has_correct_version() -> None:
    spec = generate_spec()
    assert spec["info"]["version"] == __version__  # type: ignore[index]


def test_all_routes_have_spec_entry() -> None:
    """Every registered API route in the app should have a matching OpenAPI entry.

    Non-API routes (e.g. GET / status page) are excluded from this check.
    """
    app = create_app()
    spec_keys = set(ENDPOINT_META.keys())
    non_api_paths = {"/"}

    for resource in app.router.resources():
        info = resource.get_info()
        path = info.get("path") or info.get("formatter", "")
        if not path or path in non_api_paths:
            continue
        for route in resource:
            method = route.method.upper()
            if method == "HEAD":
                continue
            assert (method, path) in spec_keys, f"Route {method} {path} not in OpenAPI spec"


def test_all_spec_entries_have_routes() -> None:
    """Every OpenAPI entry should correspond to a registered route."""
    app = create_app()
    registered: set[tuple[str, str]] = set()

    for resource in app.router.resources():
        info = resource.get_info()
        path = info.get("path") or info.get("formatter", "")
        if not path:
            continue
        for route in resource:
            method = route.method.upper()
            registered.add((method, path))

    for method, path in ENDPOINT_META:
        assert (method, path) in registered, f"Spec entry {method} {path} has no registered route"


def test_write_spec_to_file(tmp_path: Path) -> None:
    """Should write a valid YAML spec file."""
    output = tmp_path / "companion-v1.yaml"
    write_spec(str(output))
    assert output.is_file()
    content = output.read_text()
    assert "openapi: 3.0.3" in content or "openapi:" in content


def test_spec_has_20_endpoints() -> None:
    """Spec should have exactly 36 endpoint operations (34 + ref scan/replace)."""
    assert len(ENDPOINT_META) == 36


def test_spec_paths_count() -> None:
    """Spec should cover all path groups."""
    spec = generate_spec()
    paths = spec["paths"]
    assert isinstance(paths, dict)
    # health(2) config(3) related(1) ref(2) templates(2) scripts(2) automations(2)
    # helpers(2) ha(2) wireguard(4) logs(1) = 23 paths
    assert len(paths) == 23


def test_committed_spec_matches_generator(tmp_path: Path) -> None:
    """openapi/companion-v1.yaml must be in sync with write_spec().

    If this fails, run: make spec
    """
    generated = tmp_path / "generated.yaml"
    write_spec(str(generated))
    committed = Path(__file__).parent.parent / "openapi" / "companion-v1.yaml"
    assert generated.read_text() == committed.read_text(), (
        "Committed spec is out of sync with the generator. Run: make spec"
    )


def test_status_endpoint_uses_capability_schema() -> None:
    """GET /v1/status must use the full capability schema, not the simple {status: string} schema."""
    schema = ENDPOINT_META[("GET", "/v1/status")]["response_schema"]
    assert "required" in schema, "/v1/status schema must have a 'required' list"
    required = schema["required"]
    for field in ("version", "supervisor_reachable", "has_ha_cli", "config_writable", "ingress_active", "auth_mode"):
        assert field in required, f"field '{field}' missing from /v1/status schema required list"


# Regression guard: if _STATUS_SCHEMA is redefined (variable shadowing), these endpoints would
# silently inherit the capability-report schema instead of the simple {status: string} schema.
_SIMPLE_STATUS_ENDPOINTS = [
    ("PUT", "/v1/config/template"),
    ("DELETE", "/v1/config/template"),
    ("PUT", "/v1/config/script"),
    ("DELETE", "/v1/config/script"),
    ("PUT", "/v1/config/automation"),
    ("DELETE", "/v1/config/automation"),
    ("PUT", "/v1/config/helper"),
    ("DELETE", "/v1/config/helper"),
]


@pytest.mark.parametrize("method,path", _SIMPLE_STATUS_ENDPOINTS)
def test_write_endpoints_use_simple_status_schema(method: str, path: str) -> None:
    """Write/delete/check endpoints must return {status: string}, not the capability-report schema."""
    schema = ENDPOINT_META[(method, path)]["response_schema"]
    assert schema.get("properties") == {"status": {"type": "string"}}, (
        f"{method} {path} must have {{status: string}} schema, got: {schema.get('properties')}"
    )
    assert "required" not in schema, (
        f"{method} {path} must not carry a 'required' list — got the capability schema by mistake"
    )
