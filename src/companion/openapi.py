"""OpenAPI schema generation from registered routes."""

from __future__ import annotations

from companion import __version__

# Response schemas for each endpoint group
_HEALTH_SCHEMA = {"type": "object", "properties": {"status": {"type": "string"}, "version": {"type": "string"}}}

_CONFIG_FILES_SCHEMA = {
    "type": "object",
    "properties": {"files": {"type": "array", "items": {"type": "string"}}},
}
_CONFIG_FILE_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
}
_CONFIG_BLOCK_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "id": {"type": "string"}, "content": {"type": "string"}},
}
_CONFIG_WRITE_DRY_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "diff": {"type": "string"}},
}

_TEMPLATE_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "templates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "unique_id": {"type": "string"},
                    "name": {"type": "string"},
                    "domain": {"type": "string"},
                    "state": {"type": "string"},
                    "unit_of_measurement": {"type": "string"},
                    "device_class": {"type": "string"},
                },
            },
        }
    },
}
_TEMPLATE_SCHEMA = {
    "type": "object",
    "properties": {"unique_id": {"type": "string"}, "content": {"type": "string"}},
}
_SCRIPT_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "scripts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "alias": {"type": "string"},
                    "mode": {"type": "string"},
                    "fields": {"type": "array", "items": {"type": "object"}},
                },
            },
        }
    },
}
_SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {"id": {"type": "string"}, "content": {"type": "string"}},
}
_AUTOMATION_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "automations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "alias": {"type": "string"},
                    "mode": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        }
    },
}
_AUTOMATION_SCHEMA = {
    "type": "object",
    "properties": {"id": {"type": "string"}, "content": {"type": "string"}},
}
_STATUS_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string"}},
}
_RELOAD_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "domain": {"type": "string"}},
}
_CREATED_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "id": {"type": "string"}},
}
_CREATED_UID_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "unique_id": {"type": "string"}},
}
_HELPER_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "helpers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "domain": {"type": "string"},
                    "icon": {"type": "string"},
                },
            },
        }
    },
}
_HELPER_SCHEMA = {
    "type": "object",
    "properties": {"id": {"type": "string"}, "domain": {"type": "string"}, "content": {"type": "string"}},
}
_WG_CONFIG_RESPONSE = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "tunnel": {"type": "string"}},
}
_WG_START_RESPONSE = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "tunnel": {"type": "string"},
        "auto_enable": {"type": "boolean"},
    },
}
_WG_STOP_RESPONSE = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "tunnel": {"type": "string"}},
}
_WG_STATUS_RESPONSE = {
    "type": "object",
    "properties": {
        "tunnel": {"type": "string"},
        "state": {"type": "string", "enum": ["active", "inactive"]},
        "auto_enable": {"type": "boolean"},
        "interface": {
            "type": "object",
            "properties": {
                "public_key": {"type": "string"},
                "listening_port": {"type": "integer"},
            },
        },
        "peers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "public_key": {"type": "string"},
                    "endpoint": {"type": "string"},
                    "allowed_ips": {"type": "string"},
                    "latest_handshake": {"type": "string"},
                    "transfer_rx": {"type": "string"},
                    "transfer_tx": {"type": "string"},
                },
            },
        },
    },
}
_WG_CONFIG_JSON_BODY = {
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "properties": {
                    "tunnel_name": {"type": "string", "default": "wg0"},
                    "interface": {
                        "type": "object",
                        "required": ["private_key", "address"],
                        "properties": {
                            "private_key": {"type": "string"},
                            "address": {"type": "string"},
                            "dns": {"type": "string"},
                        },
                    },
                    "peers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["public_key", "allowed_ips"],
                            "properties": {
                                "public_key": {"type": "string"},
                                "endpoint": {"type": "string"},
                                "allowed_ips": {"type": "string"},
                                "persistent_keepalive": {"type": "integer"},
                            },
                        },
                    },
                },
            },
        },
        "text/plain": {"schema": {"type": "string"}},
    },
    "required": True,
}

