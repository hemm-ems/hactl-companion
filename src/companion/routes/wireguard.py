"""WireGuard VPN client endpoints — config, start, stop, status."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from aiohttp import web

from companion import wg_monitor
from companion.wg import (
    _conf_from_json,
    _is_interface_up,
    _parse_wg_show,
    _resolve_endpoint_hostnames,
    _run_wg_cmd,
    _runtime_path,
    _validate_tunnel,
    materialize,
    save_config,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@dataclass
class RouteDef:
    method: str
    path: str
    handler: object


async def post_config(request: web.Request) -> web.Response:
    """POST /v1/wireguard/config — push a WireGuard config."""
    tunnel = _validate_tunnel(request.query.get("tunnel", "wg0"))

    content_type = request.content_type or ""
    body = await request.read()
    if not body:
        raise web.HTTPBadRequest(text="Empty request body")

    if "application/json" in content_type:
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise web.HTTPBadRequest(text=f"Invalid JSON: {exc}") from exc
        tunnel = _validate_tunnel(data.get("tunnel_name", tunnel))
        conf_text = _conf_from_json(data)
    else:
        conf_text = body.decode("utf-8", errors="replace")

    # Persist to the canonical location (survives restarts; shared with the
    # startup supervisor) and mirror into /etc/wireguard. save_config validates.
    save_config(tunnel, conf_text)

    logger.info("WireGuard config written for tunnel %s", tunnel)
    return web.json_response({"status": "configured", "tunnel": tunnel})


async def post_start(request: web.Request) -> web.Response:
    """POST /v1/wireguard/start — bring up a WireGuard tunnel."""
    tunnel = _validate_tunnel(request.query.get("tunnel", "wg0"))

    if await _is_interface_up(tunnel):
        raise web.HTTPConflict(text=f"Tunnel {tunnel} is already active")

    # Regenerate /etc/wireguard from the persistent config (it may have been
    # wiped by a restart). No persistent config → nothing to start.
    if not materialize(tunnel):
        raise web.HTTPNotFound(text=f"No config for tunnel {tunnel} — push one via POST /v1/wireguard/config first")

    conf_text = _runtime_path(tunnel).read_text(encoding="utf-8")
    failed = await _resolve_endpoint_hostnames(conf_text)
    if failed:
        logger.error("DNS resolution failed for endpoint(s): %s", ", ".join(failed))
        raise web.HTTPBadGateway(text=f"DNS resolution failed for endpoint(s): {', '.join(failed)}")

    rc, _, stderr = await _run_wg_cmd("wg-quick", "up", tunnel)
    if rc != 0:
        logger.error("wg-quick up %s failed (rc=%s): %s", tunnel, rc, stderr.strip())
        raise web.HTTPInternalServerError(text=f"Failed to start tunnel: {stderr.strip()}")

    wg_monitor.start_monitor(tunnel, conf_text)
    logger.info("WireGuard tunnel %s started", tunnel)
    return web.json_response({"status": "started", "tunnel": tunnel})


async def post_stop(request: web.Request) -> web.Response:
    """POST /v1/wireguard/stop — bring down a WireGuard tunnel."""
    tunnel = _validate_tunnel(request.query.get("tunnel", "wg0"))

    if not await _is_interface_up(tunnel):
        raise web.HTTPConflict(text=f"Tunnel {tunnel} is not active")

    wg_monitor.stop_monitor(tunnel)
    rc, _, stderr = await _run_wg_cmd("wg-quick", "down", tunnel)
    if rc != 0:
        logger.error("wg-quick down %s failed (rc=%s): %s", tunnel, rc, stderr.strip())
        raise web.HTTPInternalServerError(text=f"Failed to stop tunnel: {stderr.strip()}")

    logger.info("WireGuard tunnel %s stopped", tunnel)
    return web.json_response({"status": "stopped", "tunnel": tunnel})


async def get_status(request: web.Request) -> web.Response:
    """GET /v1/wireguard/status — get tunnel status."""
    tunnel = _validate_tunnel(request.query.get("tunnel", "wg0"))

    if not await _is_interface_up(tunnel):
        return web.json_response({"tunnel": tunnel, "state": "inactive"})

    rc, stdout, _ = await _run_wg_cmd("wg", "show", tunnel)
    if rc != 0:
        return web.json_response({"tunnel": tunnel, "state": "inactive"})

    parsed = _parse_wg_show(stdout)
    return web.json_response(
        {
            "tunnel": tunnel,
            "state": "active",
            **parsed,
        }
    )


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------

routes: list[RouteDef] = [
    RouteDef("POST", "/v1/wireguard/config", post_config),
    RouteDef("POST", "/v1/wireguard/start", post_start),
    RouteDef("POST", "/v1/wireguard/stop", post_stop),
    RouteDef("GET", "/v1/wireguard/status", get_status),
]
