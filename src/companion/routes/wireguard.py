"""WireGuard VPN client endpoints — config, start, stop, status."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

_TUNNEL_RE = re.compile(r"^[a-zA-Z0-9_]{1,15}$")

# WireGuard configs live on the add-on's *persistent* volume (/data) so they
# survive container restarts, updates, and host reboots. /etc/wireguard is the
# ephemeral container layer and would be wiped every restart, leaving wg-quick
# with no config to bring the tunnel back up. Overridable via WG_CONFIG_DIR for
# tests and local dev.
_DEFAULT_WG_CONFIG_DIR = "/data/wireguard"


def _wg_config_dir() -> Path:
    """Persistent directory holding tunnel `.conf` files and autostart markers."""
    return Path(os.environ.get("WG_CONFIG_DIR", _DEFAULT_WG_CONFIG_DIR))


def _conf_path(tunnel: str) -> Path:
    """Path of a tunnel's persistent `.conf` file."""
    return _wg_config_dir() / f"{tunnel}.conf"


def _marker_path(tunnel: str) -> Path:
    """Path of a tunnel's autostart marker (used by boot restore)."""
    return _wg_config_dir() / f"{tunnel}.autostart"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_tunnel(name: str) -> str:
    """Validate and return tunnel name; raise 400 on bad input."""
    if not _TUNNEL_RE.fullmatch(name):
        raise web.HTTPBadRequest(text="Invalid tunnel name: must be 1-15 alphanumeric/underscore characters")
    return name


def _conf_from_json(data: dict[str, object]) -> str:
    """Convert structured JSON to WireGuard .conf format."""
    iface = data.get("interface")
    if not isinstance(iface, dict):
        raise web.HTTPBadRequest(text="Missing or invalid 'interface' object")

    private_key = iface.get("private_key")
    address = iface.get("address")
    if not private_key or not address:
        raise web.HTTPBadRequest(text="interface.private_key and interface.address are required")

    lines = ["[Interface]", f"PrivateKey = {private_key}", f"Address = {address}"]
    if dns := iface.get("dns"):
        lines.append(f"DNS = {dns}")

    peers = data.get("peers")
    if not isinstance(peers, list) or len(peers) == 0:
        raise web.HTTPBadRequest(text="At least one peer is required")

    for peer in peers:
        if not isinstance(peer, dict):
            raise web.HTTPBadRequest(text="Each peer must be an object")
        pub = peer.get("public_key")
        allowed = peer.get("allowed_ips")
        if not pub or not allowed:
            raise web.HTTPBadRequest(text="peer.public_key and peer.allowed_ips are required")
        lines.append("")
        lines.append("[Peer]")
        lines.append(f"PublicKey = {pub}")
        if endpoint := peer.get("endpoint"):
            lines.append(f"Endpoint = {endpoint}")
        lines.append(f"AllowedIPs = {allowed}")
        if keepalive := peer.get("persistent_keepalive"):
            lines.append(f"PersistentKeepalive = {keepalive}")

    lines.append("")  # trailing newline
    return "\n".join(lines)


def _validate_conf(content: str) -> None:
    """Basic validation of a WireGuard .conf — must have [Interface] and [Peer]."""
    if "[Interface]" not in content:
        raise web.HTTPBadRequest(text="Config must contain an [Interface] section")
    if "[Peer]" not in content:
        raise web.HTTPBadRequest(text="Config must contain at least one [Peer] section")
    if "PrivateKey" not in content:
        raise web.HTTPBadRequest(text="Config must contain a PrivateKey in [Interface]")


def _parse_wg_show(output: str) -> dict[str, object]:
    """Parse ``wg show <iface>`` output into structured dict."""
    result: dict[str, object] = {"interface": {}, "peers": []}
    current_peer: dict[str, object] | None = None

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower().replace(" ", "_")
        value = value.strip()

        if key == "public_key" and current_peer is None:
            # Interface public key
            iface = result["interface"]
            assert isinstance(iface, dict)
            iface["public_key"] = value
        elif key == "listening_port":
            iface = result["interface"]
            assert isinstance(iface, dict)
            iface["listening_port"] = int(value) if value.isdigit() else value
        elif key == "peer":
            # New peer section
            current_peer = {"public_key": value}
            peers = result["peers"]
            assert isinstance(peers, list)
            peers.append(current_peer)
        elif current_peer is not None:
            if key in ("transfer_rx", "transfer_tx"):
                # e.g. "1.23 KiB" — keep as string for simplicity
                current_peer[key] = value
            elif key == "latest_handshake":
                current_peer[key] = value
            else:
                current_peer[key] = value

    return result


