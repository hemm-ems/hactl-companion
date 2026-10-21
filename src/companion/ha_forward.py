"""Carry Home Assistant's port into the WireGuard tunnel.

The tunnel terminates in the add-on's own network namespace, where nothing
listens on HA's port — HA core runs on the host. A peer on the tunnel reached
the companion (:9100) and got "connection refused" on :8123, and hactl needs HA
itself (REST + WebSocket), so the tunnel was useless for the job it is sold for
(hactl-companion#135).

While a tunnel is up, this module listens on the tunnel's own address at HA's
port and pipes every connection, byte for byte, to HA core. Byte for byte means
TLS, WebSocket and anything else HA speaks on that port pass through untouched.

What it deliberately is not:

- not configurable: the target is HA core, resolved from the Supervisor — a
  port forward to anywhere would turn the add-on into a pivot into the LAN;
- not on ``0.0.0.0``: only the tunnel address, so nothing new appears on the
  hassio network;
- not a reason for HA to trust anyone: HA still asks every client for a login
  or token. It sees tunnel clients as this add-on's IP.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
from dataclasses import dataclass, field
from functools import partial

import aiohttp

logger = logging.getLogger(__name__)

# Inside an add-on, HA core answers at this name (Supervisor maps it to the
# hassio gateway, 172.30.32.1). Verified from an add-on on HAOS, 2026-10-20.
_HA_HOST = "homeassistant"
_DEFAULT_PORT = 8123
_CORE_INFO_URL = "http://supervisor/core/info"
_CORE_INFO_TIMEOUT = aiohttp.ClientTimeout(total=5)
_CONNECT_TIMEOUT = 10.0
_CHUNK = 64 * 1024


@dataclass
class HATarget:
    host: str
    port: int
    # From Supervisor's /core/info; None when it could not be asked.
    ssl: bool | None = None


@dataclass
class _Forward:
    target: HATarget
    servers: list[asyncio.Server] = field(default_factory=list)
    listening: list[str] = field(default_factory=list)
    writers: set[asyncio.StreamWriter] = field(default_factory=set)
    connections_total: int = 0
    connections_active: int = 0
    last_error: str | None = None


_forwards: dict[str, _Forward] = {}


async def resolve_target() -> HATarget:
    """Ask the Supervisor which port HA core serves on (and whether over TLS).

    HA's port is user-configurable (``http: server_port``), so 8123 is only the
    fallback for when the Supervisor cannot be asked — e.g. the dev stack.
    """
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if token:
        try:
            async with (
                aiohttp.ClientSession(timeout=_CORE_INFO_TIMEOUT) as session,
                session.get(_CORE_INFO_URL, headers={"Authorization": f"Bearer {token}"}) as resp,
            ):
                if resp.status == 200:
                    data = (await resp.json()).get("data") or {}
                    port = data.get("port")
                    ssl = data.get("ssl")
                    if isinstance(port, int) and 0 < port < 65536:
                        return HATarget(_HA_HOST, port, ssl if isinstance(ssl, bool) else None)
                logger.warning("Supervisor /core/info gave no HA port (HTTP %s)", resp.status)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            logger.warning("could not ask Supervisor for HA's port (%s)", type(exc).__name__)
        logger.warning("assuming HA serves on port %s", _DEFAULT_PORT)
    return HATarget(_HA_HOST, _DEFAULT_PORT)


def tunnel_addresses(conf_text: str) -> list[str]:
    """Return the host addresses from the ``[Interface]`` ``Address`` line(s)."""
    addrs: list[str] = []
    in_interface = False
    for raw in conf_text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_interface = line.lower() == "[interface]"
            continue
        key, sep, value = line.partition("=")
        if not (in_interface and sep and key.strip() == "Address"):
            continue
        for item in value.split(","):
            with contextlib.suppress(ValueError):
                addrs.append(str(ipaddress.ip_interface(item.strip()).ip))
    return addrs


def _hostport(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


async def start(tunnel: str, conf_text: str) -> None:
    """Listen on the tunnel's address(es) at HA's port. Never raises.

    Called after the interface is up, so its address is assigned and bindable.
    """
    await stop(tunnel)
    try:
        target = await resolve_target()
        fwd = _Forward(target)
        _forwards[tunnel] = fwd
        addrs = tunnel_addresses(conf_text)
        if not addrs:
            fwd.last_error = "no Address in [Interface]"
        for addr in addrs:
            try:
                server = await asyncio.start_server(partial(_handle, fwd), host=addr, port=target.port)
            except OSError as exc:
                fwd.last_error = f"listen {_hostport(addr, target.port)}: {exc.strerror or exc}"
                continue
            fwd.servers.append(server)
            fwd.listening.append(_hostport(addr, target.port))

        if not fwd.listening:
            logger.warning("HA is NOT reachable through tunnel %s: %s", tunnel, fwd.last_error)
            return
        scheme = "https" if target.ssl else "http"
        logger.info(
            "HA reachable through tunnel %s at %s (forwarded to %s)",
            tunnel,
            ", ".join(f"{scheme}://{hp}" for hp in fwd.listening),
            _hostport(target.host, target.port),
        )
    except Exception:
        logger.exception("HA forward for tunnel %s failed to start", tunnel)


async def stop(tunnel: str) -> None:
    """Stop listening and cut every forwarded connection for ``tunnel``."""
    fwd = _forwards.pop(tunnel, None)
    if fwd is None:
        return
    for server in fwd.servers:
        server.close()
    # Server.wait_closed waits for open connections too (3.12+), and an open
    # WebSocket never ends by itself — so close them first.
    for writer in list(fwd.writers):
        writer.close()
    for server in fwd.servers:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), timeout=5)
    if fwd.listening:
        logger.info("HA forward for tunnel %s stopped", tunnel)


def status(tunnel: str) -> dict[str, object]:
    """Live state for ``GET /v1/wireguard/status``."""
    fwd = _forwards.get(tunnel)
    if fwd is None:
        return {"running": False}
    return {
        "running": bool(fwd.listening),
        "listening": list(fwd.listening),
        "target": _hostport(fwd.target.host, fwd.target.port),
        "connections_total": fwd.connections_total,
        "connections_active": fwd.connections_active,
        "last_error": fwd.last_error,
    }


async def _connect(target: HATarget) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open the upstream connection to HA core (a seam for tests)."""
    return await asyncio.open_connection(target.host, target.port)


