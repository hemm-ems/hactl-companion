"""Unit tests for the HA forward into the WireGuard tunnel (hactl-companion#135)."""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from companion import ha_forward, wg_supervisor
from companion.ha_forward import HATarget, tunnel_addresses


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _conf(address: str) -> str:
    return f"[Interface]\nPrivateKey = k\nAddress = {address}\n[Peer]\nPublicKey = p\nAllowedIPs = 10.0.0.0/24\n"


# ---------------------------------------------------------------------------
# tunnel_addresses
# ---------------------------------------------------------------------------


class TestTunnelAddresses:
    def test_single(self) -> None:
        assert tunnel_addresses(_conf("10.6.0.4/24")) == ["10.6.0.4"]

    def test_comma_separated_dual_stack(self) -> None:
        assert tunnel_addresses(_conf("10.6.0.4/24, fd00::4/64")) == ["10.6.0.4", "fd00::4"]

    def test_peer_section_ignored(self) -> None:
        conf = "[Interface]\nAddress = 10.6.0.4/24\n[Peer]\nAddress = 10.9.9.9/32\n"
        assert tunnel_addresses(conf) == ["10.6.0.4"]

    def test_garbage_entry_skipped(self) -> None:
        assert tunnel_addresses(_conf("not-an-ip, 10.6.0.4/24")) == ["10.6.0.4"]

    def test_none(self) -> None:
        assert tunnel_addresses("[Interface]\nPrivateKey = k\n") == []


# ---------------------------------------------------------------------------
# resolve_target — HA's port comes from the Supervisor, 8123 only as fallback
# ---------------------------------------------------------------------------


class TestResolveTarget:
    async def test_no_token_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        assert await ha_forward.resolve_target() == HATarget("homeassistant", 8123)

    async def _serve(self, monkeypatch: pytest.MonkeyPatch, handler) -> TestServer:
        app = web.Application()
        app.router.add_get("/core/info", handler)
        server = TestServer(app)
        await server.start_server()
        monkeypatch.setattr(ha_forward, "_CORE_INFO_URL", str(server.make_url("/core/info")))
        monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
        return server

    async def test_port_and_ssl_from_core_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        async def info(request: web.Request) -> web.Response:
            seen.append(request.headers.get("Authorization", ""))
            # Shape observed on HAOS (jansHA, 2026-10-20), trimmed.
            return web.json_response({"result": "ok", "data": {"port": 8443, "ssl": True, "ip_address": "172.30.32.1"}})

        server = await self._serve(monkeypatch, info)
        try:
            assert await ha_forward.resolve_target() == HATarget("homeassistant", 8443, True)
            assert seen == ["Bearer tok"]
        finally:
            await server.close()

    async def test_error_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def info(request: web.Request) -> web.Response:
            return web.json_response({"result": "error"}, status=403)

        server = await self._serve(monkeypatch, info)
        try:
            assert await ha_forward.resolve_target() == HATarget("homeassistant", 8123)
        finally:
            await server.close()


# ---------------------------------------------------------------------------
# The forward itself
# ---------------------------------------------------------------------------


@pytest.fixture
async def fake_ha() -> AsyncIterator[tuple[int, list[asyncio.StreamWriter]]]:
    """A stand-in for HA core: echoes each chunk upper-cased, keeps the line open."""
    writers: list[asyncio.StreamWriter] = []

    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        writers.append(w)
        while data := await r.read(1024):
            w.write(data.upper())
            await w.drain()
        w.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield port, writers
    server.close()
    for w in writers:
        w.close()


