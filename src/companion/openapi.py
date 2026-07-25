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
_CONFIG_WRITE_SCHEMA = {
    "type": "object",
    "required": ["status"],
    "properties": {
        "status": {"type": "string"},
        # dry-run returns a diff; apply returns whether validation ran and the backup name.
        "diff": {"type": "string"},
        "validated": {"type": "boolean"},
        "backup": {"type": "string"},
    },
}
_RELATED_ENTITY_SCHEMA = {
    "type": "object",
    "required": ["entity_id", "stale", "related", "stale_refs"],
    "properties": {
        "entity_id": {"type": "string"},
        "stale": {"type": "boolean"},
        "related": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["entity_id", "relationship", "detail"],
                "properties": {
                    "entity_id": {"type": "string"},
                    "relationship": {
                        "type": "string",
                        "description": (
                            "How the two are related: config-entry-reference, referenced-entity, "
                            "device-reference, yaml-reference (entities co-occurring in one YAML node), "
                            "or automation-reference. automation-reference means an automation's config "
                            "mentions the queried entity; entity_id is then the automation's entity_id "
                            "(registry-resolved via its unique_id, else derived from its alias) and "
                            "detail is 'file:path-within-file (alias)'."
                        ),
                    },
                    "detail": {"type": "string"},
                },
            },
        },
        # Populated only when ?stale=true and the entity is no longer in the registry:
        # every place the literal id is still referenced across config files.
        "stale_refs": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["location", "path", "matched_value"],
                "properties": {
                    "location": {"type": "string"},
                    "path": {"type": "string"},
                    "matched_value": {"type": "string"},
                },
            },
        },
    },
}

_REF_SCAN_SCHEMA = {
    "type": "object",
    "required": ["target", "hits"],
    "properties": {
        "target": {"type": "string"},
        "hits": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["location", "path", "matched_value"],
                "properties": {
                    "location": {"type": "string"},
                    "path": {"type": "string"},
                    "matched_value": {"type": "string"},
                },
            },
        },
    },
}
_REF_ENTITIES_SCHEMA = {
    "type": "object",
    "required": ["entities"],
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["location", "path", "key", "matched_value"],
                "properties": {
                    "location": {"type": "string"},
                    "path": {"type": "string"},
                    "key": {"type": "string"},
                    "matched_value": {"type": "string"},
                },
            },
        },
    },
}
_REF_REPLACE_SCHEMA = {
    "type": "object",
    "required": ["status", "changes"],
    "properties": {
        "status": {"type": "string"},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["location", "path", "before", "after"],
                "properties": {
                    "location": {"type": "string"},
                    "path": {"type": "string"},
                    "before": {"type": "string"},
                    "after": {"type": "string"},
                },
            },
        },
    },
}
_REF_REPLACE_BODY = {
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "required": ["old", "new"],
                "properties": {
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "dry_run": {"type": "boolean", "default": True},
                },
            },
        },
    },
    "required": True,
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
                    "trigger": {
                        "type": "boolean",
                        "description": "true if the entity lives in a trigger-based block",
                    },
                },
            },
        }
    },
}
_TEMPLATE_SCHEMA = {
    "type": "object",
    "properties": {
        "unique_id": {"type": "string"},
        "content": {"type": "string"},
        "trigger": {"type": "boolean", "description": "true if the entry is trigger-based"},
    },
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
# PUT template/script/automation: dry-run returns a diff; apply returns `reloaded`.
_WRITE_RESULT_SCHEMA = {
    "type": "object",
    "required": ["status"],
    "properties": {
        "status": {"type": "string"},
        "diff": {"type": "string"},
        "reloaded": {"type": "boolean"},
    },
}
# DELETE (template/script/automation/helper) and PUT helper: {status, reloaded}.
_RELOAD_RESULT_SCHEMA = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}, "reloaded": {"type": "boolean"}},
}
_RELOAD_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string"}, "domain": {"type": "string"}},
}
_CHECK_CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "valid": {"type": "boolean"},
        "errors": {"type": "string"},
    },
}
_CREATED_SCRIPT_SCHEMA = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}, "id": {"type": "string"}, "reloaded": {"type": "boolean"}},
}
_CREATED_AUTOMATION_SCHEMA = {
    "type": "object",
    "required": ["status"],
    "properties": {
        "status": {"type": "string"},
        "id": {"type": "string"},
        "entity_id": {"type": "string", "nullable": True},
        "reloaded": {"type": "boolean"},
    },
}
_CREATED_UID_SCHEMA = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}, "unique_id": {"type": "string"}, "reloaded": {"type": "boolean"}},
}
_CREATED_HELPER_SCHEMA = {
    "type": "object",
    "required": ["status"],
    "properties": {
        "status": {"type": "string"},
        "id": {"type": "string"},
        "entity_id": {"type": "string"},
        "reloaded": {"type": "boolean"},
        "entity_created": {"type": "boolean"},
    },
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
                    "latest_handshake_secs": {"type": "integer", "nullable": True},
                    "transfer_rx": {"type": "string"},
                    "transfer_tx": {"type": "string"},
                    "transfer_rx_bytes": {"type": "integer"},
                    "transfer_tx_bytes": {"type": "integer"},
                },
            },
        },
        "monitor": {
            "type": "object",
            "description": "Live dyndns re-resolution monitor state.",
            "properties": {
                "running": {"type": "boolean"},
                "hostnames": {"type": "array", "items": {"type": "string"}},
                "healthy": {"type": "boolean"},
                "resolved": {"type": "object", "additionalProperties": {"type": "string"}},
                "last_check_secs_ago": {"type": "integer", "nullable": True},
                "last_reresolve_secs_ago": {"type": "integer", "nullable": True},
                "attempt": {"type": "integer"},
                "next_retry_secs": {"type": "integer", "nullable": True},
                "last_error": {"type": "string", "nullable": True},
            },
        },
    },
}
_LOGS_RESPONSE = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ts": {"type": "number"},
                    "level": {"type": "string"},
                    "name": {"type": "string"},
                    "message": {"type": "string"},
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
_STATUS_SCHEMA = {
    "type": "object",
    "required": ["version", "supervisor_reachable", "has_ha_cli", "config_writable", "ingress_active", "auth_mode"],
    "properties": {
        "version": {"type": "string"},
        "supervisor_reachable": {"type": "boolean"},
        "has_ha_cli": {"type": "boolean"},
        "config_writable": {"type": "boolean"},
        "ingress_active": {"type": "boolean"},
        "auth_mode": {"type": "string", "enum": ["ingress", "bearer"]},
    },
}