async def _run_wg_cmd(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a WireGuard command, return (returncode, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except FileNotFoundError as exc:
        raise web.HTTPBadGateway(text=f"Command not found: {args[0]}") from exc
    except TimeoutError as exc:
        raise web.HTTPGatewayTimeout(text=f"Command timed out after {timeout}s") from exc

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    return proc.returncode or 0, stdout, stderr


async def _is_interface_up(tunnel: str) -> bool:
    """Check if a WireGuard interface is currently active."""
    rc, _, _ = await _run_wg_cmd("wg", "show", tunnel)
    return rc == 0


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

    # Write config to the persistent volume so it survives restarts.
    conf_path = _conf_path(tunnel)
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    conf_path.write_text(conf_text, encoding="utf-8")
    conf_path.chmod(0o600)

    logger.info("WireGuard config written for tunnel %s", tunnel)
    return web.json_response({"status": "configured", "tunnel": tunnel})


async def post_start(request: web.Request) -> web.Response:
    """POST /v1/wireguard/start — bring up a WireGuard tunnel."""
    tunnel = _validate_tunnel(request.query.get("tunnel", "wg0"))

    if await _is_interface_up(tunnel):
        raise web.HTTPConflict(text=f"Tunnel {tunnel} is already active")

    conf_path = _conf_path(tunnel)
    if not conf_path.exists():
        raise web.HTTPNotFound(text=f"No config for tunnel {tunnel} — push one via POST /v1/wireguard/config first")

    auto_enable = request.query.get("auto_enable", "false").lower() in ("true", "1", "yes")

    # Pass the full config path: wg-quick derives the interface name from the
    # basename, and the config no longer lives in the default /etc/wireguard.
    rc, _, stderr = await _run_wg_cmd("wg-quick", "up", str(conf_path))
    if rc != 0:
        logger.error("wg-quick up %s failed (rc=%s): %s", tunnel, rc, stderr.strip())
        raise web.HTTPInternalServerError(text=f"Failed to start tunnel: {stderr.strip()}")

    # Optionally enable auto-start via systemd
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

    # wg-quick down also needs the config path (it re-reads it to tear down the
    # interface), and the config lives on the persistent volume now.
    rc, _, stderr = await _run_wg_cmd("wg-quick", "down", str(_conf_path(tunnel)))
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
# Auto-start helpers (persistent marker + on_startup restore)
#
# The add-on image is Alpine with no systemd, so `systemctl enable wg-quick@`
# is a silent no-op. Instead we record auto-start intent as a marker file next
# to the config on the persistent volume, and `restore_tunnels` (an aiohttp
# on_startup hook) brings up every marked tunnel when the container starts.
# ---------------------------------------------------------------------------


async def _enable_auto_start(tunnel: str) -> None:
    """Mark a tunnel for auto-start on boot."""
    marker = _marker_path(tunnel)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


async def _disable_auto_start(tunnel: str) -> None:
    """Remove a tunnel's auto-start marker."""
    _marker_path(tunnel).unlink(missing_ok=True)


async def _is_auto_enabled(tunnel: str) -> bool:
    """Check if auto-start is enabled for a tunnel."""
    return _marker_path(tunnel).exists()


async def restore_tunnels(app: web.Application) -> None:
    """aiohttp on_startup hook: bring up every tunnel marked for auto-start.

    Best-effort — logs and continues on any failure so a single bad tunnel can
    never block the companion from starting.
    """
    conf_dir = _wg_config_dir()
    if not conf_dir.is_dir():
        return
    for marker in sorted(conf_dir.glob("*.autostart")):
        tunnel = marker.stem
        try:
            conf_path = _conf_path(tunnel)
            if not conf_path.exists():
                logger.warning("autostart marker for %s but no config — skipping", tunnel)
                continue
            if await _is_interface_up(tunnel):
                logger.info("tunnel %s already up — skipping autostart", tunnel)
                continue
            rc, _, stderr = await _run_wg_cmd("wg-quick", "up", str(conf_path))
            if rc != 0:
                logger.error("autostart wg-quick up %s failed (rc=%s): %s", tunnel, rc, stderr.strip())
            else:
                logger.info("autostart: tunnel %s brought up", tunnel)
        except Exception:
            # Never let one bad tunnel block the companion from starting.
            logger.exception("autostart for tunnel %s raised", tunnel)


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------

routes: list[RouteDef] = [
    RouteDef("POST", "/v1/wireguard/config", post_config),
    RouteDef("POST", "/v1/wireguard/start", post_start),
    RouteDef("POST", "/v1/wireguard/stop", post_stop),
    RouteDef("GET", "/v1/wireguard/status", get_status),
]
