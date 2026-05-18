"""Tests for OpenAPI spec generation and conformance."""

from __future__ import annotations

from pathlib import Path

from companion.openapi import ENDPOINT_META, generate_spec, write_spec
from companion.server import create_app


def test_spec_is_valid_openapi() -> None:
    """Generated spec should pass OpenAPI validation."""
    from openapi_spec_validator import validate

    spec = generate_spec()
    validate(spec)  # Raises on invalid spec


def test_spec_has_correct_version() -> None:
    spec = generate_spec()
    assert spec["info"]["version"] == "0.3.0"  # type: ignore[index]


def test_all_routes_have_spec_entry() -> None:
    """Every registered route in the app should have a matching OpenAPI entry."""
    app = create_app()
    spec_keys = set(ENDPOINT_META.keys())

    for resource in app.router.resources():
        info = resource.get_info()
        path = info.get("path") or info.get("formatter", "")
        if not path:
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
    """Spec should have exactly 30 endpoint operations."""
    assert len(ENDPOINT_META) == 30


def test_spec_paths_count() -> None:
    """Spec should cover all path groups."""
    spec = generate_spec()
    paths = spec["paths"]
    assert isinstance(paths, dict)
    # health(1) + config(3) + templates(2) + scripts(2) + automations(2) + helpers(2) + ha(1) + wireguard(4) = 17 paths
    assert len(paths) == 17