@pytest.fixture
async def forward(
    fake_ha: tuple[int, list[asyncio.StreamWriter]], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[int]:
    """Start a forward for tunnel 'wg0' on 127.0.0.1 and return its listen port.

    In production the listen port IS HA's port; here both sides share one
    loopback address, so the upstream connect is pointed at the fake instead.
    """
    ha_port, _ = fake_ha
    listen_port = _free_port()
    monkeypatch.setattr(ha_forward, "resolve_target", AsyncMock(return_value=HATarget("homeassistant", listen_port)))

    async def connect(target: HATarget) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection("127.0.0.1", ha_port)

    monkeypatch.setattr(ha_forward, "_connect", connect)
    await ha_forward.start("wg0", _conf("127.0.0.1/8"))
    yield listen_port
    await ha_forward.stop("wg0")


class TestForward:
    async def test_bytes_both_ways_and_half_close(self, forward: int) -> None:
        r, w = await asyncio.open_connection("127.0.0.1", forward)
        w.write(b"get /api/ http/1.1\r\n\r\n")
        w.write_eof()
        assert await asyncio.wait_for(r.read(), timeout=5) == b"GET /API/ HTTP/1.1\r\n\r\n"
        w.close()

    async def test_long_lived_conversation(self, forward: int) -> None:
        """WebSocket-shaped: many exchanges over one connection that stays open."""
        r, w = await asyncio.open_connection("127.0.0.1", forward)
        for i in range(5):
            w.write(f"ping {i}".encode())
            await w.drain()
            assert await asyncio.wait_for(r.read(1024), timeout=5) == f"PING {i}".encode()
        assert ha_forward.status("wg0")["connections_active"] == 1
        w.close()

    async def test_binds_tunnel_address_only(self, forward: int) -> None:
        st = ha_forward.status("wg0")
        assert st["running"] is True
        assert st["listening"] == [f"127.0.0.1:{forward}"]
        servers = ha_forward._forwards["wg0"].servers
        bound = {sock.getsockname()[0] for server in servers for sock in server.sockets}
        assert bound == {"127.0.0.1"}

    async def test_target_is_ha_core(self, forward: int) -> None:
        assert ha_forward.status("wg0")["target"] == f"homeassistant:{forward}"

    async def test_stop_cuts_open_connections_and_listener(self, forward: int) -> None:
        r, w = await asyncio.open_connection("127.0.0.1", forward)
        w.write(b"x")
        await w.drain()
        await asyncio.wait_for(r.read(1), timeout=5)

        t0 = time.monotonic()
        await ha_forward.stop("wg0")
        # An open WebSocket must not hold stop() hostage (Server.wait_closed).
        assert time.monotonic() - t0 < 3
        assert await asyncio.wait_for(r.read(), timeout=5) == b""
        assert ha_forward.status("wg0") == {"running": False}
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", forward)
        w.close()

    async def test_ha_down_closes_client_and_records_error(
        self, forward: int, fake_ha: tuple[int, list[asyncio.StreamWriter]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def refused(target: HATarget) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            raise ConnectionRefusedError(111, "Connection refused")

        monkeypatch.setattr(ha_forward, "_connect", refused)
        r, w = await asyncio.open_connection("127.0.0.1", forward)
        assert await asyncio.wait_for(r.read(), timeout=5) == b""
        st = ha_forward.status("wg0")
        assert st["last_error"] == f"connect homeassistant:{forward}: Connection refused"
        assert st["connections_active"] == 0
        w.close()

    async def test_listen_failure_is_reported_not_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ha_forward, "resolve_target", AsyncMock(return_value=HATarget("homeassistant", 8123)))
        # 192.0.2.1 (TEST-NET-1) is not assigned here, like a tunnel address before wg-quick up.
        await ha_forward.start("wgx", _conf("192.0.2.1/24"))
        try:
            st = ha_forward.status("wgx")
            assert st["running"] is False
            assert st["listening"] == []
            assert str(st["last_error"]).startswith("listen 192.0.2.1:8123:")
        finally:
            await ha_forward.stop("wgx")

    async def test_restart_replaces_previous_forward(self, forward: int) -> None:
        first = ha_forward._forwards["wg0"]
        await ha_forward.start("wg0", _conf("127.0.0.1/8"))
        assert ha_forward._forwards["wg0"] is not first
        assert ha_forward.status("wg0")["running"] is True


# ---------------------------------------------------------------------------
# The vpn.forward_ha switch, on both paths that bring a tunnel up
# ---------------------------------------------------------------------------


class TestForwardSwitch:
    async def _run_startup(self, forward_ha: bool) -> AsyncMock:
        app = web.Application()
        start = AsyncMock()
        with (
            patch.object(ha_forward, "start", start),
            patch.object(ha_forward, "stop", AsyncMock()),
            patch.object(wg_supervisor.wg_monitor, "start_monitor"),
            patch.object(wg_supervisor, "_watch_connection", AsyncMock()),
        ):
            wg_supervisor.register_startup_tasks(app, "wg0", _conf("10.6.0.4/24"), forward_ha=forward_ha)
            for hook in app.on_startup:
                await hook(app)
        return start

    async def test_startup_forwards_by_default(self) -> None:
        start = await self._run_startup(True)
        start.assert_awaited_once_with("wg0", _conf("10.6.0.4/24"))

    async def test_startup_off_switch(self) -> None:
        start = await self._run_startup(False)
        start.assert_not_awaited()