async def _handle(fwd: _Forward, client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    fwd.connections_total += 1
    fwd.connections_active += 1
    fwd.writers.add(client_w)
    upstream_w: asyncio.StreamWriter | None = None
    try:
        try:
            upstream_r, up_w = await asyncio.wait_for(_connect(fwd.target), timeout=_CONNECT_TIMEOUT)
        except (OSError, TimeoutError) as exc:
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
            error = f"connect {_hostport(fwd.target.host, fwd.target.port)}: {reason}"
            # Once per distinct failure: a client retrying in a loop must not flood the log.
            if error != fwd.last_error:
                logger.warning("HA forward: %s", error)
            fwd.last_error = error
            return
        upstream_w = up_w
        fwd.writers.add(up_w)
        await asyncio.gather(
            _pipe(client_r, up_w, client_w),
            _pipe(upstream_r, client_w, up_w),
        )
    finally:
        fwd.connections_active -= 1
        for writer in (client_w, upstream_w):
            if writer is None:
                continue
            fwd.writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, other: asyncio.StreamWriter) -> None:
    """Copy one direction until EOF, then pass the half-close on.

    A reset on either side aborts both: otherwise the opposite direction could
    wait forever on a peer that is gone.
    """
    try:
        while data := await reader.read(_CHUNK):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        writer.close()
        other.close()
