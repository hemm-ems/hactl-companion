"""Shared WireGuard helpers used by route handlers and the startup supervisor."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

_TUNNEL_RE = re.compile(r"^[a-zA-Z0-9_]{1,15}$")

# Runtime location wg-quick reads from. Ephemeral (container layer) — wiped on
# every add-on restart, so it must never be the source of truth.
_WG_CONFIG_DIR = Path("/etc/wireguard")

# Source of truth: persistent, lives in the mapped HA /config volume so it
# survives restarts and is viewable/editable via the File Editor add-on. Both
# the REST endpoint (hactl) and the startup supervisor read/write here, which
# keeps the two in sync. /etc/wireguard is regenerated from this on demand.
_PERSIST_DIR = Path("/config/hactl")


def _persist_path(tunnel: str, persist_dir: Path = _PERSIST_DIR) -> Path:
    """Path of a tunnel's canonical (persistent) config."""
    return persist_dir / f"{tunnel}.conf"


def _runtime_path(tunnel: str) -> Path:
    """Path of a tunnel's runtime config in /etc/wireguard."""
    return _WG_CONFIG_DIR / f"{tunnel}.conf"


def materialize(tunnel: str, persist_dir: Path | None = None) -> bool:
    """Copy the persistent config into /etc/wireguard so wg-quick can use it.

    Returns False when no persistent config exists for the tunnel. ``persist_dir``
    resolves to the module default at call time so tests can monkeypatch it.
    """
    persist_dir = persist_dir if persist_dir is not None else _PERSIST_DIR
    src = _persist_path(tunnel, persist_dir)
    if not src.exists():
        return False
    _WG_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    dst = _runtime_path(tunnel)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    dst.chmod(0o600)
    return True


def save_config(tunnel: str, conf_text: str, persist_dir: Path | None = None) -> Path:
    """Validate, persist, and materialize a tunnel config.

    Writes the canonical copy to the persistent ``persist_dir`` (the source of
    truth, shared by the REST endpoint and the startup supervisor) and mirrors
    it into /etc/wireguard for wg-quick. Raises HTTPBadRequest on invalid input.
    """
    persist_dir = persist_dir if persist_dir is not None else _PERSIST_DIR
    _validate_conf(conf_text)
    persist_dir.mkdir(parents=True, exist_ok=True)
    persist = _persist_path(tunnel, persist_dir)
    persist.write_text(conf_text, encoding="utf-8")
    persist.chmod(0o600)
    materialize(tunnel, persist_dir)
    return persist


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
            iface = result["interface"]
            assert isinstance(iface, dict)
            iface["public_key"] = value
        elif key == "listening_port":
            iface = result["interface"]
            assert isinstance(iface, dict)
            iface["listening_port"] = int(value) if value.isdigit() else value
        elif key == "peer":
            current_peer = {"public_key": value}
            peers = result["peers"]
            assert isinstance(peers, list)
            peers.append(current_peer)
        elif current_peer is not None:
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


async def _enable_auto_start(tunnel: str) -> None:
    """Enable wg-quick@<tunnel> systemd service for auto-start on boot (best-effort)."""
    try:
        rc, _, stderr = await _run_wg_cmd("systemctl", "enable", f"wg-quick@{tunnel}")
        if rc != 0:
            logger.warning("Could not enable auto-start for %s: %s", tunnel, stderr.strip())
    except web.HTTPBadGateway:
        logger.warning("systemctl not available — auto-start not supported on this system")


async def _disable_auto_start(tunnel: str) -> None:
    """Disable wg-quick@<tunnel> systemd service (best-effort)."""
    try:
        rc, _, stderr = await _run_wg_cmd("systemctl", "disable", f"wg-quick@{tunnel}")
        if rc != 0:
            logger.warning("Could not disable auto-start for %s: %s", tunnel, stderr.strip())
    except web.HTTPBadGateway:
        logger.warning("systemctl not available — auto-start not supported on this system")


async def _is_auto_enabled(tunnel: str) -> bool:
    """Check if auto-start is enabled for a tunnel (best-effort)."""
    try:
        rc, stdout, _ = await _run_wg_cmd("systemctl", "is-enabled", f"wg-quick@{tunnel}")
        return rc == 0 and "enabled" in stdout.lower()
    except web.HTTPBadGateway:
        return False
