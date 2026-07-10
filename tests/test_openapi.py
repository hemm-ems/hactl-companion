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


def test_spec_endpoint_count_matches_registered_routes() -> None:
    """ENDPOINT_META should have exactly one entry per registered API operation.

    Derived from the app router rather than a hardcoded number, so adding a route
    without a spec entry (or vice versa) fails here instead of drifting silently.
    """
    app = create_app()
    non_api_paths = {"/"}
    registered: set[tuple[str, str]] = set()
    for resource in app.router.resources():
        info = resource.get_info()
        path = info.get("path") or info.get("formatter", "")
        if not path or path in non_api_paths:
            continue
        for route in resource:
            method = route.method.upper()
            if method == "HEAD":
                continue
            registered.add((method, path))
    assert set(ENDPOINT_META.keys()) == registered


def test_spec_paths_count_matches_unique_paths() -> None:
    """The generated spec should cover exactly the set of unique paths in ENDPOINT_META."""
    spec = generate_spec()
    paths = spec["paths"]
    assert isinstance(paths, dict)
    assert set(paths.keys()) == {path for _method, path in ENDPOINT_META}


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


# Regression guard: if _STATUS_SCHEMA is accidentally reused (variable shadowing), these
# write/delete endpoints would inherit the capability-report schema. They must instead expose a
# `status` string plus their own write result fields (e.g. reloaded/diff), never the capability
# fields (supervisor_reachable, has_ha_cli, ...).
_WRITE_ENDPOINTS = [
    ("PUT", "/v1/config/template"),
    ("DELETE", "/v1/config/template"),
    ("PUT", "/v1/config/script"),
    ("DELETE", "/v1/config/script"),
    ("PUT", "/v1/config/automation"),
    ("DELETE", "/v1/config/automation"),
    ("PUT", "/v1/config/helper"),
    ("DELETE", "/v1/config/helper"),
]

_CAPABILITY_FIELDS = {"supervisor_reachable", "has_ha_cli", "config_writable", "ingress_active", "auth_mode"}


@pytest.mark.parametrize("method,path", _WRITE_ENDPOINTS)
def test_write_endpoints_do_not_leak_capability_schema(method: str, path: str) -> None:
    """Write/delete endpoints expose a status string, never the capability-report schema."""
    schema = ENDPOINT_META[(method, path)]["response_schema"]
    props = schema.get("properties", {})
    assert props.get("status") == {"type": "string"}, (
        f"{method} {path} must expose a 'status' string field, got: {props}"
    )
    assert not (_CAPABILITY_FIELDS & set(props)), (
        f"{method} {path} leaked capability-report fields into its schema: {set(props) & _CAPABILITY_FIELDS}"
    )
