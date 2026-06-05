"""Shared WireGuard helpers used by route handlers and the startup supervisor."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
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
    conf_text = _normalize_conf(conf_text)
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


# WireGuard config keys are a fixed vocabulary, which lets us reconstruct a
# config whose line breaks were eaten by an HA add-on options text field (a
# single-line `str`), e.g. a paste that arrives as
# "[Interface]PrivateKey=...Address=...[Peer]PublicKey=...". We put each section
# header and key=value on its own line so wg-quick can parse it.
_WG_KEYS = (
    "PrivateKey",
    "Address",
    "ListenPort",
    "FwMark",
    "DNS",
    "MTU",
    "Table",
    "PreUp",
    "PostUp",
    "PreDown",
    "PostDown",
    "SaveConfig",
    "PublicKey",
    "PresharedKey",
    "AllowedIPs",
    "Endpoint",
    "PersistentKeepalive",
)
_ENDPOINT_RE = re.compile(r"^Endpoint\s*=\s*\[?([^\]]+)\]?:(\d+)\s*$", re.MULTILINE)
_HOSTPORT_RE = re.compile(r"^\[?([^\]]+)\]?:(\d+)$")

_WG_SECTION_RE = re.compile(r"\s*(\[(?:Interface|Peer)\])\s*", re.IGNORECASE)
# Match a known key immediately followed by '='. No word-boundary requirement —
# a value may abut the next key with no separator (e.g. "…:51826AllowedIPs=…").
# Case-sensitive PascalCase keeps this from tripping on base64 key material.
_WG_KEY_RE = re.compile(r"\s*(" + "|".join(_WG_KEYS) + r")\s*=\s*")


def _normalize_conf(content: str) -> str:
    """Reconstruct a tidy wg.conf even if line breaks/spaces were stripped.

    Idempotent on already well-formed input. Each ``[Interface]``/``[Peer]``
    header and each known ``Key = Value`` is placed on its own line. Values are
    preserved verbatim (only the key token and surrounding ``=`` are touched), so
    base64 keys, IPs, and ``host:port`` endpoints are unaffected.
    """
    content = _WG_SECTION_RE.sub(lambda m: "\n" + m.group(1) + "\n", content)
    content = _WG_KEY_RE.sub(lambda m: "\n" + m.group(1) + " = ", content)
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return "\n".join(lines) + "\n" if lines else content


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


async def _dns_lookup_ip(host: str, *, timeout: float = 5.0) -> str | None:
    """Resolve a hostname to its first address string; return None on failure."""
    try:
        loop = asyncio.get_running_loop()
        results = await asyncio.wait_for(loop.getaddrinfo(host, None), timeout=timeout)
        return results[0][4][0]
    except (TimeoutError, socket.gaierror, OSError, IndexError):
        return None


async def _resolve_endpoint_hostnames(conf_text: str) -> list[str]:
    """Return list of hostname endpoints that fail DNS resolution (IPs are skipped)."""
    failed = []
    for m in _ENDPOINT_RE.finditer(conf_text):
        host = m.group(1)
        try:
            ipaddress.ip_address(host)
            continue  # literal IP — skip
        except ValueError:
            pass
        if await _dns_lookup_ip(host) is None:
            failed.append(host)
    return failed


@dataclass
class _PeerEndpoint:
    pubkey: str
    hostname: str
    port: int


def _parse_hostname_peers(conf_text: str) -> list[_PeerEndpoint]:
    """Return one entry per peer whose Endpoint is a hostname (not a literal IP)."""
    peers: list[_PeerEndpoint] = []
    current: dict[str, str] = {}
    in_peer = False
    for line in conf_text.splitlines():
        line = line.strip()
        if line.lower() == "[peer]":
            _flush_peer(current, peers)
            current = {}
            in_peer = True
        elif line.lower() == "[interface]":
            in_peer = False
        elif in_peer and "=" in line:
            k, _, v = line.partition("=")
            current[k.strip()] = v.strip()
    _flush_peer(current, peers)
    return peers


def _flush_peer(data: dict[str, str], out: list[_PeerEndpoint]) -> None:
    pubkey = data.get("PublicKey")
    endpoint = data.get("Endpoint", "")
    m = _HOSTPORT_RE.match(endpoint)
    if not pubkey or not m:
        return
    host, port = m.group(1), int(m.group(2))
    try:
        ipaddress.ip_address(host)
        return  # literal IP — monitor not needed
    except ValueError:
        pass
    out.append(_PeerEndpoint(pubkey, host, port))