# Map of (method, path) -> endpoint metadata
ENDPOINT_META: dict[tuple[str, str], dict[str, object]] = {
    # Health
    ("GET", "/v1/health"): {
        "summary": "Liveness check",
        "tags": ["health"],
        "response_schema": _HEALTH_SCHEMA,
    },
    # Config files
    ("GET", "/v1/config/files"): {
        "summary": "List YAML config files",
        "tags": ["config"],
        "response_schema": _CONFIG_FILES_SCHEMA,
    },
    ("GET", "/v1/config/file"): {
        "summary": "Read a config file (with optional !include resolution)",
        "tags": ["config"],
        "parameters": [
            {"name": "path", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "resolve", "in": "query", "required": False, "schema": {"type": "string", "default": "true"}},
        ],
        "response_schema": _CONFIG_FILE_SCHEMA,
    },
    ("GET", "/v1/config/block"): {
        "summary": "Read a specific block from a config file",
        "tags": ["config"],
        "parameters": [
            {"name": "path", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "id", "in": "query", "required": True, "schema": {"type": "string"}},
        ],
        "response_schema": _CONFIG_BLOCK_SCHEMA,
    },
    ("PUT", "/v1/config/file"): {
        "summary": "Write a config file (dry-run or apply)",
        "tags": ["config"],
        "parameters": [
            {"name": "path", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "dry_run", "in": "query", "required": False, "schema": {"type": "string", "default": "true"}},
        ],
        "requestBody": {
            "content": {"text/plain": {"schema": {"type": "string"}}},
            "required": True,
        },
        "response_schema": _CONFIG_WRITE_DRY_SCHEMA,
    },
    # Templates
    ("GET", "/v1/config/templates"): {
        "summary": "List all template sensor definitions",
        "tags": ["templates"],
        "response_schema": _TEMPLATE_LIST_SCHEMA,
    },
    ("GET", "/v1/config/template"): {
        "summary": "Get single template definition",
        "tags": ["templates"],
        "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "string"}}],
        "response_schema": _TEMPLATE_SCHEMA,
    },
    ("PUT", "/v1/config/template"): {
        "summary": "Update template definition",
        "tags": ["templates"],
        "parameters": [
            {"name": "id", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "dry_run", "in": "query", "required": False, "schema": {"type": "string", "default": "true"}},
        ],
        "requestBody": {
            "content": {"text/plain": {"schema": {"type": "string"}}},
            "required": True,
        },
        "response_schema": _STATUS_SCHEMA,
    },
    ("POST", "/v1/config/template"): {
        "summary": "Create new template sensor",
        "tags": ["templates"],
        "parameters": [
            {"name": "domain", "in": "query", "required": False, "schema": {"type": "string", "default": "sensor"}},
        ],
        "requestBody": {
            "content": {"text/plain": {"schema": {"type": "string"}}},
            "required": True,
        },
        "response_schema": _CREATED_UID_SCHEMA,
        "response_status": 201,
    },
    ("DELETE", "/v1/config/template"): {
        "summary": "Delete template sensor",
        "tags": ["templates"],
        "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "string"}}],
        "response_schema": _STATUS_SCHEMA,
    },
    # Scripts
    ("GET", "/v1/config/scripts"): {
        "summary": "List all script definitions",
        "tags": ["scripts"],
        "response_schema": _SCRIPT_LIST_SCHEMA,
    },
    ("GET", "/v1/config/script"): {
        "summary": "Get single script definition",
        "tags": ["scripts"],
        "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "string"}}],
        "response_schema": _SCRIPT_SCHEMA,
    },
    ("PUT", "/v1/config/script"): {
        "summary": "Update script definition",
        "tags": ["scripts"],
        "parameters": [
            {"name": "id", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "dry_run", "in": "query", "required": False, "schema": {"type": "string", "default": "true"}},
        ],
        "requestBody": {
            "content": {"text/plain": {"schema": {"type": "string"}}},
            "required": True,
        },
        "response_schema": _STATUS_SCHEMA,
    },
    ("POST", "/v1/config/script"): {
        "summary": "Create new script",
        "tags": ["scripts"],
        "requestBody": {
            "content": {"text/plain": {"schema": {"type": "string"}}},
            "required": True,
        },
        "response_schema": _CREATED_SCHEMA,
        "response_status": 201,
    },
    ("DELETE", "/v1/config/script"): {
        "summary": "Delete script",
        "tags": ["scripts"],
        "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "string"}}],
        "response_schema": _STATUS_SCHEMA,
    },
    # Automations
    ("GET", "/v1/config/automations"): {
        "summary": "List all automation definitions",
        "tags": ["automations"],
        "response_schema": _AUTOMATION_LIST_SCHEMA,
    },
    ("GET", "/v1/config/automation"): {
        "summary": "Get single automation definition",
        "tags": ["automations"],
        "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "string"}}],
        "response_schema": _AUTOMATION_SCHEMA,
    },
    ("PUT", "/v1/config/automation"): {
        "summary": "Update automation definition",
        "tags": ["automations"],
        "parameters": [
            {"name": "id", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "dry_run", "in": "query", "required": False, "schema": {"type": "string", "default": "true"}},
        ],
        "requestBody": {
            "content": {"text/plain": {"schema": {"type": "string"}}},
            "required": True,
        },
        "response_schema": _STATUS_SCHEMA,
    },
    ("POST", "/v1/config/automation"): {
        "summary": "Create new automation",
        "tags": ["automations"],
        "requestBody": {
            "content": {"text/plain": {"schema": {"type": "string"}}},
            "required": True,
        },
        "response_schema": _CREATED_SCHEMA,
        "response_status": 201,
    },
    ("DELETE", "/v1/config/automation"): {
        "summary": "Delete automation",
        "tags": ["automations"],
        "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "string"}}],
        "response_schema": _STATUS_SCHEMA,
    },
    # Helpers
    ("GET", "/v1/config/helpers"): {
        "summary": "List all helpers",
        "tags": ["helpers"],
        "parameters": [
            {"name": "domain", "in": "query", "required": False, "schema": {"type": "string"}},
        ],
        "response_schema": _HELPER_LIST_SCHEMA,
    },
    ("GET", "/v1/config/helper"): {
        "summary": "Get single helper definition",
        "tags": ["helpers"],
        "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "string"}}],
        "response_schema": _HELPER_SCHEMA,
    },
    ("POST", "/v1/config/helper"): {
        "summary": "Create new helper",
        "tags": ["helpers"],
        "requestBody": {
            "content": {"application/json": {"schema": {"type": "object"}}},
            "required": True,
        },
        "response_schema": _CREATED_SCHEMA,
        "response_status": 201,
    },
    ("PUT", "/v1/config/helper"): {
        "summary": "Update helper definition",
        "tags": ["helpers"],
        "requestBody": {
            "content": {"application/json": {"schema": {"type": "object"}}},
            "required": True,
        },
        "response_schema": _STATUS_SCHEMA,
    },
    ("DELETE", "/v1/config/helper"): {
        "summary": "Delete helper",
        "tags": ["helpers"],
        "parameters": [
            {"name": "domain", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "id", "in": "query", "required": True, "schema": {"type": "string"}},
        ],
        "response_schema": _STATUS_SCHEMA,
    },
    # HA CLI
    ("POST", "/v1/ha/reload/{domain}"): {
        "summary": "Reload an HA integration domain",
        "tags": ["ha"],
        "parameters": [
            {"name": "domain", "in": "path", "required": True, "schema": {"type": "string"}},
        ],
        "response_schema": _RELOAD_SCHEMA,
    },
    # WireGuard
    ("POST", "/v1/wireguard/config"): {
        "summary": "Push WireGuard tunnel configuration",
        "tags": ["wireguard"],
        "parameters": [
            {"name": "tunnel", "in": "query", "required": False, "schema": {"type": "string", "default": "wg0"}},
        ],
        "requestBody": _WG_CONFIG_JSON_BODY,
        "response_schema": _WG_CONFIG_RESPONSE,
    },
    ("POST", "/v1/wireguard/start"): {
        "summary": "Start a WireGuard tunnel",
        "tags": ["wireguard"],
        "parameters": [
            {"name": "tunnel", "in": "query", "required": False, "schema": {"type": "string", "default": "wg0"}},
            {"name": "auto_enable", "in": "query", "required": False, "schema": {"type": "string", "default": "false"}},
        ],
        "response_schema": _WG_START_RESPONSE,
    },
    ("POST", "/v1/wireguard/stop"): {
        "summary": "Stop a WireGuard tunnel",
        "tags": ["wireguard"],
        "parameters": [
            {"name": "tunnel", "in": "query", "required": False, "schema": {"type": "string", "default": "wg0"}},
            {
                "name": "auto_disable",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "default": "false"},
            },
        ],
        "response_schema": _WG_STOP_RESPONSE,
    },
    ("GET", "/v1/wireguard/status"): {
        "summary": "Get WireGuard tunnel status",
        "tags": ["wireguard"],
        "parameters": [
            {"name": "tunnel", "in": "query", "required": False, "schema": {"type": "string", "default": "wg0"}},
        ],
        "response_schema": _WG_STATUS_RESPONSE,
    },
}