ENDPOINT_META: dict[tuple[str, str], dict[str, object]] = {
    # Health
    ("GET", "/v1/health"): {
        "summary": "Liveness check",
        "tags": ["health"],
        "response_schema": _HEALTH_SCHEMA,
    },
    ("GET", "/v1/status"): {
        "summary": "Companion capability report",
        "description": "Returns version and capability flags. Auth-exempt (same policy as /v1/health).",
        "tags": ["health"],
        "response_schema": _STATUS_SCHEMA,
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
            {"name": "resolve", "in": "query", "required": False, "schema": {"type": "boolean", "default": True}},
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
            {"name": "dry_run", "in": "query", "required": False, "schema": {"type": "boolean", "default": True}},
        ],
        "requestBody": {
            "content": {"text/plain": {"schema": {"type": "string"}}},
            "required": True,
        },
        "response_schema": _CONFIG_WRITE_SCHEMA,
    },
    # Related graph
    ("GET", "/v1/related/entity"): {
        "summary": "Find entities related to an entity from HA config and storage metadata",
        "tags": ["related"],
        "parameters": [
            {"name": "entity_id", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "stale", "in": "query", "required": False, "schema": {"type": "boolean", "default": False}},
        ],
        "response_schema": _RELATED_ENTITY_SCHEMA,
    },
    # Reference scan/replace
    ("GET", "/v1/ref/scan"): {
        "summary": "Find every literal reference to a value across the config file graph",
        "tags": ["ref"],
        "parameters": [
            {"name": "target", "in": "query", "required": True, "schema": {"type": "string"}},
        ],
        "response_schema": _REF_SCAN_SCHEMA,
    },
    ("GET", "/v1/ref/entities"): {
        "summary": "Enumerate every entity_id-shaped reference across config files",
        "tags": ["ref"],
        "response_schema": _REF_ENTITIES_SCHEMA,
    },
    ("POST", "/v1/ref/replace"): {
        "summary": "Rewrite a literal reference across config files (dry-run or apply)",
        "tags": ["ref"],
        "requestBody": _REF_REPLACE_BODY,
        "response_schema": _REF_REPLACE_SCHEMA,
    },
    # Templates
    ("GET", "/v1/config/templates"): {
        "summary": "List all template sensor definitions",
        "tags": ["templates"],
        "response_schema": _TEMPLATE_LIST_SCHEMA,
    },
    ("GET", "/v1/config/template"): {
        "summary": "Get single template definition",
        "description": (
            "For a trigger-based entry, `content` is the whole block (trigger + entity) so the "
            "trigger is visible; otherwise it is the entity item."
        ),
        "tags": ["templates"],
        "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "string"}}],
        "response_schema": _TEMPLATE_SCHEMA,
    },
    ("PUT", "/v1/config/template"): {
        "summary": "Update template definition",
        "tags": ["templates"],
        "parameters": [
            {"name": "id", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "dry_run", "in": "query", "required": False, "schema": {"type": "boolean", "default": True}},
        ],
        "requestBody": {
            "content": {"text/plain": {"schema": {"type": "string"}}},
            "required": True,
        },
        "response_schema": _WRITE_RESULT_SCHEMA,
    },
    ("POST", "/v1/config/template"): {
        "summary": "Create a new template entry",
        "description": (
            "Refuses with 400 unless `configuration.yaml` has a top-level `template:` key "
            "(or a labelled `template <label>:` key) that `!include`s a file — without it "
            "Home Assistant never reads `template.yaml` and the new entry would be written and "
            "ignored. The entry is written to the file the include names, not to `template.yaml` "
            "by convention. "
            "Body is either a bare entity item (placed into a state-based block for `domain`) or a "
            "full block (declares any template entity domain — `sensor:`, `number:`, `select:`, "
            "`button:`, `weather:`, … — optionally with block-level `triggers:`/`actions:`/"
            "`conditions:`), appended as its own list item. The latter is how trigger-based and "
            "multi-domain entries are created. A bare item carrying a block-level trigger key is "
            "rejected (400)."
        ),
        "tags": ["templates"],
        "parameters": [
            {
                "name": "domain",
                "in": "query",
                "required": False,
                "description": (
                    "domain for a bare entity item (any template entity domain; "
                    "default sensor); ignored for a full block"
                ),
                "schema": {"type": "string", "default": "sensor"},
            },
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
        "response_schema": _RELOAD_RESULT_SCHEMA,
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
            {"name": "dry_run", "in": "query", "required": False, "schema": {"type": "boolean", "default": True}},
        ],
        "requestBody": {
            "content": {"text/plain": {"schema": {"type": "string"}}},
            "required": True,
        },
        "response_schema": _WRITE_RESULT_SCHEMA,
    },
    ("POST", "/v1/config/script"): {
        "summary": "Create new script",
        "description": (
            "Refuses with 400 unless `configuration.yaml` has a top-level `script:` key "
            "(or a labelled `script <label>:` key) that `!include`s a file — without it "
            "Home Assistant never reads `scripts.yaml` and the new entry would be written and "
            "ignored. The entry is written to the file the include names, not to `scripts.yaml` "
            "by convention. "
        ),
        "tags": ["scripts"],
        "requestBody": {
            "content": {"text/plain": {"schema": {"type": "string"}}},
            "required": True,
        },
        "response_schema": _CREATED_SCRIPT_SCHEMA,
        "response_status": 201,
    },
    ("DELETE", "/v1/config/script"): {
        "summary": "Delete script",
        "tags": ["scripts"],
        "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "string"}}],
        "response_schema": _RELOAD_RESULT_SCHEMA,
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
            {"name": "dry_run", "in": "query", "required": False, "schema": {"type": "boolean", "default": True}},
        ],
        "requestBody": {
            "content": {"text/plain": {"schema": {"type": "string"}}},
            "required": True,
        },
        "response_schema": _WRITE_RESULT_SCHEMA,
    },
    ("POST", "/v1/config/automation"): {
        "summary": "Create new automation",
        "description": (
            "Refuses with 400 unless `configuration.yaml` has a top-level `automation:` key "
            "(or a labelled `automation <label>:` key) that `!include`s a file — without it "
            "Home Assistant never reads `automations.yaml` and the new entry would be written and "
            "ignored. The entry is written to the file the include names, not to `automations.yaml` "
            "by convention. "
        ),
        "tags": ["automations"],
        "requestBody": {
            "content": {"text/plain": {"schema": {"type": "string"}}},
            "required": True,
        },
        "response_schema": _CREATED_AUTOMATION_SCHEMA,
        "response_status": 201,
    },
    ("DELETE", "/v1/config/automation"): {
        "summary": "Delete automation",
        "tags": ["automations"],
        "parameters": [{"name": "id", "in": "query", "required": True, "schema": {"type": "string"}}],
        "response_schema": _RELOAD_RESULT_SCHEMA,
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
        "description": (
            "Refuses with 400 unless `configuration.yaml` has a top-level `<domain>:` key "
            "(or a labelled `<domain> <label>:` key) that `!include`s a file — without it Home Assistant "
            "never reads `<domain>.yaml` and the new helper would be written and ignored. The helper is "
            "written to the file the include names, not to `<domain>.yaml` by convention."
        ),
        "tags": ["helpers"],
        "parameters": [
            {"name": "domain", "in": "query", "required": True, "schema": {"type": "string"}},
        ],
        "requestBody": {
            "content": {"application/json": {"schema": {"type": "object"}}},
            "required": True,
        },
        "response_schema": _CREATED_HELPER_SCHEMA,
        "response_status": 201,
    },
    ("PUT", "/v1/config/helper"): {
        "summary": "Update helper definition",
        "tags": ["helpers"],
        "parameters": [
            {"name": "id", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "domain", "in": "query", "required": False, "schema": {"type": "string"}},
        ],
        "requestBody": {
            "content": {"application/json": {"schema": {"type": "object"}}},
            "required": True,
        },
        "response_schema": _RELOAD_RESULT_SCHEMA,
    },
    ("DELETE", "/v1/config/helper"): {
        "summary": "Delete helper",
        "tags": ["helpers"],
        "parameters": [
            {"name": "id", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "domain", "in": "query", "required": False, "schema": {"type": "string"}},
        ],
        "response_schema": _RELOAD_RESULT_SCHEMA,
    },
    # HA core API
    ("POST", "/v1/ha/reload/{domain}"): {
        "summary": "Reload an HA integration domain",
        "tags": ["ha"],
        "parameters": [
            {"name": "domain", "in": "path", "required": True, "schema": {"type": "string"}},
        ],
        "response_schema": _RELOAD_SCHEMA,
    },
    ("POST", "/v1/ha/check-config"): {
        "summary": "Validate HA configuration via the core API",
        "tags": ["ha"],
        "response_schema": _CHECK_CONFIG_SCHEMA,
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
        ],
        "response_schema": _WG_START_RESPONSE,
    },
    ("POST", "/v1/wireguard/stop"): {
        "summary": "Stop a WireGuard tunnel",
        "tags": ["wireguard"],
        "parameters": [
            {"name": "tunnel", "in": "query", "required": False, "schema": {"type": "string", "default": "wg0"}},
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
    # Logs
    ("GET", "/v1/logs"): {
        "summary": "Query recent companion log records from the in-memory ring buffer",
        "tags": ["logs"],
        "parameters": [
            {"name": "component", "in": "query", "required": False, "schema": {"type": "string"}},
            {"name": "level", "in": "query", "required": False, "schema": {"type": "string"}},
            {"name": "since", "in": "query", "required": False, "schema": {"type": "string"}},
            {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer"}},
        ],
        "response_schema": _LOGS_RESPONSE,
    },
}


def generate_spec() -> dict[str, object]:
    """Generate a full OpenAPI 3.0 spec dict from the ENDPOINT_META map."""
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
                # Every operation can return the shared JSON error envelope.
                "4XX": {"$ref": "#/components/responses/Error"},
                "5XX": {"$ref": "#/components/responses/Error"},
            },
        }

        if "description" in meta:
            operation["description"] = meta["description"]
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
            "schemas": {
                "Error": {
                    "type": "object",
                    "required": ["error"],
                    "properties": {
                        "error": {
                            "type": "object",
                            "required": ["code", "message"],
                            "properties": {
                                "code": {"type": "integer", "description": "HTTP status code"},
                                "message": {"type": "string"},
                            },
                        },
                    },
                },
            },
            "responses": {
                "Error": {
                    "description": "Error response (JSON envelope)",
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
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
