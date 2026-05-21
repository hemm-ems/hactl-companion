"""WireGuard VPN client endpoints — config, start, stop, status."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from aiohttp import web

from companion.wg import (
    _WG_CONFIG_DIR,
    _conf_from_json,
    _disable_auto_start,
    _enable_auto_start,
    _is_auto_enabled,
    _is_interface_up,
    _parse_wg_show,
    _run_wg_cmd,
    _validate_conf,
    _validate_tunnel,
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

    _validate_conf(conf_text)

    conf_dir = _WG_CONFIG_DIR
    conf_dir.mkdir(parents=True, exist_ok=True)
    conf_path = conf_dir / f"{tunnel}.conf"
    conf_path.write_text(conf_text, encoding="utf-8")
    conf_path.chmod(0o600)

    logger.info("WireGuard config written for tunnel %s", tunnel)
    return web.json_response({"status": "configured", "tunnel": tunnel})


async def post_start(request: web.Request) -> web.Response:
    """POST /v1/wireguard/start — bring up a WireGuard tunnel."""
    tunnel = _validate_tunnel(request.query.get("tunnel", "wg0"))

    if await _is_interface_up(tunnel):
        raise web.HTTPConflict(text=f"Tunnel {tunnel} is already active")

    auto_enable = request.query.get("auto_enable", "false").lower() in ("true", "1", "yes")

    rc, _, stderr = await _run_wg_cmd("wg-quick", "up", tunnel)
    if rc != 0:
        logger.error("wg-quick up %s failed (rc=%s): %s", tunnel, rc, stderr.strip())
        raise web.HTTPInternalServerError(text=f"Failed to start tunnel: {stderr.strip()}")

    if auto_enable:
        await _enable_auto_start(tunnel)

    logger.info("WireGuard tunnel %s started (auto_enable=%s)", tunnel, auto_enable)
    return web.json_response({"status": "started", "tunnel": tunnel, "auto_enable": auto_enable})


async def post_stop(request: web.Request) -> web.Response:
    """POST /v1/wireguard/stop — bring down a WireGuard tunnel."""
    tunnel = _validate_tunnel(request.query.get("tunnel", "wg0"))

    if not await _is_interface_up(tunnel):
        raise web.HTTPConflict(text=f"Tunnel {tunnel} is not active")

    auto_disable = request.query.get("auto_disable", "false").lower() in ("true", "1", "yes")

    rc, _, stderr = await _run_wg_cmd("wg-quick", "down", tunnel)
    if rc != 0:
        logger.error("wg-quick down %s failed (rc=%s): %s", tunnel, rc, stderr.strip())
        raise web.HTTPInternalServerError(text=f"Failed to stop tunnel: {stderr.strip()}")

    if auto_disable:
        await _disable_auto_start(tunnel)

    logger.info("WireGuard tunnel %s stopped (auto_disable=%s)", tunnel, auto_disable)
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
    auto = await _is_auto_enabled(tunnel)
    return web.json_response(
        {
            "tunnel": tunnel,
            "state": "active",
            "auto_enable": auto,
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