def generate_spec() -> dict[str, object]:
    """Generate a full OpenAPI 3.0 spec dict from registered routes."""
    paths: dict[str, dict[str, object]] = {}

    for (method, path), meta in ENDPOINT_META.items():
        openapi_path = path
        if openapi_path not in paths:
            paths[openapi_path] = {}

        status = str(meta.get("response_status", 200))
        operation: dict[str, object] = {
            "summary": meta.get("summary", ""),
            "tags": meta.get("tags", []),
            "responses": {
                status: {
                    "description": "Successful response",
                    "content": {"application/json": {"schema": meta.get("response_schema", {})}},
                },
            },
        }

        if "parameters" in meta:
            operation["parameters"] = meta["parameters"]
        if "requestBody" in meta:
            operation["requestBody"] = meta["requestBody"]

        paths[openapi_path][method.lower()] = operation

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "hactl-companion API",
            "version": __version__,
            "description": (
                "YAML file access API for Home Assistant. "
                "Provides structured CRUD for templates, scripts, and automations, "
                "plus raw config file read/write with !include resolution."
            ),
        },
        "servers": [{"url": "/", "description": "HA Ingress"}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                },
            },
        },
        "security": [{"bearerAuth": []}],
    }


def write_spec(output_path: str = "openapi/companion-v1.yaml") -> None:
    """Generate and write the OpenAPI spec to a YAML file."""
    from pathlib import Path

    from ruamel.yaml import YAML

    spec = generate_spec()
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    y = YAML()
    y.default_flow_style = False
    with open(out, "w", encoding="utf-8") as f:
        y.dump(spec, f)


if __name__ == "__main__":
    write_spec()
